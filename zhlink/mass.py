from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

from zhc_rawtx import split_largest_utxo  # type: ignore

from .api import (
    SATOSHIS,
    _address_from_wif,
    _amount_raw,
    _broadcast_with_fallback,
    _fetch_zeroscan,
    _normalize_utxos,
    _rpc_call,
)
from .config import ZHLinkConfig


DEFAULT_REORG_TARGET_UTXOS = 100
DEFAULT_MIN_REORG_OUTPUT_ZHC = Decimal("1")
DEFAULT_REORG_FEE_SAT = 10_000_000
DEFAULT_BATCH_DELAY_SECONDS = 2.0
DEFAULT_BLOCK_POLL_SECONDS = 15.0
MASS_SEND_TEMPLATE_NAMES = ("usdz", "zhc", "zrc20")


@dataclass(frozen=True)
class MassRecipient:
    address: str
    amount: Decimal


@dataclass(frozen=True)
class MassSendPlan:
    asset: str
    recipients: tuple[MassRecipient, ...]
    token_contract: str | None = None
    token_decimals: int = 8
    gas: int = 1_000_000
    note: str = ""

    @property
    def is_zhc(self) -> bool:
        return self.asset.upper() == "ZHC"

    @property
    def is_usdz(self) -> bool:
        return self.asset.upper() == "USDZ"

    @property
    def required_tx_count(self) -> int:
        return len(self.recipients)


def get_mass_send_template(name: str = "usdz") -> dict[str, Any]:
    """Return a bundled mass-send JSON template as a dict.

    Available names: ``usdz``, ``zhc`` and ``zrc20``.
    """

    normalized = str(name or "").strip().lower()
    if normalized not in MASS_SEND_TEMPLATE_NAMES:
        raise ValueError(f"unknown mass-send template {name!r}; choose one of {', '.join(MASS_SEND_TEMPLATE_NAMES)}")
    template_path = resources.files("zhlink").joinpath("templates", f"mass_send_{normalized}.json")
    return json.loads(template_path.read_text(encoding="utf-8"))


def write_mass_send_template(name: str, output_path: str | Path) -> Path:
    """Write a bundled mass-send template to ``output_path`` and return path."""

    data = get_mass_send_template(name)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_mass_send_plan(path_or_data: str | Path | Mapping[str, Any]) -> MassSendPlan:
    """Load a mass-send plan from JSON path or already parsed dict.

    Supported JSON:

    ```json
    {
      "asset": "USDZ",
      "token_contract": "a48d...",
      "token_decimals": 8,
      "recipients": [
        {"address": "Z...", "amount": "0.1"}
      ]
    }
    ```
    """

    if isinstance(path_or_data, Mapping):
        data = dict(path_or_data)
    else:
        raw = Path(path_or_data).read_text(encoding="utf-8")
        data = json.loads(raw)
    asset = str(data.get("asset") or data.get("symbol") or "").strip().upper()
    if not asset:
        raise ValueError("mass-send plan must include asset")
    raw_recipients = data.get("recipients")
    if not isinstance(raw_recipients, list) or not raw_recipients:
        raise ValueError("mass-send plan must include non-empty recipients list")
    recipients: list[MassRecipient] = []
    for index, item in enumerate(raw_recipients, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"recipient #{index} must be an object")
        address = str(item.get("address") or item.get("to") or "").strip()
        amount = Decimal(str(item.get("amount") or "0"))
        if not address:
            raise ValueError(f"recipient #{index} has empty address")
        if amount <= 0:
            raise ValueError(f"recipient #{index} amount must be greater than zero")
        recipients.append(MassRecipient(address=address, amount=amount))
    return MassSendPlan(
        asset=asset,
        recipients=tuple(recipients),
        token_contract=(str(data.get("token_contract") or data.get("contract") or "").strip() or None),
        token_decimals=int(data.get("token_decimals") or data.get("decimals") or 8),
        gas=int(data.get("gas") or 1_000_000),
        note=str(data.get("note") or ""),
    )


def _spendable_confirmed_utxos(config: ZHLinkConfig, address: str) -> list[dict[str, Any]]:
    utxos = _normalize_utxos(_fetch_zeroscan(config, f"/address/{address}/utxo"))
    return [
        item
        for item in utxos
        if int(item.get("confirmations", 0)) >= 1
        and int(item.get("value_sat", 0)) > 0
        and not item.get("coinbase")
        and not item.get("coinstake")
    ]


def estimate_mass_send(
    private_key_wif: str,
    plan: MassSendPlan | str | Path | Mapping[str, Any],
    *,
    config: ZHLinkConfig | None = None,
) -> dict[str, Any]:
    """Estimate whether current confirmed UTXO are enough for a mass send."""

    cfg = config or ZHLinkConfig()
    parsed = load_mass_send_plan(plan) if not isinstance(plan, MassSendPlan) else plan
    from_address = _address_from_wif(private_key_wif)
    utxos = _spendable_confirmed_utxos(cfg, from_address)
    confirmed_sat = sum(int(item.get("value_sat", 0)) for item in utxos)
    required_txs = parsed.required_tx_count
    need_reorg = len(utxos) < required_txs
    largest_sat = max([int(item.get("value_sat", 0)) for item in utxos] or [0])
    max_split_outputs_at_1_zhc = max(0, (largest_sat - DEFAULT_REORG_FEE_SAT) // int(DEFAULT_MIN_REORG_OUTPUT_ZHC * SATOSHIS))
    return {
        "from_address": from_address,
        "asset": parsed.asset,
        "recipient_count": required_txs,
        "confirmed_utxo_count": len(utxos),
        "confirmed_zhc": str(Decimal(confirmed_sat) / SATOSHIS),
        "need_reorg": need_reorg,
        "recommended_reorg_target_utxos": DEFAULT_REORG_TARGET_UTXOS if need_reorg or required_txs > DEFAULT_REORG_TARGET_UTXOS else required_txs,
        "min_reorg_output_zhc": str(DEFAULT_MIN_REORG_OUTPUT_ZHC),
        "largest_utxo_zhc": str(Decimal(largest_sat) / SATOSHIS),
        "largest_utxo_can_split_to_1_zhc_outputs": int(max_split_outputs_at_1_zhc),
        "warning": (
            "Not enough confirmed UTXO for one-transaction-per-recipient mass send. "
            "Run prepare_mass_send_utxos first and wait for confirmation."
            if need_reorg
            else ""
        ),
    }


async def async_estimate_mass_send(
    private_key_wif: str,
    plan: MassSendPlan | str | Path | Mapping[str, Any],
    *,
    config: ZHLinkConfig | None = None,
) -> dict[str, Any]:
    """Async version of ``estimate_mass_send``."""

    return await asyncio.to_thread(
        estimate_mass_send,
        private_key_wif,
        plan,
        config=config,
    )


def _current_block(config: ZHLinkConfig) -> int:
    return int(_rpc_call(config, "getblockcount", []))


def wait_for_next_block(
    *,
    config: ZHLinkConfig | None = None,
    from_height: int | None = None,
    poll_seconds: float = DEFAULT_BLOCK_POLL_SECONDS,
    timeout_seconds: float = 3600,
) -> int:
    """Wait until the public RPC reports a block higher than ``from_height``."""

    cfg = config or ZHLinkConfig()
    start = time.time()
    initial = _current_block(cfg) if from_height is None else int(from_height)
    while True:
        height = _current_block(cfg)
        if height > initial:
            return height
        if time.time() - start > timeout_seconds:
            raise TimeoutError(f"timed out waiting for next block after {initial}")
        time.sleep(float(poll_seconds))


async def async_wait_for_next_block(
    *,
    config: ZHLinkConfig | None = None,
    from_height: int | None = None,
    poll_seconds: float = DEFAULT_BLOCK_POLL_SECONDS,
    timeout_seconds: float = 3600,
) -> int:
    """Async version of ``wait_for_next_block``."""

    return await asyncio.to_thread(
        wait_for_next_block,
        config=config,
        from_height=from_height,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )


def prepare_mass_send_utxos(
    private_key_wif: str,
    plan: MassSendPlan | str | Path | Mapping[str, Any],
    *,
    config: ZHLinkConfig | None = None,
    target_utxos: int = DEFAULT_REORG_TARGET_UTXOS,
    min_output_zhc: str | Decimal = DEFAULT_MIN_REORG_OUTPUT_ZHC,
    wait_confirmation: bool = True,
    broadcast: bool = True,
) -> dict[str, Any]:
    """Prepare confirmed UTXO for a large mass-send.

    If there are too few UTXO, the largest UTXO is split into up to
    ``target_utxos`` self-outputs. Every created output must be at least
    ``min_output_zhc``. If there are too many small UTXO, this can also
    consolidate before a later split by passing a smaller ``target_utxos``.
    """

    cfg = config or ZHLinkConfig()
    parsed = load_mass_send_plan(plan) if not isinstance(plan, MassSendPlan) else plan
    from_address = _address_from_wif(private_key_wif)
    utxos = _spendable_confirmed_utxos(cfg, from_address)
    target = max(2, min(100, int(target_utxos)))
    min_output_sat = int(Decimal(str(min_output_zhc)) * SATOSHIS)
    if min_output_sat < int(DEFAULT_MIN_REORG_OUTPUT_ZHC * SATOSHIS):
        raise ValueError("min_output_zhc must be at least 1 ZHC")
    if len(utxos) >= parsed.required_tx_count and len(utxos) >= min(target, parsed.required_tx_count):
        return {
            "status": "ok",
            "action": "none",
            "from_address": from_address,
            "confirmed_utxo_count": len(utxos),
            "message": "Enough confirmed UTXO are already available.",
        }

    largest_sat = max([int(item.get("value_sat", 0)) for item in utxos] or [0])
    max_outputs = max(0, (largest_sat - DEFAULT_REORG_FEE_SAT) // min_output_sat)
    output_count = min(target, max(2, parsed.required_tx_count), int(max_outputs))
    if output_count < 2:
        raise RuntimeError(
            "Cannot split UTXO for mass-send: largest confirmed UTXO is too small. "
            f"largest={Decimal(largest_sat) / SATOSHIS} ZHC, min_output={min_output_zhc} ZHC"
        )

    built = split_largest_utxo(
        address=from_address,
        private_key_wif=private_key_wif,
        utxos=utxos,
        output_count=output_count,
        fee_sat=DEFAULT_REORG_FEE_SAT,
    )
    if not broadcast:
        return {
            "status": "ok",
            "action": "split",
            "from_address": from_address,
            "output_count": output_count,
            "built": built,
        }
    height_before = _current_block(cfg)
    sent = _broadcast_with_fallback(cfg, built["rawtx"])
    result = {
        "status": "ok",
        "action": "split",
        "from_address": from_address,
        "output_count": output_count,
        "broadcast": sent,
        "height_before": height_before,
    }
    if wait_confirmation:
        result["confirmed_height"] = wait_for_next_block(config=cfg, from_height=height_before)
    return result


async def async_prepare_mass_send_utxos(
    private_key_wif: str,
    plan: MassSendPlan | str | Path | Mapping[str, Any],
    *,
    config: ZHLinkConfig | None = None,
    target_utxos: int = DEFAULT_REORG_TARGET_UTXOS,
    min_output_zhc: str | Decimal = DEFAULT_MIN_REORG_OUTPUT_ZHC,
    wait_confirmation: bool = True,
    broadcast: bool = True,
) -> dict[str, Any]:
    """Async version of ``prepare_mass_send_utxos``."""

    return await asyncio.to_thread(
        prepare_mass_send_utxos,
        private_key_wif,
        plan,
        config=config,
        target_utxos=target_utxos,
        min_output_zhc=min_output_zhc,
        wait_confirmation=wait_confirmation,
        broadcast=broadcast,
    )


async def _send_mass_async(
    private_key_wif: str,
    plan: MassSendPlan,
    *,
    config: ZHLinkConfig | None,
    batch_delay_seconds: float,
    wait_between_batches: bool,
    max_batch_size: int | None,
) -> dict[str, Any]:
    from .rpc import ZHCashRPC, _build_zrc20_transfer_data

    cfg = config or ZHLinkConfig()
    from_address = _address_from_wif(private_key_wif)
    contract = plan.token_contract or (cfg.usdz_contract if plan.is_usdz else "")
    if not plan.is_zhc and not contract:
        raise ValueError("token mass-send requires token_contract, except USDZ which uses config.usdz_contract")

    pending = list(plan.recipients)
    sent: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    batch_index = 0

    while pending:
        utxos = _spendable_confirmed_utxos(cfg, from_address)
        available = len(utxos)
        if available <= 0:
            raise RuntimeError(
                "No confirmed UTXO available for the next mass-send transaction. "
                "Wait for a block or run prepare_mass_send_utxos."
            )
        current_batch_size = min(len(pending), available, int(max_batch_size or available))
        batch = pending[:current_batch_size]
        pending = pending[current_batch_size:]
        batch_index += 1
        client = ZHCashRPC(cfg)
        try:
            for item in batch:
                if plan.is_zhc:
                    result = await client.send_zhc(
                        from_address=from_address,
                        to_address=item.address,
                        amount=float(item.amount),
                        private_key=private_key_wif,
                    )
                else:
                    recipient_hex = await client.gethexaddress(item.address)
                    amount_raw = _amount_raw(item.amount, plan.token_decimals)
                    data_hex = _build_zrc20_transfer_data(recipient_hex, amount_raw)
                    result = await client.send_to_contract(
                        contract_address=str(contract),
                        from_address=from_address,
                        to_address=item.address,
                        amount=0.0,
                        private_key=private_key_wif,
                        hex_command=data_hex,
                        gas=plan.gas,
                        require_bool_success=True,
                    )
                row = {
                    "batch": batch_index,
                    "address": item.address,
                    "amount": str(item.amount),
                    "asset": plan.asset,
                    "result": result,
                }
                if result.get("status") == "ok":
                    sent.append(row)
                else:
                    failed.append(row)
                if batch_delay_seconds > 0:
                    await asyncio.sleep(float(batch_delay_seconds))
        finally:
            await client.close()
        if pending and wait_between_batches:
            await asyncio.to_thread(wait_for_next_block, config=cfg)

    return {
        "status": "ok" if not failed else "partial",
        "from_address": from_address,
        "asset": plan.asset,
        "token_contract": contract or None,
        "recipient_count": len(plan.recipients),
        "sent_count": len(sent),
        "failed_count": len(failed),
        "sent": sent,
        "failed": failed,
    }


def send_mass(
    private_key_wif: str,
    plan: MassSendPlan | str | Path | Mapping[str, Any],
    *,
    config: ZHLinkConfig | None = None,
    auto_prepare_utxos: bool = True,
    batch_delay_seconds: float = DEFAULT_BATCH_DELAY_SECONDS,
    wait_between_batches: bool = True,
    max_batch_size: int | None = None,
) -> dict[str, Any]:
    """Send ZHC, USDZ or any ZRC20 token to many recipients from JSON.

    The function never starts more transactions in a batch than confirmed UTXO
    currently available. If `auto_prepare_utxos` is enabled and UTXO are
    insufficient, it first splits the largest UTXO into up to 100 outputs of at
    least 1 ZHC, waits for confirmation, and then starts the mailing.
    """

    cfg = config or ZHLinkConfig()
    from .api import _run

    result = _run(
        async_send_mass(
            private_key_wif,
            plan,
            config=cfg,
            auto_prepare_utxos=auto_prepare_utxos,
            batch_delay_seconds=batch_delay_seconds,
            wait_between_batches=wait_between_batches,
            max_batch_size=max_batch_size,
        )
    )
    return result


async def async_send_mass(
    private_key_wif: str,
    plan: MassSendPlan | str | Path | Mapping[str, Any],
    *,
    config: ZHLinkConfig | None = None,
    auto_prepare_utxos: bool = True,
    batch_delay_seconds: float = DEFAULT_BATCH_DELAY_SECONDS,
    wait_between_batches: bool = True,
    max_batch_size: int | None = None,
) -> dict[str, Any]:
    """Async version of ``send_mass``."""

    parsed = load_mass_send_plan(plan) if not isinstance(plan, MassSendPlan) else plan
    cfg = config or ZHLinkConfig()
    estimate = await async_estimate_mass_send(private_key_wif, parsed, config=cfg)
    prepare_result: dict[str, Any] | None = None
    if auto_prepare_utxos and estimate["need_reorg"]:
        prepare_result = await async_prepare_mass_send_utxos(
            private_key_wif,
            parsed,
            config=cfg,
            target_utxos=DEFAULT_REORG_TARGET_UTXOS,
            min_output_zhc=DEFAULT_MIN_REORG_OUTPUT_ZHC,
            wait_confirmation=True,
            broadcast=True,
        )
    result = await _send_mass_async(
        private_key_wif,
        parsed,
        config=cfg,
        batch_delay_seconds=batch_delay_seconds,
        wait_between_batches=wait_between_batches,
        max_batch_size=max_batch_size,
    )
    result["initial_estimate"] = estimate
    if prepare_result is not None:
        result["prepare_utxos"] = prepare_result
    return result


__all__ = [
    "MASS_SEND_TEMPLATE_NAMES",
    "MassRecipient",
    "MassSendPlan",
    "async_estimate_mass_send",
    "async_prepare_mass_send_utxos",
    "async_send_mass",
    "async_wait_for_next_block",
    "estimate_mass_send",
    "get_mass_send_template",
    "load_mass_send_plan",
    "prepare_mass_send_utxos",
    "send_mass",
    "wait_for_next_block",
    "write_mass_send_template",
]

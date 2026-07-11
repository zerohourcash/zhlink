from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from . import _rawtx_bridge  # noqa: F401
from .address import BitcoinAddress, WalletKey, create_wallet
from .config import DEFAULT_USDZ_CONTRACT, ZHLinkConfig
from zhc_rawtx import GasFreeStore, send_usdz_gas_freee  # type: ignore
from zhc_rawtx.gasfree import zhc_address_hash  # type: ignore


SATOSHIS = Decimal("100000000")
GASFREE_GAS_LIMIT = 350_000
GASFREE_GAS_PRICE = 40
GASFREE_NETWORK_FEE_SAT = 10_000_000
GASFREE_MIN_ADMIN_UTXO_SAT = 50_000_000


@dataclass(frozen=True)
class Balance:
    address: str
    zhc: Decimal
    usdz: Decimal
    tokens: dict[str, Decimal]
    utxo_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "zhc": str(self.zhc),
            "usdz": str(self.usdz),
            "tokens": {symbol: str(value) for symbol, value in self.tokens.items()},
            "utxo_count": self.utxo_count,
        }


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "zhlink sync API was called from a running asyncio loop. "
        "Use the async ZHCashRPC client directly in async applications."
    )


def _amount_raw(amount: str | int | float | Decimal, decimals: int = 8) -> int:
    value = Decimal(str(amount))
    if value <= 0:
        raise ValueError("amount must be greater than zero")
    scale = Decimal(10) ** int(decimals)
    return int((value * scale).to_integral_value(rounding=ROUND_HALF_UP))


def _from_raw(value: int | str, decimals: int = 8) -> Decimal:
    scale = Decimal(10) ** int(decimals)
    return (Decimal(int(value)) / scale).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def _address_from_wif(private_key_wif: str) -> str:
    return BitcoinAddress().address_from_wif(private_key_wif)


def create_address() -> WalletKey:
    """Create one local ZHCASH address and WIF private key."""

    return create_wallet()


async def _get_balance_async(
    address: str,
    *,
    config: ZHLinkConfig | None = None,
    tokens: Mapping[str, str] | None = None,
    token_decimals: Mapping[str, int] | None = None,
) -> Balance:
    from .rpc import ZHCashRPC

    client = ZHCashRPC(config)
    try:
        base = await client.getbalance(address)
        if base.get("status") != "ok":
            raise RuntimeError(base.get("reason") or base)
        zhc = Decimal(str(base.get("balance", "0")))
        decimals = dict(token_decimals or {})
        contracts = {"USDZ": (config or ZHLinkConfig()).usdz_contract}
        contracts.update({symbol.upper(): contract for symbol, contract in (tokens or {}).items()})
        token_values: dict[str, Decimal] = {}
        for symbol, contract in contracts.items():
            raw = await client.get_zrc20_balance_raw(contract, address)
            token_values[symbol] = _from_raw(raw, decimals.get(symbol, 8))
        return Balance(
            address=address,
            zhc=zhc,
            usdz=token_values.get("USDZ", Decimal("0")),
            tokens=token_values,
            utxo_count=int(base.get("utxo_len") or 0),
        )
    finally:
        await client.close()


def get_balance(
    address: str,
    *,
    config: ZHLinkConfig | None = None,
    tokens: Mapping[str, str] | None = None,
    token_decimals: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Return ZHC + USDZ balance.

    Extra ZRC-20 contracts can be passed as ``tokens={"EDS": "contracthex"}``.
    """

    return _run(
        _get_balance_async(
            address,
            config=config,
            tokens=tokens,
            token_decimals=token_decimals,
        )
    ).as_dict()


async def _send_zhc_async(
    private_key_wif: str,
    to_address: str,
    amount: str | int | float | Decimal,
    *,
    config: ZHLinkConfig | None = None,
) -> dict[str, Any]:
    from .rpc import ZHCashRPC

    from_address = _address_from_wif(private_key_wif)
    client = ZHCashRPC(config)
    try:
        result = await client.send_zhc(
            from_address=from_address,
            to_address=to_address,
            amount=float(Decimal(str(amount))),
            private_key=private_key_wif,
        )
        result.setdefault("from_address", from_address)
        result.setdefault("to_address", to_address)
        result.setdefault("amount", str(amount))
        return result
    finally:
        await client.close()


def send_zhc(
    private_key_wif: str,
    to_address: str,
    amount: str | int | float | Decimal,
    *,
    config: ZHLinkConfig | None = None,
) -> dict[str, Any]:
    """Send native ZHC.

    The sender address is derived from ``private_key_wif`` automatically.
    """

    return _run(_send_zhc_async(private_key_wif, to_address, amount, config=config))


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
    data = None
    headers = {"accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else None


def _rpc_call(config: ZHLinkConfig, method: str, params: list[Any]) -> Any:
    errors: list[str] = []
    for url in config.public_rpc_urls:
        try:
            data = _http_json(
                "POST",
                url,
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=config.timeout_seconds,
            )
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(data["error"])
            return data.get("result") if isinstance(data, dict) else data
        except Exception as exc:
            errors.append(f"{url}={exc}")
    raise RuntimeError(f"all RPC endpoints failed for {method}: {'; '.join(errors)}")


def _fetch_zeroscan(config: ZHLinkConfig, path: str) -> Any:
    errors: list[str] = []
    for base in config.zeroscan_endpoints:
        try:
            return _http_json("GET", f"{base.rstrip('/')}/{path.lstrip('/')}", timeout=config.timeout_seconds)
        except Exception as exc:
            errors.append(f"{base}={exc}")
    raise RuntimeError(f"all ZeroScan endpoints failed for {path}: {'; '.join(errors)}")


def _extract_txid(data: Any) -> str:
    if isinstance(data, str) and len(data) == 64:
        return data
    if isinstance(data, dict):
        for key in ("txid", "id", "hash", "result"):
            value = data.get(key)
            if isinstance(value, str) and len(value) == 64:
                return value
        nested = data.get("result")
        if isinstance(nested, dict):
            return _extract_txid(nested)
    return ""


def _broadcast_zeroscan(config: ZHLinkConfig, rawtx: str) -> dict[str, Any]:
    errors: list[str] = []
    for base in config.zeroscan_endpoints:
        try:
            data = _http_json("POST", f"{base.rstrip('/')}/tx/send", {"rawtx": rawtx}, timeout=config.timeout_seconds)
            txid = _extract_txid(data)
            if not txid:
                raise RuntimeError(f"unexpected response: {data}")
            return {"ok": True, "txid": txid, "endpoint": base, "transport": "zeroscan", "raw": data}
        except Exception as exc:
            errors.append(f"{base}={exc}")
    raise RuntimeError("; ".join(errors))


def _broadcast_rpc(config: ZHLinkConfig, rawtx: str) -> dict[str, Any]:
    txid = _rpc_call(config, "sendrawtransaction", [rawtx, True])
    if not isinstance(txid, str) or len(txid) != 64:
        raise RuntimeError(f"unexpected sendrawtransaction response: {txid}")
    return {"ok": True, "txid": txid, "transport": "rpc", "endpoint": "rpc"}


def _broadcast_with_fallback(config: ZHLinkConfig, rawtx: str) -> dict[str, Any]:
    errors: list[str] = []
    try:
        return _broadcast_zeroscan(config, rawtx)
    except Exception as exc:
        errors.append(f"zeroscan={exc}")
    try:
        result = _broadcast_rpc(config, rawtx)
        result["fallbackErrors"] = errors
        return result
    except Exception as exc:
        errors.append(f"rpc={exc}")
    raise RuntimeError("; ".join(errors))


def _normalize_utxos(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("utxos") or raw.get("unspents") or []
    result: list[dict[str, Any]] = []
    for item in raw or []:
        script = item.get("script_pubkey") or item.get("scriptPubKeyHex") or item.get("scriptPubKey")
        if isinstance(script, dict):
            script = script.get("hex")
        txid = item.get("txid") or item.get("transactionId")
        vout = item.get("vout", item.get("outputIndex"))
        if not txid or vout is None or not script:
            continue
        if "value_sat" in item:
            value_sat = int(item["value_sat"])
        elif "value" in item:
            value_sat = int(item["value"])
        elif "amount" in item:
            value_sat = int((Decimal(str(item["amount"])) * SATOSHIS).to_integral_value())
        else:
            continue
        result.append(
            {
                "txid": str(txid),
                "vout": int(vout),
                "value_sat": value_sat,
                "script_pubkey": str(script),
                "confirmations": int(item.get("confirmations") or 0),
                "coinbase": bool(item.get("coinbase")),
                "coinstake": bool(item.get("coinstake") or item.get("isStake")),
            }
        )
    return result


def _zrc20_balance_raw(config: ZHLinkConfig, contract: str, address: str) -> int:
    data_hex = "70a08231" + zhc_address_hash(address).hex().rjust(64, "0")
    result = _rpc_call(config, "callcontract", [contract, data_hex, address, 1_000_000])
    execution = ((result or {}).get("executionResult") or {})
    excepted = execution.get("excepted")
    if excepted not in (None, "", "None", "none"):
        raise RuntimeError(f"contract read reverted: {execution.get('exceptedMessage') or excepted}")
    return int((execution.get("output") or "0")[-64:] or "0", 16)


def _preflight_usdz_transfer(config: ZHLinkConfig, sender: str, recipient: str, amount_raw: int) -> dict[str, Any]:
    from zhc_rawtx.gasfree import build_transfer_data  # type: ignore

    data_hex = build_transfer_data(recipient, amount_raw)
    result = _rpc_call(config, "callcontract", [config.usdz_contract, data_hex, sender, GASFREE_GAS_LIMIT])
    execution = ((result or {}).get("executionResult") or {})
    excepted = execution.get("excepted")
    if excepted not in (None, "", "None", "none"):
        raise RuntimeError(f"USDZ dry-run reverted: {execution.get('exceptedMessage') or execution.get('output') or excepted}")
    output = (execution.get("output") or "").lower()
    if output and int(output[-64:], 16) == 0:
        raise RuntimeError("USDZ dry-run returned false")
    return result


def _testmempoolaccept(config: ZHLinkConfig, rawtx: str) -> dict[str, Any]:
    try:
        result = _rpc_call(config, "testmempoolaccept", [[rawtx], False])
        entry = result[0] if isinstance(result, list) and result else {}
        return {
            "allowed": bool(entry.get("allowed")),
            "reject-reason": entry.get("reject-reason"),
            "txid": entry.get("txid"),
            "raw": entry,
        }
    except Exception as exc:
        return {"allowed": False, "reject-reason": f"testmempoolaccept unavailable: {exc}"}


def admin_gas_wallet_info(
    admin_private_key_wif: str,
    *,
    config: ZHLinkConfig | None = None,
    store_path: str | Path = ".zhlink-gasfree-utxos.json",
) -> dict[str, Any]:
    """Return gas-ticket UTXO capacity for the admin gas wallet."""

    cfg = config or ZHLinkConfig()
    admin_address = _address_from_wif(admin_private_key_wif)
    store = GasFreeStore(store_path)
    used = store.used_outpoints()
    utxos = _normalize_utxos(_fetch_zeroscan(cfg, f"/address/{admin_address}/utxo"))
    suitable = [
        u
        for u in utxos
        if f"{str(u.get('txid', '')).lower()}:{int(u.get('vout', 0))}" not in used
        and int(u.get("confirmations", 0)) >= 1
        and int(u.get("value_sat", 0)) >= GASFREE_MIN_ADMIN_UTXO_SAT
    ]
    confirmed_sat = sum(int(u.get("value_sat", 0)) for u in utxos if int(u.get("confirmations", 0)) >= 1)
    return {
        "address": admin_address,
        "utxo_count": len(utxos),
        "suitable_gas_utxo_count": len(suitable),
        "confirmed_zhc": str(Decimal(confirmed_sat) / SATOSHIS),
        "recommended_ticket_zhc": "0.5",
        "recommended_split_count": int(max(0, (confirmed_sat - GASFREE_NETWORK_FEE_SAT) // GASFREE_MIN_ADMIN_UTXO_SAT)),
        "reserved_or_used_count": len(used),
    }


def send_usdz_gas_free(
    sender_private_key_wif: str,
    admin_private_key_wif: str,
    to_address: str,
    amount: str | int | float | Decimal,
    *,
    config: ZHLinkConfig | None = None,
    store_path: str | Path = ".zhlink-gasfree-utxos.json",
    broadcast: bool = True,
    max_attempts: int = 5,
) -> dict[str, Any]:
    """Send USDZ while the admin wallet pays ZHC gas.

    The function performs balance check, contract dry-run, UTXO selection,
    ``testmempoolaccept`` and ZeroScan/RPC broadcast fallback automatically.
    """

    cfg = config or ZHLinkConfig()
    sender_address = _address_from_wif(sender_private_key_wif)
    admin_address = _address_from_wif(admin_private_key_wif)
    amount_raw = _amount_raw(amount)

    balance_raw = _zrc20_balance_raw(cfg, cfg.usdz_contract, sender_address)
    if balance_raw < amount_raw:
        raise RuntimeError(
            f"insufficient USDZ balance: have {_from_raw(balance_raw)} USDZ, "
            f"need {_from_raw(amount_raw)} USDZ"
        )
    contract_check = _preflight_usdz_transfer(cfg, sender_address, to_address, amount_raw)

    store = GasFreeStore(store_path)
    admin_utxos = _normalize_utxos(_fetch_zeroscan(cfg, f"/address/{admin_address}/utxo"))
    if not admin_utxos:
        raise RuntimeError(f"admin gas wallet has no UTXO: {admin_address}")

    normal_utxos = [
        u
        for u in admin_utxos
        if int(u.get("confirmations", 0)) >= 1
        and int(u.get("value_sat", 0)) >= GASFREE_MIN_ADMIN_UTXO_SAT
    ]
    if not normal_utxos:
        raise RuntimeError(
            "admin gas wallet has no suitable gas-ticket UTXO. "
            "Prepare separate UTXO of at least 0.5 ZHC; one gas-free transaction needs one independent UTXO."
        )

    mempool_checks: list[dict[str, Any]] = []

    def preflight(rawtx: str) -> dict[str, Any]:
        check = _testmempoolaccept(cfg, rawtx)
        mempool_checks.append(check)
        return check

    result = send_usdz_gas_freee(
        sender_private_key_wif=sender_private_key_wif,
        sender_address=sender_address,
        to_address=to_address,
        amount_raw=amount_raw,
        admin_fee_private_key_wif=admin_private_key_wif,
        admin_fee_address=admin_address,
        admin_fee_utxos=normal_utxos,
        contract_address_hex=cfg.usdz_contract,
        gas_limit=GASFREE_GAS_LIMIT,
        gas_price=GASFREE_GAS_PRICE,
        network_fee_sat=GASFREE_NETWORK_FEE_SAT,
        store=store,
        preflight=preflight,
        broadcast=(lambda rawtx: _broadcast_with_fallback(cfg, rawtx)) if broadcast else None,
        max_attempts=max_attempts,
        reserve_local=broadcast,
    )
    result["sender_address"] = sender_address
    result["admin_address"] = admin_address
    result["to_address"] = to_address
    result["amount"] = str(Decimal(str(amount)))
    result["amount_raw"] = amount_raw
    result["contract_check"] = contract_check
    result["testmempoolaccept"] = mempool_checks
    return result


__all__ = [
    "Balance",
    "ZHLinkConfig",
    "admin_gas_wallet_info",
    "create_address",
    "get_balance",
    "send_zhc",
    "send_usdz_gas_free",
]

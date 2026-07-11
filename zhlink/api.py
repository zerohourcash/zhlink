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
from .cache import SQLiteBalanceCache
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
    confirmed_zhc: Decimal | None = None
    pending_zhc: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "address": self.address,
            "zhc": str(self.zhc),
            "usdz": str(self.usdz),
            "tokens": {symbol: str(value) for symbol, value in self.tokens.items()},
            "utxo_count": self.utxo_count,
        }
        if self.confirmed_zhc is not None:
            data["confirmed_zhc"] = str(self.confirmed_zhc)
        if self.pending_zhc is not None:
            data["pending_zhc"] = str(self.pending_zhc)
        return data


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


def _requested_token_symbols(tokens: Mapping[str, str] | None) -> set[str]:
    symbols = {"USDZ"}
    symbols.update(symbol.upper() for symbol in (tokens or {}).keys())
    return symbols


def _balance_from_cached_payload(
    address: str,
    payload: Mapping[str, Any],
    requested_symbols: set[str],
) -> Balance | None:
    token_payload = payload.get("tokens")
    if not isinstance(token_payload, Mapping):
        return None
    cached_symbols = {str(symbol).upper() for symbol in token_payload.keys()}
    if not requested_symbols.issubset(cached_symbols):
        return None
    return Balance(
        address=address,
        zhc=Decimal(str(payload.get("zhc", payload.get("balance", "0")))),
        usdz=Decimal(str(payload.get("usdz", token_payload.get("USDZ", "0")))),
        tokens={
            str(symbol).upper(): Decimal(str(value))
            for symbol, value in token_payload.items()
            if str(symbol).upper() in requested_symbols
        },
        utxo_count=int(payload.get("utxo_count") or payload.get("utxo_len") or 0),
        confirmed_zhc=Decimal(str(payload["confirmed_zhc"]))
        if "confirmed_zhc" in payload
        else Decimal(str(payload["confirmed_balance"]))
        if "confirmed_balance" in payload and payload.get("confirmed_balance") is not None
        else None,
        pending_zhc=Decimal(str(payload["pending_zhc"]))
        if "pending_zhc" in payload
        else Decimal(str(payload["pending_balance"]))
        if "pending_balance" in payload and payload.get("pending_balance") is not None
        else None,
    )


def create_address() -> WalletKey:
    """Create one local ZHCASH address and WIF private key."""

    return create_wallet()


def new_wallet() -> WalletKey:
    """Create one local ZHCASH wallet.

    Beginner-friendly alias for ``create_address()``.
    """

    return create_address()


async def async_new_wallet() -> WalletKey:
    """Async alias for ``new_wallet()``."""

    return create_address()


async def _get_balance_async(
    address: str,
    *,
    config: ZHLinkConfig | None = None,
    tokens: Mapping[str, str] | None = None,
    token_decimals: Mapping[str, int] | None = None,
    force_refresh: bool = False,
) -> Balance:
    from .rpc import ZHCashRPC

    cfg = config or ZHLinkConfig()
    requested_symbols = _requested_token_symbols(tokens)
    cached = SQLiteBalanceCache(cfg.cache_path).get_balance(address)
    if not force_refresh and cached:
        cached_balance = _balance_from_cached_payload(address, cached, requested_symbols)
        if cached_balance is not None:
            return cached_balance

    client = ZHCashRPC(config)
    try:
        base = await client.getbalance(address, force_refresh=force_refresh)
        if base.get("status") != "ok":
            raise RuntimeError(base.get("reason") or base)
        if base.get("cached") or base.get("throttled"):
            cached_balance = _balance_from_cached_payload(address, base, requested_symbols)
            if cached_balance is not None:
                return cached_balance
        zhc = Decimal(str(base.get("balance", "0")))
        confirmed_zhc = (
            Decimal(str(base["confirmed_balance"]))
            if "confirmed_balance" in base
            else Decimal(str(base["confirmed_zhc"]))
            if "confirmed_zhc" in base
            else None
        )
        pending_zhc = (
            Decimal(str(base["pending_balance"]))
            if "pending_balance" in base
            else Decimal(str(base["pending_zhc"]))
            if "pending_zhc" in base
            else None
        )
        decimals = dict(token_decimals or {})
        contracts = {"USDZ": cfg.usdz_contract}
        contracts.update({symbol.upper(): contract for symbol, contract in (tokens or {}).items()})
        token_values: dict[str, Decimal] = {}
        for symbol, contract in contracts.items():
            raw = await client.get_zrc20_balance_raw(contract, address)
            token_values[symbol] = _from_raw(raw, decimals.get(symbol, 8))
        balance = Balance(
            address=address,
            zhc=zhc,
            usdz=token_values.get("USDZ", Decimal("0")),
            tokens=token_values,
            utxo_count=int(base.get("utxo_len") or 0),
            confirmed_zhc=confirmed_zhc,
            pending_zhc=pending_zhc,
        )
        client.sqlite_cache.put_balance(
            address,
            {
                **base,
                **balance.as_dict(),
            },
            int(base.get("height") or 0),
        )
        return balance
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


async def async_get_balance(
    address: str,
    *,
    config: ZHLinkConfig | None = None,
    tokens: Mapping[str, str] | None = None,
    token_decimals: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Async version of ``get_balance``."""

    return (
        await _get_balance_async(
            address,
            config=config,
            tokens=tokens,
            token_decimals=token_decimals,
        )
    ).as_dict()


def balance(
    address: str,
    *,
    config: ZHLinkConfig | None = None,
    tokens: Mapping[str, str] | None = None,
    token_decimals: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Return ZHC + USDZ balance.

    Beginner-friendly alias for ``get_balance()``.
    """

    return get_balance(
        address,
        config=config,
        tokens=tokens,
        token_decimals=token_decimals,
    )


async def async_balance(
    address: str,
    *,
    config: ZHLinkConfig | None = None,
    tokens: Mapping[str, str] | None = None,
    token_decimals: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Async alias for ``balance()``."""

    return await async_get_balance(
        address,
        config=config,
        tokens=tokens,
        token_decimals=token_decimals,
    )


def force_refresh_balance(
    address: str,
    *,
    config: ZHLinkConfig | None = None,
    tokens: Mapping[str, str] | None = None,
    token_decimals: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Refresh an address balance, throttled by ``config.force_refresh_seconds``.

    If called more often than the configured interval, the last SQLite snapshot
    is returned with ``throttled=True``.
    """

    return _run(
        _get_balance_async(
            address,
            config=config,
            tokens=tokens,
            token_decimals=token_decimals,
            force_refresh=True,
        )
    ).as_dict()


async def async_force_refresh_balance(
    address: str,
    *,
    config: ZHLinkConfig | None = None,
    tokens: Mapping[str, str] | None = None,
    token_decimals: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Async version of ``force_refresh_balance``."""

    return (
        await _get_balance_async(
            address,
            config=config,
            tokens=tokens,
            token_decimals=token_decimals,
            force_refresh=True,
        )
    ).as_dict()


async def watch_balance(
    address: str,
    *,
    config: ZHLinkConfig | None = None,
    tokens: Mapping[str, str] | None = None,
    token_decimals: Mapping[str, int] | None = None,
    emit_initial: bool = True,
):
    """Yield balance snapshots with WSS-first updates and HTTP/RPC fallback."""

    from .rpc import ZHCashRPC

    cfg = config or ZHLinkConfig()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    client = ZHCashRPC(cfg)

    async def on_update(_address: str, _snapshot: dict[str, Any]) -> None:
        try:
            balance = await _get_balance_async(
                address,
                config=cfg,
                tokens=tokens,
                token_decimals=token_decimals,
                force_refresh=True,
            )
            payload = balance.as_dict()
            if isinstance(_snapshot, Mapping):
                event = _snapshot.get("event")
                if event:
                    payload["realtime_event"] = event
                payload["realtime_source"] = _snapshot.get("source") or (
                    event.get("payload", {}).get("source")
                    if isinstance(event, Mapping)
                    else None
                )
            await queue.put(payload)
        except Exception as exc:
            await queue.put({"address": address, "status": "error", "reason": str(exc)})

    unsubscribe = client.subscribe_balance(address, on_update)
    await client.start_block_watch()
    try:
        if emit_initial:
            await queue.put(
                (
                    await _get_balance_async(
                        address,
                        config=cfg,
                        tokens=tokens,
                        token_decimals=token_decimals,
                    )
                ).as_dict()
            )
        while True:
            yield await queue.get()
    finally:
        unsubscribe()
        await client.close()


def get_cached_balance(
    address: str,
    *,
    config: ZHLinkConfig | None = None,
) -> dict[str, Any] | None:
    """Return the last SQLite balance snapshot without network access."""

    cfg = config or ZHLinkConfig()
    return SQLiteBalanceCache(cfg.cache_path).get_balance(address)


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


async def async_send_zhc(
    private_key_wif: str,
    to_address: str,
    amount: str | int | float | Decimal,
    *,
    config: ZHLinkConfig | None = None,
) -> dict[str, Any]:
    """Async version of ``send_zhc``."""

    return await _send_zhc_async(private_key_wif, to_address, amount, config=config)


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


def _normalize_hex(value: str, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("0x"):
        normalized = normalized[2:]
    if not normalized:
        normalized = ""
    if any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError(f"{field} must be hex")
    if len(normalized) % 2:
        normalized = "0" + normalized
    return normalized


def _contract_execution(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    execution = result.get("executionResult") or {}
    return execution if isinstance(execution, dict) else {}


def _normalize_contract_call_result(result: Any) -> dict[str, Any]:
    execution = _contract_execution(result)
    return {
        "status": "ok",
        "output": execution.get("output") or "",
        "gas_used": execution.get("gasUsed"),
        "excepted": execution.get("excepted"),
        "excepted_message": execution.get("exceptedMessage"),
        "raw": result,
    }


def call_contract(
    contract_address: str,
    data_hex: str = "",
    *,
    from_address: str | None = None,
    gas: int = 1_000_000,
    config: ZHLinkConfig | None = None,
    allow_revert: bool = False,
    require_bool_success: bool = False,
) -> dict[str, Any]:
    """Run ZHCASH ``callcontract`` through configured public RPC endpoints.

    This is the recommended dry-run/read method before sending any contract
    transaction. ``data_hex`` is ABI calldata without a required ``0x`` prefix.
    """

    cfg = config or ZHLinkConfig()
    contract = _normalize_hex(contract_address, field="contract_address")
    data = _normalize_hex(data_hex, field="data_hex")
    caller = from_address or cfg.admin_address
    result = _rpc_call(cfg, "callcontract", [contract, data, caller, int(gas)])
    normalized = _normalize_contract_call_result(result)
    excepted = normalized.get("excepted")
    if excepted not in (None, "", "None", "none") and not allow_revert:
        raise RuntimeError(
            "contract call reverted: "
            f"{normalized.get('excepted_message') or normalized.get('output') or excepted}"
        )
    output = str(normalized.get("output") or "").lower()
    if require_bool_success and output and int(output[-64:] or "0", 16) == 0:
        raise RuntimeError("contract call returned false")
    return normalized


async def async_call_contract(
    contract_address: str,
    data_hex: str = "",
    *,
    from_address: str | None = None,
    gas: int = 1_000_000,
    config: ZHLinkConfig | None = None,
    allow_revert: bool = False,
    require_bool_success: bool = False,
) -> dict[str, Any]:
    """Async version of ``call_contract``."""

    return await asyncio.to_thread(
        call_contract,
        contract_address,
        data_hex,
        from_address=from_address,
        gas=gas,
        config=config,
        allow_revert=allow_revert,
        require_bool_success=require_bool_success,
    )


async def _send_to_contract_async(
    private_key_wif: str,
    contract_address: str,
    data_hex: str = "",
    *,
    amount: str | int | float | Decimal = "0",
    gas: int = 1_000_000,
    config: ZHLinkConfig | None = None,
    require_bool_success: bool = False,
    service_fee: str | int | float | Decimal | None = None,
    selection_buffer: str | int | float | Decimal | None = None,
) -> dict[str, Any]:
    from .rpc import ZHCashRPC

    cfg = config or ZHLinkConfig()
    from_address = _address_from_wif(private_key_wif)
    contract = _normalize_hex(contract_address, field="contract_address")
    data = _normalize_hex(data_hex, field="data_hex")
    client = ZHCashRPC(cfg)
    try:
        result = await client.send_to_contract(
            contract_address=contract,
            from_address=from_address,
            to_address=from_address,
            amount=float(Decimal(str(amount))),
            private_key=private_key_wif,
            hex_command=data,
            gas=int(gas),
            require_bool_success=require_bool_success,
            service_fee=Decimal(str(service_fee)) if service_fee is not None else None,
            selection_buffer=Decimal(str(selection_buffer)) if selection_buffer is not None else None,
        )
        result.setdefault("from_address", from_address)
        result.setdefault("contract_address", contract)
        result.setdefault("amount", str(amount))
        result.setdefault("data_hex", data)
        result.setdefault("gas", int(gas))
        return result
    finally:
        await client.close()


def send_to_contract(
    private_key_wif: str,
    contract_address: str,
    data_hex: str = "",
    *,
    amount: str | int | float | Decimal = "0",
    gas: int = 1_000_000,
    config: ZHLinkConfig | None = None,
    require_bool_success: bool = False,
    service_fee: str | int | float | Decimal | None = None,
    selection_buffer: str | int | float | Decimal | None = None,
) -> dict[str, Any]:
    """Send a payable ZHCASH contract call from a WIF private key.

    The sender address is derived locally. The function runs ``callcontract``
    preflight, selects UTXO, signs locally, checks mempool when RPC is
    available, broadcasts through ZeroScan, and falls back to RPC broadcast.
    """

    return _run(
        _send_to_contract_async(
            private_key_wif,
            contract_address,
            data_hex,
            amount=amount,
            gas=gas,
            config=config,
            require_bool_success=require_bool_success,
            service_fee=service_fee,
            selection_buffer=selection_buffer,
        )
    )


async def async_send_to_contract(
    private_key_wif: str,
    contract_address: str,
    data_hex: str = "",
    *,
    amount: str | int | float | Decimal = "0",
    gas: int = 1_000_000,
    config: ZHLinkConfig | None = None,
    require_bool_success: bool = False,
    service_fee: str | int | float | Decimal | None = None,
    selection_buffer: str | int | float | Decimal | None = None,
) -> dict[str, Any]:
    """Async version of ``send_to_contract``."""

    return await _send_to_contract_async(
        private_key_wif,
        contract_address,
        data_hex,
        amount=amount,
        gas=gas,
        config=config,
        require_bool_success=require_bool_success,
        service_fee=service_fee,
        selection_buffer=selection_buffer,
    )


async def _send_zrc20_token_async(
    private_key_wif: str,
    token_contract: str,
    to_address: str,
    amount: str | int | float | Decimal,
    *,
    gas: int = 1_000_000,
    config: ZHLinkConfig | None = None,
) -> dict[str, Any]:
    from .rpc import ZHCashRPC

    cfg = config or ZHLinkConfig()
    from_address = _address_from_wif(private_key_wif)
    contract = _normalize_hex(token_contract, field="token_contract")
    client = ZHCashRPC(cfg)
    try:
        result = await client.send_token(
            contract_address=contract,
            from_address=from_address,
            to_address=to_address,
            amount_or_id=float(Decimal(str(amount))),
            private_key=private_key_wif,
            gas=int(gas),
        )
        result.setdefault("from_address", from_address)
        result.setdefault("to_address", to_address)
        result.setdefault("token_contract", contract)
        result.setdefault("amount", str(amount))
        result.setdefault("gas", int(gas))
        return result
    finally:
        await client.close()


def send_zrc20_token(
    private_key_wif: str,
    token_contract: str,
    to_address: str,
    amount: str | int | float | Decimal,
    *,
    gas: int = 1_000_000,
    config: ZHLinkConfig | None = None,
) -> dict[str, Any]:
    """Send any ZRC-20 token by contract address.

    For native ZHC use ``send_zhc()``. For USDZ with admin-paid gas use
    ``send_usdz_free()``.
    """

    return _run(
        _send_zrc20_token_async(
            private_key_wif,
            token_contract,
            to_address,
            amount,
            gas=gas,
            config=config,
        )
    )


async def async_send_zrc20_token(
    private_key_wif: str,
    token_contract: str,
    to_address: str,
    amount: str | int | float | Decimal,
    *,
    gas: int = 1_000_000,
    config: ZHLinkConfig | None = None,
) -> dict[str, Any]:
    """Async version of ``send_zrc20_token``."""

    return await _send_zrc20_token_async(
        private_key_wif,
        token_contract,
        to_address,
        amount,
        gas=gas,
        config=config,
    )


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


async def async_admin_gas_wallet_info(
    admin_private_key_wif: str,
    *,
    config: ZHLinkConfig | None = None,
    store_path: str | Path = ".zhlink-gasfree-utxos.json",
) -> dict[str, Any]:
    """Async version of ``admin_gas_wallet_info``."""

    return await asyncio.to_thread(
        admin_gas_wallet_info,
        admin_private_key_wif,
        config=config,
        store_path=store_path,
    )


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


async def async_send_usdz_gas_free(
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
    """Async wrapper around ``send_usdz_gas_free``.

    The underlying raw transaction engine is synchronous and deterministic, so
    this wrapper runs it in a worker thread to avoid blocking the event loop.
    """

    return await asyncio.to_thread(
        send_usdz_gas_free,
        sender_private_key_wif,
        admin_private_key_wif,
        to_address,
        amount,
        config=config,
        store_path=store_path,
        broadcast=broadcast,
        max_attempts=max_attempts,
    )


def send_usdz_free(
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

    Short beginner-friendly alias for ``send_usdz_gas_free()``.
    """

    return send_usdz_gas_free(
        sender_private_key_wif,
        admin_private_key_wif,
        to_address,
        amount,
        config=config,
        store_path=store_path,
        broadcast=broadcast,
        max_attempts=max_attempts,
    )


async def async_send_usdz_free(
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
    """Async alias for ``send_usdz_free()``."""

    return await async_send_usdz_gas_free(
        sender_private_key_wif,
        admin_private_key_wif,
        to_address,
        amount,
        config=config,
        store_path=store_path,
        broadcast=broadcast,
        max_attempts=max_attempts,
    )


__all__ = [
    "Balance",
    "ZHLinkConfig",
    "admin_gas_wallet_info",
    "async_admin_gas_wallet_info",
    "async_balance",
    "async_call_contract",
    "async_force_refresh_balance",
    "async_get_balance",
    "async_new_wallet",
    "async_send_to_contract",
    "async_send_usdz_gas_free",
    "async_send_usdz_free",
    "async_send_zhc",
    "async_send_zrc20_token",
    "balance",
    "call_contract",
    "create_address",
    "force_refresh_balance",
    "get_cached_balance",
    "get_balance",
    "new_wallet",
    "send_to_contract",
    "send_zhc",
    "send_usdz_gas_free",
    "send_usdz_free",
    "send_zrc20_token",
    "watch_balance",
]

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal

from .api import async_send_usdz_gas_free, create_address, watch_balance
from .config import ZHLinkConfig


ReceiverAction = Literal["status", "new", "delete", "serve"]
ReceiverEventCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class UsdzReceiverConfig:
    admin_address: str = "ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi"
    admin_gas_wif: str = ""
    min_usdz: Decimal = Decimal("0.00000001")
    send_real_tx: bool = True
    delete_after_forward: bool = False
    db_path: Path = Path(".zhlink-usdz-receiver.sqlite3")
    gasfree_store_path: Path = Path(".zhlink-gasfree-utxos.json")
    address_subscription_ttl_seconds: float = 12 * 60 * 60
    ws_max_failures: int = 5
    ws_cooldown_seconds: float = 120.0
    wait_timeout_seconds: float = 3600
    debug: bool = False
    event_callback: ReceiverEventCallback | None = None


def _emit(config: UsdzReceiverConfig, event: str, **payload: Any) -> None:
    message = {"event": event, **payload}
    if config.event_callback:
        config.event_callback(message)
    elif config.debug:
        print(message)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS receiver_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL UNIQUE,
                private_key_wif TEXT NOT NULL,
                status TEXT NOT NULL,
                last_usdz TEXT NOT NULL DEFAULT '0',
                forward_txid TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()


def _now() -> float:
    return time.time()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _active_rows(db_path: Path) -> list[sqlite3.Row]:
    with closing(_connect(db_path)) as conn:
        return list(
            conn.execute(
                "SELECT * FROM receiver_addresses WHERE status = 'active' ORDER BY id ASC"
            ).fetchall()
        )


def _get_row(db_path: Path, address: str) -> sqlite3.Row:
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM receiver_addresses WHERE address = ?",
            (address,),
        ).fetchone()
    if row is None:
        raise ValueError(f"Receiver address is not stored locally: {address}")
    return row


def _update_address(db_path: Path, address: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [address]
    with closing(_connect(db_path)) as conn:
        conn.execute(f"UPDATE receiver_addresses SET {assignments} WHERE address = ?", values)
        conn.commit()


def usdz_receiver_status(config: UsdzReceiverConfig | None = None) -> dict[str, Any]:
    cfg = config or UsdzReceiverConfig()
    _init_db(cfg.db_path)
    rows = [_row_to_dict(row) for row in _active_rows(cfg.db_path)]
    result = {
        "db_path": str(cfg.db_path),
        "active_receiver_count": len(rows),
        "active_receivers": rows,
    }
    _emit(cfg, "receiver_status", db_path=result["db_path"], active_receiver_count=len(rows))
    return result


def create_usdz_receiver_address(config: UsdzReceiverConfig | None = None) -> dict[str, Any]:
    cfg = config or UsdzReceiverConfig()
    _init_db(cfg.db_path)
    wallet = create_address()
    ts = _now()
    with closing(_connect(cfg.db_path)) as conn:
        conn.execute(
            """
            INSERT INTO receiver_addresses(address, private_key_wif, status, created_at, updated_at)
            VALUES (?, ?, 'active', ?, ?)
            """,
            (wallet.address, wallet.priv_key, ts, ts),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM receiver_addresses WHERE address = ?",
            (wallet.address,),
        ).fetchone()
    result = _row_to_dict(row)
    _emit(cfg, "receiver_created", address=result["address"], db_path=str(cfg.db_path))
    return result


def delete_usdz_receiver_address(address: str, config: UsdzReceiverConfig | None = None) -> bool:
    cfg = config or UsdzReceiverConfig()
    _init_db(cfg.db_path)
    with closing(_connect(cfg.db_path)) as conn:
        cursor = conn.execute("DELETE FROM receiver_addresses WHERE address = ?", (address,))
        conn.commit()
        deleted = cursor.rowcount > 0
    _emit(cfg, "receiver_deleted" if deleted else "receiver_not_found", address=address)
    return deleted


async def _forward_deposit(row: sqlite3.Row, usdz: Decimal, config: UsdzReceiverConfig) -> dict[str, Any]:
    address = row["address"]
    _update_address(config.db_path, address, status="forwarding", last_usdz=str(usdz), error=None)
    _emit(config, "receiver_forward_start", address=address, amount=str(usdz), broadcast=config.send_real_tx)
    try:
        result = await async_send_usdz_gas_free(
            sender_private_key_wif=row["private_key_wif"],
            admin_private_key_wif=config.admin_gas_wif,
            to_address=config.admin_address,
            amount=str(usdz),
            config=ZHLinkConfig.public_network(),
            broadcast=config.send_real_tx,
            store_path=config.gasfree_store_path,
        )
        txid = (
            result.get("txid")
            or result.get("broadcast", {}).get("txid")
            or result.get("built", {}).get("txid")
            or ""
        )
        _update_address(
            config.db_path,
            address,
            status="forwarded" if config.send_real_tx else "preview",
            last_usdz=str(usdz),
            forward_txid=str(txid),
            error=None,
        )
        if config.send_real_tx and config.delete_after_forward:
            delete_usdz_receiver_address(address, config)
        response = {
            "status": "ok",
            "address": address,
            "amount": str(usdz),
            "txid": str(txid),
            "result": result,
        }
        _emit(config, "receiver_forward_ok", address=address, amount=str(usdz), txid=str(txid))
        return response
    except Exception as exc:
        _update_address(config.db_path, address, status="error", last_usdz=str(usdz), error=str(exc))
        response = {"status": "error", "address": address, "amount": str(usdz), "error": str(exc)}
        _emit(config, "receiver_forward_error", address=address, amount=str(usdz), error=str(exc))
        return response


async def _watch_receiver(row: sqlite3.Row, config: UsdzReceiverConfig) -> None:
    address = row["address"]
    zhc_config = ZHLinkConfig.public_network(
        address_subscription_ttl_seconds=config.address_subscription_ttl_seconds,
        ws_max_failures=config.ws_max_failures,
        ws_cooldown_seconds=config.ws_cooldown_seconds,
    )
    mempool_seen = False
    _emit(config, "receiver_watch_start", address=address)
    async for balance in watch_balance(address, config=zhc_config):
        if balance.get("status") == "error":
            _emit(config, "receiver_watch_error", address=address, reason=balance.get("reason"))
            continue
        event = balance.get("realtime_event") or {}
        event_payload = event.get("payload") if isinstance(event, dict) else {}
        source = balance.get("realtime_source") or (
            event_payload.get("source") if isinstance(event_payload, dict) else None
        )
        txid = event_payload.get("txid") if isinstance(event_payload, dict) else ""
        if source == "mempool" and not mempool_seen:
            mempool_seen = True
            _update_address(config.db_path, address, status="paid_mempool", error=None)
            _emit(config, "receiver_payment_mempool", address=address, txid=txid or "")
        usdz = Decimal(str(balance.get("usdz", "0")))
        _update_address(config.db_path, address, last_usdz=str(usdz))
        _emit(config, "receiver_balance", address=address, usdz=str(usdz))
        if usdz >= config.min_usdz:
            _emit(config, "receiver_payment_accepted", address=address, usdz=str(usdz))
            result = await _forward_deposit(row, usdz, config)
            _emit(config, "receiver_complete", address=address, result=result)
            return


async def _wait_for_usdz(address: str, config: UsdzReceiverConfig) -> Decimal:
    zhc_config = ZHLinkConfig.public_network(
        address_subscription_ttl_seconds=config.address_subscription_ttl_seconds,
        ws_max_failures=config.ws_max_failures,
        ws_cooldown_seconds=config.ws_cooldown_seconds,
    )
    started = time.monotonic()
    _emit(config, "receiver_wait_start", address=address, min_usdz=str(config.min_usdz))
    async for balance in watch_balance(address, config=zhc_config):
        if time.monotonic() - started >= config.wait_timeout_seconds:
            _emit(config, "receiver_timeout", address=address, timeout_seconds=config.wait_timeout_seconds)
            raise TimeoutError(f"USDZ deposit was not detected in {config.wait_timeout_seconds} seconds")
        if balance.get("status") == "error":
            _emit(config, "receiver_watch_error", address=address, reason=balance.get("reason"))
            continue
        event = balance.get("realtime_event") or {}
        event_payload = event.get("payload") if isinstance(event, dict) else {}
        source = balance.get("realtime_source") or (
            event_payload.get("source") if isinstance(event_payload, dict) else None
        )
        txid = event_payload.get("txid") if isinstance(event_payload, dict) else ""
        if source == "mempool":
            _update_address(config.db_path, address, status="paid_mempool", error=None)
            _emit(config, "receiver_payment_mempool", address=address, txid=txid or "")
        usdz = Decimal(str(balance.get("usdz", "0")))
        _update_address(config.db_path, address, last_usdz=str(usdz))
        _emit(config, "receiver_balance", address=address, usdz=str(usdz))
        if usdz >= config.min_usdz:
            _update_address(config.db_path, address, status="paid_confirmed", last_usdz=str(usdz), error=None)
            _emit(config, "receiver_payment_accepted", address=address, usdz=str(usdz))
            return usdz


async def async_wait_for_usdz_deposit(address: str, config: UsdzReceiverConfig | None = None) -> Decimal:
    """Wait until a receiver address has at least ``config.min_usdz`` USDZ."""

    cfg = config or UsdzReceiverConfig()
    _init_db(cfg.db_path)
    _get_row(cfg.db_path, address)
    return await _wait_for_usdz(address, cfg)


def wait_for_usdz_deposit(address: str, config: UsdzReceiverConfig | None = None) -> Decimal:
    """Synchronous wrapper around :func:`async_wait_for_usdz_deposit`."""

    return asyncio.run(async_wait_for_usdz_deposit(address, config))


async def async_forward_usdz_deposit(
    address: str,
    amount: Decimal | str | None = None,
    config: UsdzReceiverConfig | None = None,
) -> dict[str, Any]:
    """Forward USDZ from a stored receiver address to the configured admin address."""

    cfg = config or UsdzReceiverConfig()
    if cfg.admin_gas_wif == "":
        raise RuntimeError("Set admin_gas_wif in UsdzReceiverConfig.")
    _init_db(cfg.db_path)
    row = _get_row(cfg.db_path, address)
    usdz = Decimal(str(amount if amount is not None else row["last_usdz"]))
    if usdz < cfg.min_usdz:
        raise ValueError(f"Receiver balance {usdz} USDZ is below min_usdz {cfg.min_usdz}")
    return await _forward_deposit(row, usdz, cfg)


def forward_usdz_deposit(
    address: str,
    amount: Decimal | str | None = None,
    config: UsdzReceiverConfig | None = None,
) -> dict[str, Any]:
    """Synchronous wrapper around :func:`async_forward_usdz_deposit`."""

    return asyncio.run(async_forward_usdz_deposit(address, amount, config))


async def async_create_and_forward_usdz_deposit(config: UsdzReceiverConfig | None = None) -> dict[str, Any]:
    """Create one receiver address, wait for USDZ, then forward it gas-free."""

    config = config or UsdzReceiverConfig()
    if config.admin_gas_wif == "":
        raise RuntimeError("Set admin_gas_wif in UsdzReceiverConfig.")
    _init_db(config.db_path)
    row_dict = create_usdz_receiver_address(config)
    address = row_dict["address"]
    _emit(config, "receiver_deposit_request", address=address, min_usdz=str(config.min_usdz))

    amount = await _wait_for_usdz(address, config)
    forward = await async_forward_usdz_deposit(address, amount, config)
    return {"receiver": row_dict, "amount": str(amount), "forward": forward}


def create_and_forward_usdz_deposit(config: UsdzReceiverConfig | None = None) -> dict[str, Any]:
    """Create one receiver address, wait for USDZ, then forward it gas-free."""

    return asyncio.run(async_create_and_forward_usdz_deposit(config))


async def _service_loop(config: UsdzReceiverConfig) -> None:
    if config.admin_gas_wif == "":
        raise RuntimeError("Set admin_gas_wif in UsdzReceiverConfig.")
    _init_db(config.db_path)
    tasks: dict[str, asyncio.Task] = {}
    _emit(config, "receiver_service_start", db_path=str(config.db_path))
    while True:
        for row in _active_rows(config.db_path):
            address = row["address"]
            if address not in tasks or tasks[address].done():
                tasks[address] = asyncio.create_task(_watch_receiver(row, config))

        done_addresses = [address for address, task in tasks.items() if task.done()]
        for address in done_addresses:
            task = tasks.pop(address)
            if task.exception():
                _emit(config, "receiver_watch_task_failed", address=address, error=str(task.exception()))

        await asyncio.sleep(5)


def run_usdz_receiver(
    *,
    action: ReceiverAction = "status",
    config: UsdzReceiverConfig | None = None,
    delete_address: str = "",
) -> dict[str, Any] | None:
    cfg = config or UsdzReceiverConfig()
    if action == "status":
        result = usdz_receiver_status(cfg)
        if not cfg.event_callback:
            print("receiver database:", result["db_path"])
            print("active receiver count:", result["active_receiver_count"])
        return result
    if action == "new":
        result = create_usdz_receiver_address(cfg)
        if not cfg.event_callback:
            print("created receiver:", result["address"])
        return result
    if action == "delete":
        if not delete_address:
            _emit(cfg, "receiver_delete_error", reason="missing delete_address")
            if not cfg.event_callback:
                print("Set delete_address.")
            return {"deleted": False, "reason": "missing delete_address"}
        deleted = delete_usdz_receiver_address(delete_address, cfg)
        if not cfg.event_callback:
            print("deleted:" if deleted else "not found:", delete_address)
        return {"deleted": deleted, "address": delete_address}
    if action == "serve":
        asyncio.run(_service_loop(cfg))
        return None
    raise ValueError(f"Unsupported receiver action: {action}")

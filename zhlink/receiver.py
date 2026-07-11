from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from .api import async_send_usdz_gas_free, create_address, watch_balance
from .config import ZHLinkConfig


ReceiverAction = Literal["status", "new", "delete", "serve"]


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
    return {
        "db_path": str(cfg.db_path),
        "active_receiver_count": len(rows),
        "active_receivers": rows,
    }


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
    return _row_to_dict(row)


def delete_usdz_receiver_address(address: str, config: UsdzReceiverConfig | None = None) -> bool:
    cfg = config or UsdzReceiverConfig()
    _init_db(cfg.db_path)
    with closing(_connect(cfg.db_path)) as conn:
        cursor = conn.execute("DELETE FROM receiver_addresses WHERE address = ?", (address,))
        conn.commit()
        return cursor.rowcount > 0


async def _forward_deposit(row: sqlite3.Row, usdz: Decimal, config: UsdzReceiverConfig) -> dict[str, Any]:
    address = row["address"]
    _update_address(config.db_path, address, status="forwarding", last_usdz=str(usdz), error=None)
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
        return {"status": "ok", "address": address, "amount": str(usdz), "txid": str(txid), "result": result}
    except Exception as exc:
        _update_address(config.db_path, address, status="error", last_usdz=str(usdz), error=str(exc))
        return {"status": "error", "address": address, "amount": str(usdz), "error": str(exc)}


async def _watch_receiver(row: sqlite3.Row, config: UsdzReceiverConfig) -> None:
    address = row["address"]
    zhc_config = ZHLinkConfig.public_network(
        address_subscription_ttl_seconds=config.address_subscription_ttl_seconds,
        ws_max_failures=config.ws_max_failures,
        ws_cooldown_seconds=config.ws_cooldown_seconds,
    )
    mempool_seen = False
    print("watching:", address)
    async for balance in watch_balance(address, config=zhc_config):
        if balance.get("status") == "error":
            print("watch error:", address, balance.get("reason"))
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
            print("payment in mempool:", address, txid or "")
        usdz = Decimal(str(balance.get("usdz", "0")))
        _update_address(config.db_path, address, last_usdz=str(usdz))
        print("receiver balance:", address, usdz, "USDZ")
        if usdz >= config.min_usdz:
            print("payment accepted:", address, usdz, "USDZ")
            result = await _forward_deposit(row, usdz, config)
            print("forward result:", address, result)
            return


async def _wait_for_usdz(address: str, config: UsdzReceiverConfig) -> Decimal:
    zhc_config = ZHLinkConfig.public_network(
        address_subscription_ttl_seconds=config.address_subscription_ttl_seconds,
        ws_max_failures=config.ws_max_failures,
        ws_cooldown_seconds=config.ws_cooldown_seconds,
    )
    started = time.monotonic()
    async for balance in watch_balance(address, config=zhc_config):
        if time.monotonic() - started >= config.wait_timeout_seconds:
            raise TimeoutError(f"USDZ deposit was not detected in {config.wait_timeout_seconds} seconds")
        if balance.get("status") == "error":
            print("watch error:", address, balance.get("reason"))
            continue
        usdz = Decimal(str(balance.get("usdz", "0")))
        print("receiver balance:", address, usdz, "USDZ")
        if usdz >= config.min_usdz:
            print("payment accepted:", address, usdz, "USDZ")
            return usdz


async def _create_and_forward_usdz_deposit_async(config: UsdzReceiverConfig) -> dict[str, Any]:
    if config.admin_gas_wif == "":
        raise RuntimeError("Set admin_gas_wif in UsdzReceiverConfig.")
    _init_db(config.db_path)
    row_dict = create_usdz_receiver_address(config)
    address = row_dict["address"]
    print("new USDZ deposit address:", address)
    print("send at least", config.min_usdz, "USDZ to this address")

    with closing(_connect(config.db_path)) as conn:
        row = conn.execute("SELECT * FROM receiver_addresses WHERE address = ?", (address,)).fetchone()
    amount = await _wait_for_usdz(address, config)
    forward = await _forward_deposit(row, amount, config)
    return {"receiver": row_dict, "amount": str(amount), "forward": forward}


def create_and_forward_usdz_deposit(config: UsdzReceiverConfig | None = None) -> dict[str, Any]:
    """Create one receiver address, wait for USDZ, then forward it gas-free."""

    return asyncio.run(_create_and_forward_usdz_deposit_async(config or UsdzReceiverConfig()))


async def _service_loop(config: UsdzReceiverConfig) -> None:
    if config.admin_gas_wif == "":
        raise RuntimeError("Set admin_gas_wif in UsdzReceiverConfig.")
    _init_db(config.db_path)
    tasks: dict[str, asyncio.Task] = {}
    while True:
        for row in _active_rows(config.db_path):
            address = row["address"]
            if address not in tasks or tasks[address].done():
                tasks[address] = asyncio.create_task(_watch_receiver(row, config))

        done_addresses = [address for address, task in tasks.items() if task.done()]
        for address in done_addresses:
            task = tasks.pop(address)
            if task.exception():
                print("watch task failed:", address, task.exception())

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
        print("receiver database:", result["db_path"])
        print("active receiver count:", result["active_receiver_count"])
        return result
    if action == "new":
        result = create_usdz_receiver_address(cfg)
        print("created receiver:", result["address"])
        return result
    if action == "delete":
        if not delete_address:
            print("Set delete_address.")
            return {"deleted": False, "reason": "missing delete_address"}
        deleted = delete_usdz_receiver_address(delete_address, cfg)
        print("deleted:" if deleted else "not found:", delete_address)
        return {"deleted": deleted, "address": delete_address}
    if action == "serve":
        asyncio.run(_service_loop(cfg))
        return None
    raise ValueError(f"Unsupported receiver action: {action}")

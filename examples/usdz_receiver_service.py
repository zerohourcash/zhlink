import asyncio
import sqlite3
import sys
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from zhlink import ZHLinkConfig, async_send_usdz_gas_free, create_address, watch_balance


# Production switch:
#   True  - build, preflight, and broadcast real gas-free USDZ transactions.
#   False - build and dry-run/preflight only, without broadcasting.
FLAG_SEND_REAL_TX = True

LIBRARY_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = LIBRARY_ROOT / ".zhlink-usdz-receiver.sqlite3"
ADMIN_ADDRESS = "ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi"
ADMIN_GAS_WIF = "K..."
MIN_USDZ = Decimal("0.00000001")
DELETE_AFTER_FORWARD = False
GASFREE_STORE_PATH = LIBRARY_ROOT / ".zhlink-gasfree-utxos.json"
ADDRESS_SUBSCRIPTION_TTL_SECONDS = 12 * 60 * 60
WS_MAX_FAILURES = 5
WS_COOLDOWN_SECONDS = 120.0


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(connect()) as conn:
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


def now() -> float:
    return time.time()


def create_receiver_address() -> sqlite3.Row:
    wallet = create_address()
    ts = now()
    with closing(connect()) as conn:
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
    print("created receiver:", wallet.address)
    return row


def active_rows() -> list[sqlite3.Row]:
    with closing(connect()) as conn:
        return list(
            conn.execute(
                "SELECT * FROM receiver_addresses WHERE status = 'active' ORDER BY id ASC"
            ).fetchall()
        )


def update_address(address: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [address]
    with closing(connect()) as conn:
        conn.execute(
            f"UPDATE receiver_addresses SET {assignments} WHERE address = ?",
            values,
        )
        conn.commit()


def delete_receiver_address(address: str) -> bool:
    with closing(connect()) as conn:
        cursor = conn.execute(
            "DELETE FROM receiver_addresses WHERE address = ?",
            (address,),
        )
        conn.commit()
        return cursor.rowcount > 0


async def forward_deposit(row: sqlite3.Row, usdz: Decimal, config: ZHLinkConfig) -> None:
    address = row["address"]
    update_address(address, status="forwarding", last_usdz=str(usdz), error=None)
    print("forwarding:", usdz, "USDZ from", address, "to", ADMIN_ADDRESS)
    try:
        result = await async_send_usdz_gas_free(
            sender_private_key_wif=row["private_key_wif"],
            admin_private_key_wif=ADMIN_GAS_WIF,
            to_address=ADMIN_ADDRESS,
            amount=str(usdz),
            config=config,
            broadcast=FLAG_SEND_REAL_TX,
            store_path=GASFREE_STORE_PATH,
        )
        txid = (
            result.get("txid")
            or result.get("broadcast", {}).get("txid")
            or result.get("built", {}).get("txid")
            or ""
        )
        update_address(
            address,
            status="forwarded" if FLAG_SEND_REAL_TX else "preview",
            last_usdz=str(usdz),
            forward_txid=str(txid),
            error=None,
        )
        if FLAG_SEND_REAL_TX and DELETE_AFTER_FORWARD:
            delete_receiver_address(address)
            print("receiver deleted after forward:", address)
        print("forward result:", address, result)
    except Exception as exc:
        update_address(address, status="error", last_usdz=str(usdz), error=str(exc))
        print("forward failed:", address, exc)


async def watch_receiver(row: sqlite3.Row, config: ZHLinkConfig) -> None:
    address = row["address"]
    print("watching:", address)
    mempool_seen = False
    async for balance in watch_balance(address, config=config):
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
            update_address(address, status="paid_mempool", error=None)
            print("payment in mempool:", address, txid or "")
        usdz = Decimal(str(balance.get("usdz", "0")))
        update_address(address, last_usdz=str(usdz))
        print("receiver balance:", address, usdz, "USDZ")
        if usdz >= MIN_USDZ:
            print("payment accepted:", address, usdz, "USDZ")
            await forward_deposit(row, usdz, config)
            return


async def service_loop() -> None:
    if ADMIN_GAS_WIF == "K...":
        raise SystemExit("Edit ADMIN_GAS_WIF at the top of this file.")

    init_db()
    config = ZHLinkConfig.public_network(
        address_subscription_ttl_seconds=ADDRESS_SUBSCRIPTION_TTL_SECONDS,
        ws_max_failures=WS_MAX_FAILURES,
        ws_cooldown_seconds=WS_COOLDOWN_SECONDS,
    )

    tasks: dict[str, asyncio.Task] = {}
    while True:
        for row in active_rows():
            address = row["address"]
            if address not in tasks or tasks[address].done():
                tasks[address] = asyncio.create_task(watch_receiver(row, config))

        done_addresses = [address for address, task in tasks.items() if task.done()]
        for address in done_addresses:
            task = tasks.pop(address)
            if task.exception():
                print("watch task failed:", address, task.exception())

        await asyncio.sleep(5)


def print_usage() -> None:
    print("Usage:")
    print("  python examples/usdz_receiver_service.py new")
    print("  python examples/usdz_receiver_service.py delete Z...")
    print("  python examples/usdz_receiver_service.py serve")
    print("")
    print("Edit constants at the top of this file before production use.")
    print("'new' creates exactly one receiver address on explicit request.")
    print("'delete' removes a receiver address and its private key from the local SQLite state.")
    print("'serve' watches already-created active addresses and forwards deposits.")
    print("Set FLAG_SEND_REAL_TX=False only for a local dry-run preview.")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "new":
        init_db()
        row = create_receiver_address()
        print("address:", row["address"])
    elif command == "delete":
        init_db()
        if len(sys.argv) < 3:
            raise SystemExit("Usage: python examples/usdz_receiver_service.py delete Z...")
        deleted = delete_receiver_address(sys.argv[2])
        print("deleted:" if deleted else "not found:", sys.argv[2])
    elif command == "serve":
        asyncio.run(service_loop())
    else:
        print_usage()

import asyncio
import os
import sqlite3
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from zhlink import ZHLinkConfig, async_send_usdz_gas_free, create_address, watch_balance


DB_PATH = Path(os.environ.get("ZHLINK_RECEIVER_DB", ".zhlink-usdz-receiver.sqlite3"))
ADMIN_ADDRESS = os.environ.get("ZHLINK_ADMIN_ADDRESS", "ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi")
ADMIN_GAS_WIF = os.environ.get("ZHLINK_ADMIN_GAS_WIF", "")
MIN_USDZ = Decimal(os.environ.get("ZHLINK_MIN_USDZ", "0.00000001"))
ACTIVE_LIMIT = int(os.environ.get("ZHLINK_RECEIVER_ACTIVE", "5"))
MODE = os.environ.get("ZHLINK_RECEIVER_MODE", "pool").lower()
RUN_SERVICE = os.environ.get("RUN_USDZ_RECEIVER") == "1"
RUN_REAL_SEND = os.environ.get("RUN_REAL_SEND") == "1"


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


def ensure_active_pool() -> None:
    target = 1 if MODE == "sequential" else max(1, ACTIVE_LIMIT)
    active = active_rows()
    while len(active) < target:
        active.append(create_receiver_address())


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
            broadcast=RUN_REAL_SEND,
            store_path=os.environ.get("ZHLINK_GASFREE_STORE", ".zhlink-gasfree-utxos.json"),
        )
        txid = (
            result.get("txid")
            or result.get("broadcast", {}).get("txid")
            or result.get("built", {}).get("txid")
            or ""
        )
        update_address(
            address,
            status="forwarded" if RUN_REAL_SEND else "preview",
            last_usdz=str(usdz),
            forward_txid=str(txid),
            error=None,
        )
        print("forward result:", address, result)
    except Exception as exc:
        update_address(address, status="error", last_usdz=str(usdz), error=str(exc))
        print("forward failed:", address, exc)


async def watch_receiver(row: sqlite3.Row, config: ZHLinkConfig) -> None:
    address = row["address"]
    print("watching:", address)
    async for balance in watch_balance(address, config=config):
        if balance.get("status") == "error":
            print("watch error:", address, balance.get("reason"))
            continue
        usdz = Decimal(str(balance.get("usdz", "0")))
        update_address(address, last_usdz=str(usdz))
        print("receiver balance:", address, usdz, "USDZ")
        if usdz >= MIN_USDZ:
            await forward_deposit(row, usdz, config)
            return


async def service_loop() -> None:
    if not RUN_SERVICE:
        print("Refusing to send or run receiver. Set RUN_USDZ_RECEIVER=1 intentionally.")
        print("For live forwarding also set RUN_REAL_SEND=1 and ZHLINK_ADMIN_GAS_WIF.")
        return
    if not ADMIN_GAS_WIF:
        raise SystemExit("Set ZHLINK_ADMIN_GAS_WIF.")

    init_db()
    config = ZHLinkConfig.public_network(
        address_subscription_ttl_seconds=float(os.environ.get("ZHLINK_RECEIVER_TTL", str(12 * 60 * 60))),
        ws_max_failures=int(os.environ.get("ZHLINK_WS_MAX_FAILURES", "5")),
        ws_cooldown_seconds=float(os.environ.get("ZHLINK_WS_COOLDOWN", "120")),
    )

    tasks: dict[str, asyncio.Task] = {}
    while True:
        ensure_active_pool()
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


if __name__ == "__main__":
    asyncio.run(service_loop())

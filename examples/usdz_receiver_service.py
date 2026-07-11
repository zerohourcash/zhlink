import asyncio
from decimal import Decimal
from pathlib import Path
from pprint import pprint

from zhlink import (
    UsdzReceiverConfig,
    async_forward_usdz_deposit,
    async_wait_for_usdz_deposit,
    create_usdz_receiver_address,
    delete_usdz_receiver_address,
    usdz_receiver_status,
)


# Production example: run this file as-is after editing the constants below.
FLAG_SEND_REAL_TX = True
DEBUG_EVENTS = True

# New receiver addresses are created only when your app has a real new request.
CREATE_NEW_RECEIVER_COUNT = 1

# Existing active receiver addresses are loaded from SQLite and watched in parallel.
WATCH_EXISTING_ACTIVE_RECEIVERS = True
MAX_PARALLEL_RECEIVERS = 20


def print_event(event: dict) -> None:
    """Readable debug stream for WSS, mempool, confirmations and forward steps."""

    if DEBUG_EVENTS:
        pprint(event)


CONFIG = UsdzReceiverConfig(
    admin_address="ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi",
    admin_gas_wif="K...",
    min_usdz=Decimal("0.00000001"),
    send_real_tx=FLAG_SEND_REAL_TX,
    delete_after_forward=False,
    db_path=Path(__file__).resolve().parents[1] / ".zhlink-usdz-receiver.sqlite3",
    gasfree_store_path=Path(__file__).resolve().parents[1] / ".zhlink-gasfree-utxos.json",
    debug=DEBUG_EVENTS,
    event_callback=print_event,
)


def create_new_receivers(count: int) -> list[dict]:
    """Create receiver addresses for real deposit requests."""

    receivers = []
    for _ in range(count):
        receiver = create_usdz_receiver_address(CONFIG)
        receivers.append(receiver)
        print("Deposit address:", receiver["address"])
        print("Minimum USDZ:", CONFIG.min_usdz)
    return receivers


def active_receivers() -> list[dict]:
    """Load active receivers from the local SQLite state."""

    status = usdz_receiver_status(CONFIG)
    return list(status["active_receivers"])


async def watch_and_forward(receiver: dict, semaphore: asyncio.Semaphore) -> dict:
    """Watch one receiver asynchronously and forward USDZ after confirmation."""

    async with semaphore:
        address = receiver["address"]
        amount = await async_wait_for_usdz_deposit(address, CONFIG)
        forward = await async_forward_usdz_deposit(address, amount, CONFIG)
        result = {"receiver": address, "amount": str(amount), "forward": forward}
        print("USDZ deposit forwarded.")
        pprint(result)

        # Uncomment only if your application should forget used receiver addresses.
        # delete_usdz_receiver_address(address, CONFIG)

        return result


async def main_async() -> None:
    if CONFIG.admin_gas_wif == "K...":
        print("Edit admin_gas_wif in CONFIG before forwarding real USDZ.")
        return

    created = create_new_receivers(CREATE_NEW_RECEIVER_COUNT)
    receivers = created
    if WATCH_EXISTING_ACTIVE_RECEIVERS:
        known = {receiver["address"]: receiver for receiver in active_receivers()}
        receivers = list(known.values())

    if not receivers:
        print("No active receiver addresses to watch.")
        return

    semaphore = asyncio.Semaphore(MAX_PARALLEL_RECEIVERS)
    tasks = [asyncio.create_task(watch_and_forward(receiver, semaphore)) for receiver in receivers]
    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

from decimal import Decimal
from pathlib import Path
from pprint import pprint

from zhlink import (
    UsdzReceiverConfig,
    create_usdz_receiver_address,
    delete_usdz_receiver_address,
    forward_usdz_deposit,
    wait_for_usdz_deposit,
)


# Edit these constants before running the script.
FLAG_SEND_REAL_TX = True
DEBUG_EVENTS = True


def print_event(event: dict) -> None:
    """Readable debug stream for mempool, confirmations, fallback and forward steps."""

    if not DEBUG_EVENTS:
        return
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


def create_new_receiver() -> dict:
    """Create exactly one fresh deposit address on explicit request."""

    receiver = create_usdz_receiver_address(CONFIG)
    print("Deposit address:", receiver["address"])
    print("Minimum USDZ:", CONFIG.min_usdz)
    return receiver


def main() -> None:
    if CONFIG.admin_gas_wif == "K...":
        print("Edit admin_gas_wif in CONFIG before forwarding real USDZ.")
        return

    receiver = create_new_receiver()
    address = receiver["address"]

    amount = wait_for_usdz_deposit(address, CONFIG)
    forward = forward_usdz_deposit(address, amount, CONFIG)

    print("USDZ deposit forwarded.")
    pprint({"receiver": address, "amount": str(amount), "forward": forward})

    # Uncomment only if your application should forget used receiver addresses.
    # delete_usdz_receiver_address(address, CONFIG)


if __name__ == "__main__":
    main()

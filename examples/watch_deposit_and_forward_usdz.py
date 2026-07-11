import asyncio
import contextlib
import os
import time
from decimal import Decimal

from zhlink import ZHLinkConfig, async_send_usdz_gas_free, create_address
from zhlink.rpc import ZHCashRPC


ADMIN_ADDRESS = os.environ.get("ZHLINK_ADMIN_ADDRESS", "ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi")
ADMIN_GAS_WIF = os.environ.get("ZHLINK_ADMIN_GAS_WIF", "")
MIN_USDZ = Decimal(os.environ.get("ZHLINK_MIN_USDZ", "0.00000001"))
TIMEOUT_SECONDS = int(os.environ.get("ZHLINK_WAIT_SECONDS", "3600"))
RUN_REAL_SEND = os.environ.get("RUN_REAL_SEND") == "1"


async def wait_for_usdz(address: str, config: ZHLinkConfig) -> Decimal:
    client = ZHCashRPC(config)
    updated = asyncio.Event()

    def on_balance(_address, snapshot):
        print("balance update:", snapshot.get("usdz"), "USDZ", "height", snapshot.get("height"))
        updated.set()

    client.subscribe_balance(address, on_balance)
    await client.start_block_watch()
    started = time.monotonic()
    try:
        while time.monotonic() - started < TIMEOUT_SECONDS:
            balance = await client.getbalance(address, force_refresh=True)
            raw = await client.get_zrc20_balance_raw(config.usdz_contract, address)
            usdz = Decimal(raw) / Decimal(100_000_000)
            print("current:", usdz, "USDZ", "zhc", balance.get("balance"))
            if usdz >= MIN_USDZ:
                return usdz
            updated.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(updated.wait(), timeout=60)
        raise TimeoutError(f"USDZ deposit was not detected in {TIMEOUT_SECONDS} seconds")
    finally:
        await client.close()


async def main() -> None:
    if os.environ.get("RUN_WATCH_EXAMPLE") != "1":
        print("Refusing to send or wait for deposit. Set RUN_WATCH_EXAMPLE=1 intentionally.")
        print(
            "For live forwarding also set RUN_REAL_SEND=1, "
            "ZHLINK_ADMIN_GAS_WIF, and optional ZHLINK_ADMIN_ADDRESS."
        )
        return

    wallet = create_address()
    config = ZHLinkConfig.public_network(
        address_subscription_ttl_seconds=12 * 60 * 60,
    )

    print("New USDZ deposit address")
    print("address:", wallet.address)
    print("private_key_wif:", wallet.priv_key)
    print("send at least", MIN_USDZ, "USDZ to this address")

    amount = await wait_for_usdz(wallet.address, config)
    print("deposit detected:", amount, "USDZ")

    if not ADMIN_GAS_WIF:
        raise SystemExit("Set ZHLINK_ADMIN_GAS_WIF to forward USDZ gas-free.")

    if not RUN_REAL_SEND:
        print("RUN_REAL_SEND is not 1, building transaction only.")

    result = await async_send_usdz_gas_free(
        sender_private_key_wif=wallet.priv_key,
        admin_private_key_wif=ADMIN_GAS_WIF,
        to_address=ADMIN_ADDRESS,
        amount=str(amount),
        config=config,
        broadcast=RUN_REAL_SEND,
        store_path=os.environ.get("ZHLINK_GASFREE_STORE", ".zhlink-gasfree-utxos.json"),
    )

    print("forward result:")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import contextlib
import time
from decimal import Decimal
from pathlib import Path

from zhlink import ZHLinkConfig, async_send_usdz_gas_free, create_address
from zhlink.rpc import ZHCashRPC


# Production switch:
#   True  - after deposit confirmation, broadcast the real gas-free USDZ forward.
#   False - wait for deposit and build/preflight only, without broadcasting.
FLAG_SEND_REAL_TX = True

ADMIN_ADDRESS = "ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi"
ADMIN_GAS_WIF = "K..."
MIN_USDZ = Decimal("0.00000001")
TIMEOUT_SECONDS = 3600
GASFREE_STORE_PATH = Path(".zhlink-gasfree-utxos.json")


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
    if ADMIN_GAS_WIF == "K...":
        print("Edit ADMIN_GAS_WIF, ADMIN_ADDRESS and MIN_USDZ at the top of this file.")
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

    result = await async_send_usdz_gas_free(
        sender_private_key_wif=wallet.priv_key,
        admin_private_key_wif=ADMIN_GAS_WIF,
        to_address=ADMIN_ADDRESS,
        amount=str(amount),
        config=config,
        broadcast=FLAG_SEND_REAL_TX,
        store_path=GASFREE_STORE_PATH,
    )

    print("forward result:")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())

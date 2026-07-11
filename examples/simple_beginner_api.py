import asyncio
import tempfile
from pathlib import Path
from pprint import pprint

import zhlink


# This example is safe to run as-is. It does not broadcast transactions until
# you replace the placeholders and set FLAG_SEND_REAL_TX=True.
FLAG_SEND_REAL_TX = False

BALANCE_ADDRESS = "Z..."
PRIVATE_KEY_WIF = "L..."
ADMIN_GAS_PRIVATE_KEY_WIF = "K..."
TO_ADDRESS = "Z..."

AMOUNT_ZHC = "1"
AMOUNT_USDZ = "0.1"


def create_wallet_example() -> None:
    wallet = zhlink.new_wallet()
    print("\n1. Create a local ZHC wallet")
    pprint(
        {
            "address": wallet.address,
            "private_key_wif": wallet.private_key_wif,
        }
    )


def seed_wallet_example() -> None:
    print("\n2. Create many recoverable ZHC wallets from one BIP39 seed")
    with tempfile.TemporaryDirectory() as tmp:
        seed_path = Path(tmp) / "zhlink-seed.json"
        seed = zhlink.new_seed_config(config_path=seed_path)
        first = zhlink.next_seed_wallet(seed_path)
        second = zhlink.next_seed_wallet(seed_path)
        restored_first = zhlink.restore_seed_wallet(index=0, config_path=seed_path)
        pprint(
            {
                "seed_words": seed.mnemonic,
                "first_address": first.address,
                "second_address": second.address,
                "restored_first_address": restored_first.address,
                "derivation_path": first.derivation_path,
            }
        )


def balance_example() -> None:
    print("\n3. Check ZHC + USDZ balance")
    if BALANCE_ADDRESS == "Z...":
        print("Edit BALANCE_ADDRESS to run a real balance request.")
        return
    pprint(zhlink.balance(BALANCE_ADDRESS))


def send_zhc_example() -> None:
    print("\n4. Send native ZHC with explicit send_zhc()")
    if PRIVATE_KEY_WIF == "L..." or TO_ADDRESS == "Z...":
        pprint(
            {
                "ready": False,
                "reason": "Edit PRIVATE_KEY_WIF and TO_ADDRESS.",
                "method": "zhlink.send_zhc(...)",
            }
        )
        return
    if not FLAG_SEND_REAL_TX:
        pprint({"dry_run": True, "asset": "ZHC", "to_address": TO_ADDRESS, "amount": AMOUNT_ZHC})
        return
    pprint(zhlink.send_zhc(PRIVATE_KEY_WIF, TO_ADDRESS, AMOUNT_ZHC))


def generic_send_example() -> None:
    print("\n5. Use generic send(asset=...) when the asset is dynamic")
    if PRIVATE_KEY_WIF == "L..." or TO_ADDRESS == "Z...":
        pprint(
            {
                "ready": False,
                "reason": "Edit PRIVATE_KEY_WIF and TO_ADDRESS.",
                "method": "zhlink.send(asset='ZHC', ...)",
            }
        )
        return
    if not FLAG_SEND_REAL_TX:
        pprint({"dry_run": True, "asset": "ZHC", "to_address": TO_ADDRESS, "amount": AMOUNT_ZHC})
        return
    pprint(
        zhlink.send(
            asset="ZHC",
            private_key_wif=PRIVATE_KEY_WIF,
            to_address=TO_ADDRESS,
            amount=AMOUNT_ZHC,
        )
    )


def send_usdz_free_example() -> None:
    print("\n6. Send USDZ with admin-paid ZHC gas")
    if PRIVATE_KEY_WIF == "L..." or ADMIN_GAS_PRIVATE_KEY_WIF == "K..." or TO_ADDRESS == "Z...":
        pprint(
            {
                "ready": False,
                "reason": "Edit PRIVATE_KEY_WIF, ADMIN_GAS_PRIVATE_KEY_WIF and TO_ADDRESS.",
                "method": "zhlink.send_usdz_free(...)",
            }
        )
        return
    pprint(
        zhlink.send_usdz_free(
            sender_private_key_wif=PRIVATE_KEY_WIF,
            admin_private_key_wif=ADMIN_GAS_PRIVATE_KEY_WIF,
            to_address=TO_ADDRESS,
            amount=AMOUNT_USDZ,
            broadcast=FLAG_SEND_REAL_TX,
        )
    )


async def async_examples() -> None:
    print("\n7. Async beginner API")
    wallet = await zhlink.async_new_wallet()
    with tempfile.TemporaryDirectory() as tmp:
        seed_path = Path(tmp) / "zhlink-seed.json"
        await zhlink.async_new_seed_config(config_path=seed_path)
        first = await zhlink.async_next_seed_wallet(seed_path)
        restored = await zhlink.async_restore_seed_wallet(index=0, config_path=seed_path)
    pprint(
        {
            "async_wallet": wallet.address,
            "async_seed_wallet": first.address,
            "async_restored_wallet": restored.address,
        }
    )


def main() -> None:
    create_wallet_example()
    seed_wallet_example()
    balance_example()
    send_zhc_example()
    generic_send_example()
    send_usdz_free_example()
    asyncio.run(async_examples())


if __name__ == "__main__":
    main()

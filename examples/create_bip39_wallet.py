from zhlink import generate_bip39_zhc_wallet


def main() -> None:
    wallet = generate_bip39_zhc_wallet(12)
    print(
        {
            "mnemonic": wallet.mnemonic,
            "address": wallet.address,
            "private_key_wif": wallet.private_key_wif,
            "derivation_path": wallet.derivation_path,
        }
    )


if __name__ == "__main__":
    main()

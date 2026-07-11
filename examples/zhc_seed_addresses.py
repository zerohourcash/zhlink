from pathlib import Path

from zhlink import (
    create_next_zhc_wallet_from_config,
    derive_zhc_wallet_from_config,
    generate_bip39_zhc_seed_config,
    load_zhc_seed_config,
)


CONFIG_PATH = Path(__file__).resolve().parents[1] / ".zhlink-zhc-seed.json"
WORD_COUNT = 12


if not CONFIG_PATH.exists():
    config = generate_bip39_zhc_seed_config(
        word_count=WORD_COUNT,
        config_path=CONFIG_PATH,
    )
    print("New BIP39 seed config created.")
    print("Save this seed phrase securely:")
    print(config.mnemonic)

wallet = create_next_zhc_wallet_from_config(CONFIG_PATH)
index = int(wallet.derivation_path.rsplit("/", 1)[-1])

print("ZHC address:", wallet.address)
print("Private key WIF:", wallet.private_key_wif)
print("Derivation path:", wallet.derivation_path)

restored = derive_zhc_wallet_from_config(index=index, config_path=CONFIG_PATH)
assert restored.address == wallet.address
assert restored.private_key_wif == wallet.private_key_wif

config = load_zhc_seed_config(CONFIG_PATH)
print("Next index:", config.next_index)

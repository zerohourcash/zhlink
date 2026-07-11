from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from zhlink.address import BitcoinAddress
from zhlink.config import (
    DEFAULT_USDZ_CONTRACT,
    TEST_GASFREE_ADMIN_ADDRESS,
    TEST_GASFREE_ADMIN_PRIVATE_KEY,
    ZHLinkConfig,
)
from zhlink.mnemonic import (
    ZHC_DEFAULT_DERIVATION_PATH,
    derive_bip39_zhc_wallet,
    generate_bip39_mnemonic,
    generate_bip39_zhc_wallet,
    mnemonic_to_seed,
    normalize_mnemonic,
    validate_bip39_mnemonic,
)
from zhlink.signer import sign_raw_transaction_with_key
from zhc_rawtx import ZHC
from zhc_rawtx import GasFreeStore, consolidate_utxos, send_usdz_gas_freee, split_largest_utxo
from zhc_rawtx.core import compressed_pubkey, p2pkh_script_pubkey, private_key_from_wif, serialize_tx


ADMIN_WIF = TEST_GASFREE_ADMIN_PRIVATE_KEY
ADMIN_ADDRESS = TEST_GASFREE_ADMIN_ADDRESS
RECIPIENT_ADDRESS = "ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi"
ADMIN_SCRIPT = p2pkh_script_pubkey(ADMIN_ADDRESS, ZHC).hex()


class ZhlinkLibBip39Tests(unittest.TestCase):
    def test_bip39_seed_vector(self) -> None:
        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        seed = mnemonic_to_seed(mnemonic, passphrase="TREZOR").hex()
        self.assertEqual(
            seed,
            (
                "c55257c360c07c72029aebc1b53c05ed0362ada38ead3e3e9efa3708e5349553"
                "1f09a6987599d18264c1e1c92f2cf141630c7a3c4ab7c81b2f001698e7463b04"
            ),
        )
        self.assertTrue(validate_bip39_mnemonic(mnemonic))

    def test_generate_12_and_24_word_mnemonics(self) -> None:
        for count in (12, 24):
            mnemonic = generate_bip39_mnemonic(count)
            self.assertEqual(len(mnemonic.split()), count)
            self.assertTrue(validate_bip39_mnemonic(mnemonic))

    def test_invalid_mnemonic_is_rejected(self) -> None:
        self.assertFalse(validate_bip39_mnemonic("abandon " * 12))
        with self.assertRaises(ValueError):
            derive_bip39_zhc_wallet("abandon " * 12)
        with self.assertRaises(ValueError):
            generate_bip39_mnemonic(15)

    def test_derive_bip39_zhc_wallet_is_deterministic(self) -> None:
        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        wallet = derive_bip39_zhc_wallet(mnemonic)
        restored = derive_bip39_zhc_wallet("  " + mnemonic.upper() + "  ")
        self.assertEqual(wallet.address, restored.address)
        self.assertEqual(wallet.private_key_wif, restored.private_key_wif)
        self.assertEqual(wallet.derivation_path, ZHC_DEFAULT_DERIVATION_PATH)
        self.assertEqual(normalize_mnemonic("  A   B  "), "a b")
        self.assertTrue(wallet.address.startswith("Z"))

    def test_generated_wallet_roundtrip(self) -> None:
        wallet = generate_bip39_zhc_wallet(12)
        restored = derive_bip39_zhc_wallet(wallet.mnemonic)
        self.assertEqual(wallet.address, restored.address)
        self.assertEqual(wallet.private_key_wif, restored.private_key_wif)


class ZhlinkLibAddressAndSignerTests(unittest.TestCase):
    def test_bitcoin_address_class_is_zhc_compatible(self) -> None:
        helper = BitcoinAddress()
        generated = helper.get_address_and_private_key()
        self.assertEqual(generated["status"], "ok")
        self.assertTrue(generated["address"].startswith("Z"))
        self.assertEqual(helper.address_from_wif(generated["priv_key"]), generated["address"])

    def test_rejects_non_zhc_network_constants(self) -> None:
        with self.assertRaises(ValueError):
            BitcoinAddress(prefix=b"\xef", suffix=b"\x6f")

    def test_sign_raw_transaction_with_key_p2pkh(self) -> None:
        raw_unsigned = serialize_tx(
            [
                {
                    "txid": "ba25602d59f95af604820152c4f1815d62288a760385d79d23a88ec14c815eba",
                    "vout": 0,
                    "script_sig": b"",
                    "sequence": 0xFFFFFFFF,
                }
            ],
            [
                {
                    "value_sat": 100000000,
                    "script_pubkey": p2pkh_script_pubkey(RECIPIENT_ADDRESS, ZHC),
                },
                {
                    "value_sat": 10000000,
                    "script_pubkey": p2pkh_script_pubkey(ADMIN_ADDRESS, ZHC),
                },
            ],
        ).hex()
        signed = sign_raw_transaction_with_key(
            raw_unsigned,
            ADMIN_WIF,
            [{"scriptPubKey": p2pkh_script_pubkey(ADMIN_ADDRESS, ZHC).hex()}],
        )
        self.assertTrue(signed.startswith("0200000001"))
        expected_pubkey = compressed_pubkey(private_key_from_wif(ADMIN_WIF, ZHC)).hex()
        self.assertIn(expected_pubkey, signed)

    def test_signer_requires_utxo_metadata_for_each_input(self) -> None:
        raw_unsigned = serialize_tx(
            [{"txid": "11" * 32, "vout": 0, "script_sig": b"", "sequence": 0xFFFFFFFF}],
            [{"value_sat": 100000000, "script_pubkey": p2pkh_script_pubkey(RECIPIENT_ADDRESS, ZHC)}],
        ).hex()
        with self.assertRaises(ValueError):
            sign_raw_transaction_with_key(raw_unsigned, ADMIN_WIF, [])


class ZhlinkLibGasfreeTests(unittest.TestCase):
    def test_gasfree_selects_and_remembers_admin_utxo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GasFreeStore(f"{tmp}/store.json")
            result = send_usdz_gas_freee(
                sender_private_key_wif=ADMIN_WIF,
                sender_address=ADMIN_ADDRESS,
                to_address=RECIPIENT_ADDRESS,
                amount_raw=1000000,
                admin_fee_private_key_wif=ADMIN_WIF,
                admin_fee_address=ADMIN_ADDRESS,
                admin_fee_utxos=[
                    {
                        "txid": "cc" * 32,
                        "vout": 0,
                        "value_sat": 30000000,
                        "script_pubkey": ADMIN_SCRIPT,
                        "confirmations": 10,
                    }
                ],
                contract_address_hex=DEFAULT_USDZ_CONTRACT,
                store=store,
            )
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["built"]["rawtx"].startswith("0200000001"))
            self.assertIn(f"{'cc' * 32}:0", store.used_outpoints())

    def test_gasfree_reselects_after_preflight_reject(self) -> None:
        seen: list[str] = []

        def preflight(rawtx: str) -> dict[str, object]:
            if not seen:
                seen.append(rawtx)
                return {"allowed": False, "reject-reason": "16: bad-txns-premature-spend-of-coinbase"}
            return {"allowed": True}

        with tempfile.TemporaryDirectory() as tmp:
            result = send_usdz_gas_freee(
                sender_private_key_wif=ADMIN_WIF,
                sender_address=ADMIN_ADDRESS,
                to_address=RECIPIENT_ADDRESS,
                amount_raw=1000000,
                admin_fee_private_key_wif=ADMIN_WIF,
                admin_fee_address=ADMIN_ADDRESS,
                admin_fee_utxos=[
                    {
                        "txid": "aa" * 32,
                        "vout": 0,
                        "value_sat": 30000000,
                        "script_pubkey": ADMIN_SCRIPT,
                        "confirmations": 10,
                    },
                    {
                        "txid": "bb" * 32,
                        "vout": 1,
                        "value_sat": 35000000,
                        "script_pubkey": ADMIN_SCRIPT,
                        "confirmations": 10,
                    },
                ],
                contract_address_hex=DEFAULT_USDZ_CONTRACT,
                store=GasFreeStore(f"{tmp}/store.json"),
                preflight=preflight,
            )
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(result["built"]["adminFeeOutpoint"], f"{'bb' * 32}:1")

    def test_gasfree_rejects_mismatched_admin_key(self) -> None:
        with self.assertRaises(ValueError):
            send_usdz_gas_freee(
                sender_private_key_wif=ADMIN_WIF,
                sender_address=ADMIN_ADDRESS,
                to_address=RECIPIENT_ADDRESS,
                amount_raw=1000000,
                admin_fee_private_key_wif=ADMIN_WIF,
                admin_fee_address=RECIPIENT_ADDRESS,
                admin_fee_utxos=[],
                contract_address_hex=DEFAULT_USDZ_CONTRACT,
            )


class ZhlinkLibUtxoMaintenanceTests(unittest.TestCase):
    def test_split_and_consolidate_work_inside_rawtx_engine(self) -> None:
        script = p2pkh_script_pubkey(ADMIN_ADDRESS, ZHC).hex()
        utxos = [
            {"txid": "aa" * 32, "vout": 0, "value_sat": 100_000_000, "script_pubkey": script, "confirmations": 10},
            {"txid": "bb" * 32, "vout": 0, "value_sat": 200_000_000, "script_pubkey": script, "confirmations": 10},
            {"txid": "cc" * 32, "vout": 0, "value_sat": 300_000_000, "script_pubkey": script, "confirmations": 10},
        ]
        split = split_largest_utxo(
            address=ADMIN_ADDRESS,
            private_key_wif=ADMIN_WIF,
            utxos=utxos,
            output_count=3,
            fee_sat=10_000_000,
        )
        self.assertEqual(split["selected_outpoints"], [f"{'cc' * 32}:0"])

        consolidated = consolidate_utxos(
            address=ADMIN_ADDRESS,
            private_key_wif=ADMIN_WIF,
            utxos=utxos,
            max_inputs=2,
            fee_sat=10_000_000,
        )
        self.assertEqual(consolidated["selected_outpoints"], [f"{'aa' * 32}:0", f"{'bb' * 32}:0"])


class ZhlinkLibPublicApiAndExamplesTests(unittest.TestCase):
    def test_new_zhlink_facade_is_small_and_beginner_friendly(self) -> None:
        import zhlink

        self.assertEqual(
            sorted(zhlink.__all__),
            [
                "Balance",
                "Bip39Wallet",
                "ZHLinkConfig",
                "admin_gas_wallet_info",
                "call_contract",
                "create_address",
                "create_wallet",
                "derive_bip39_zhc_wallet",
                "generate_bip39_mnemonic",
                "generate_bip39_zhc_wallet",
                "get_balance",
                "send_to_contract",
                "send_usdz_gas_free",
                "send_zhc",
                "validate_bip39_mnemonic",
            ],
        )
        self.assertTrue(callable(zhlink.create_address))
        self.assertTrue(callable(zhlink.call_contract))
        self.assertTrue(callable(zhlink.send_to_contract))
        self.assertFalse(hasattr(zhlink, "send_usdz_gas_freee"))
        self.assertFalse(hasattr(zhlink, "GasFreeStore"))
        self.assertFalse(hasattr(zhlink, "TEST_GASFREE_ADMIN_PRIVATE_KEY"))

    def test_new_zhlink_facade_create_address_without_rpc_stack(self) -> None:
        import zhlink

        wallet = zhlink.create_address()
        self.assertTrue(wallet.address.startswith("Z"))
        self.assertTrue(wallet.priv_key.startswith(("K", "L")))

    def test_config_defaults_are_exported(self) -> None:
        cfg = ZHLinkConfig.public_network()
        self.assertEqual(cfg.usdz_contract, DEFAULT_USDZ_CONTRACT)
        self.assertGreater(len(cfg.public_rpc_urls), 0)

    def test_top_level_import_does_not_require_httpx_rpc_stack(self) -> None:
        self.assertEqual(DEFAULT_USDZ_CONTRACT, "a48d0ee7365ce1add8e595de4d54344239f8ca28")
        import zhlink

        self.assertTrue(callable(zhlink.create_address))

    def test_call_contract_public_api_normalizes_rpc_response(self) -> None:
        import zhlink.api as api

        original = api._rpc_call

        def fake_rpc_call(config, method, params):
            self.assertEqual(method, "callcontract")
            self.assertEqual(params[0], DEFAULT_USDZ_CONTRACT)
            self.assertEqual(params[1], "70a08231")
            return {
                "executionResult": {
                    "output": "0" * 63 + "1",
                    "gasUsed": 12345,
                    "excepted": "None",
                }
            }

        api._rpc_call = fake_rpc_call
        try:
            result = api.call_contract(DEFAULT_USDZ_CONTRACT, "0x70a08231")
        finally:
            api._rpc_call = original
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["gas_used"], 12345)
        self.assertEqual(result["output"], "0" * 63 + "1")

    def test_examples_use_public_zhlink_facade(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        python_path = f"{repo}:{repo / 'zhc_rawtx'}"
        for script in [
            repo / "examples" / "create_wallet.py",
            repo / "examples" / "create_bip39_wallet.py",
            repo / "examples" / "simple_create_address.py",
        ]:
            completed = subprocess.run(
                [sys.executable, str(script)],
                check=True,
                text=True,
                capture_output=True,
                env={"PYTHONPATH": python_path, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertIn("{", completed.stdout)

    def test_send_examples_are_guarded_without_run_real_send(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        python_path = f"{repo}:{repo / 'zhc_rawtx'}"
        for script in [
            repo / "examples" / "send_zhc.py",
            repo / "examples" / "send_to_contract.py",
            repo / "examples" / "simple_send_zhc.py",
            repo / "examples" / "simple_send_usdz_gas_free.py",
        ]:
            completed = subprocess.run(
                [sys.executable, str(script)],
                check=True,
                text=True,
                capture_output=True,
                env={"PYTHONPATH": python_path, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertIn("Refusing to send", completed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

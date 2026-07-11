from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import asyncio
import time
from decimal import Decimal
from pathlib import Path

from zhlink.address import BitcoinAddress
from zhlink.config import (
    DEFAULT_USDZ_CONTRACT,
    TEST_GASFREE_ADMIN_ADDRESS,
    TEST_GASFREE_ADMIN_PRIVATE_KEY,
    ZHLinkConfig,
)
from zhlink.api import Balance
from zhlink.cache import SQLiteBalanceCache
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
from zhlink.rpc import WaitNextBlockError, ZHCashRPC
from zhlink.realtime import ZeroScanWebSocketHub, get_realtime_hub
from zhlink.zeroscan import ZeroScanRPC
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

    def test_wait_next_block_when_reserved_utxo_is_required(self) -> None:
        client = ZHCashRPC()
        outpoint = f"{'aa' * 32}:0"
        client.reserved_utxos[outpoint] = 1
        utxos = [
            {
                "txid": "aa" * 32,
                "vout": 0,
                "value": 20_000_000,
                "scriptPubKey": ADMIN_SCRIPT,
                "confirmations": 10,
            }
        ]

        with self.assertRaises(WaitNextBlockError) as ctx:
            client._select_utxos_inner(
                utxos,
                min_fee=Decimal("0.1"),
                amount=Decimal("0.01"),
            )

        self.assertEqual(ctx.exception.diagnostics["reason"], "all_spendable_utxos_reserved")
        self.assertEqual(ctx.exception.diagnostics["available_utxos"], 0)

    def test_balance_dict_can_expose_confirmed_and_pending(self) -> None:
        balance = Balance(
            address=ADMIN_ADDRESS,
            zhc=Decimal("1.50000000"),
            confirmed_zhc=Decimal("1.00000000"),
            pending_zhc=Decimal("0.50000000"),
            usdz=Decimal("2.00000000"),
            tokens={"USDZ": Decimal("2.00000000")},
            utxo_count=1,
        ).as_dict()

        self.assertEqual(balance["zhc"], "1.50000000")
        self.assertEqual(balance["confirmed_zhc"], "1.00000000")
        self.assertEqual(balance["pending_zhc"], "0.50000000")

    def test_sqlite_balance_cache_stores_snapshots_and_throttles_force_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = SQLiteBalanceCache(Path(tmp) / "zhlink.sqlite3")
            payload = {
                "status": "ok",
                "zhc": "1.50000000",
                "confirmed_zhc": "1.00000000",
                "pending_zhc": "0.50000000",
                "tokens": {"USDZ": "2.00000000"},
                "utxo_count": 1,
            }
            cache.put_balance(ADMIN_ADDRESS, payload, 123)
            utxos = [
                {
                    "txid": "aa" * 32,
                    "vout": 0,
                    "value_sat": 100_000_000,
                    "scriptPubKey": ADMIN_SCRIPT,
                    "confirmations": 10,
                }
            ]
            cache.put_utxos(ADMIN_ADDRESS, utxos, 123)
            cached = cache.get_balance(ADMIN_ADDRESS)
            cached_utxos = cache.get_utxos(ADMIN_ADDRESS)

            self.assertIsNotNone(cached)
            self.assertEqual(cached["zhc"], "1.50000000")
            self.assertEqual(cached["height"], 123)
            self.assertEqual(cached_utxos, utxos)
            self.assertFalse(cache.can_force_refresh(ADMIN_ADDRESS, 10))
            self.assertTrue(cache.can_force_refresh(ADMIN_ADDRESS, 0))
            cache.set_last_block_height(124)
            self.assertEqual(cache.get_last_block_height(), 124)

    def test_zeroscan_endpoint_circuit_breaker_skips_temporarily_disabled_endpoint(self) -> None:
        client = ZeroScanRPC(["https://bad.example", "https://good.example"])
        try:
            client.endpoints[0].failures = 3
            client.endpoints[0].disabled_until = 9999999999.0
            ordered = client._ordered_indexes()
            self.assertEqual(ordered[0], 1)
        finally:
            asyncio.run(client.close())

    def test_realtime_hub_is_shared_per_event_loop_and_url_set(self) -> None:
        async def run() -> None:
            urls = ("wss://ws.zeroscan.st/ws",)
            first = get_realtime_hub(urls)
            second = get_realtime_hub(urls)
            self.assertIs(first, second)
            await first.close()

        asyncio.run(run())

    def test_realtime_hub_dispatches_address_events_to_matching_subscribers(self) -> None:
        async def run() -> None:
            hub = ZeroScanWebSocketHub(("wss://ws.zeroscan.st/ws",))
            received: list[dict[str, object]] = []

            def callback(payload):
                received.append(payload)

            unsubscribe = hub.add_address_callback(ADMIN_ADDRESS, callback)
            if hub.task:
                hub.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hub.task
                hub.task = None
            await hub._emit_address({"type": "address:transaction", "address": RECIPIENT_ADDRESS})
            await hub._emit_address({"type": "address:transaction", "address": ADMIN_ADDRESS})
            unsubscribe()

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["address"], ADMIN_ADDRESS)

        import contextlib

        asyncio.run(run())

    def test_realtime_hub_prunes_stale_address_server_subscriptions(self) -> None:
        hub = ZeroScanWebSocketHub(("wss://ws.zeroscan.st/ws",), address_ttl_seconds=60)
        hub.address_last_used[ADMIN_ADDRESS] = time.monotonic() - 61
        hub.server_subscribed_addresses.add(ADMIN_ADDRESS)

        stale = hub.prune_stale_addresses()

        self.assertEqual(stale, [ADMIN_ADDRESS])
        self.assertNotIn(ADMIN_ADDRESS, hub.server_subscribed_addresses)
        self.assertEqual(
            hub._send_queue.get_nowait(),
            {"type": "unsubscribe", "channel": "address", "address": ADMIN_ADDRESS},
        )

        hub.touch_address(ADMIN_ADDRESS)
        self.assertIn(ADMIN_ADDRESS, hub.server_subscribed_addresses)
        self.assertEqual(
            hub._send_queue.get_nowait(),
            {"type": "subscribe", "channel": "address", "address": ADMIN_ADDRESS},
        )

    def test_realtime_hub_key_includes_degradation_settings(self) -> None:
        async def run() -> None:
            urls = ("wss://ws.zeroscan.st/ws",)
            first = get_realtime_hub(
                urls,
                address_ttl_seconds=3600,
                max_failures=3,
                cooldown_seconds=60,
            )
            second = get_realtime_hub(
                urls,
                address_ttl_seconds=43200,
                max_failures=5,
                cooldown_seconds=120,
            )
            self.assertIsNot(first, second)
            self.assertEqual(second.max_failures, 5)
            self.assertEqual(second.cooldown_seconds, 120)
            await first.close()
            await second.close()

        asyncio.run(run())


class ZhlinkLibPublicApiAndExamplesTests(unittest.TestCase):
    def test_new_zhlink_facade_is_small_and_beginner_friendly(self) -> None:
        import zhlink

        self.assertEqual(
            sorted(zhlink.__all__),
            [
                "Balance",
                "Bip39Wallet",
                "MASS_SEND_TEMPLATE_NAMES",
                "MassRecipient",
                "MassSendPlan",
                "WaitNextBlockError",
                "ZHLinkConfig",
                "admin_gas_wallet_info",
                "async_force_refresh_balance",
                "async_get_balance",
                "async_send_to_contract",
                "async_send_usdz_gas_free",
                "async_send_zhc",
                "call_contract",
                "create_address",
                "create_wallet",
                "derive_bip39_zhc_wallet",
                "estimate_mass_send",
                "force_refresh_balance",
                "generate_bip39_mnemonic",
                "generate_bip39_zhc_wallet",
                "get_balance",
                "get_cached_balance",
                "get_mass_send_template",
                "load_mass_send_plan",
                "prepare_mass_send_utxos",
                "send_mass",
                "send_to_contract",
                "send_usdz_gas_free",
                "send_zhc",
                "validate_bip39_mnemonic",
                "wait_for_next_block",
                "watch_balance",
                "write_mass_send_template",
            ],
        )
        self.assertTrue(callable(zhlink.create_address))
        self.assertTrue(callable(zhlink.call_contract))
        self.assertTrue(callable(zhlink.send_to_contract))
        self.assertTrue(callable(zhlink.send_mass))
        self.assertTrue(callable(zhlink.get_mass_send_template))
        self.assertTrue(callable(zhlink.async_get_balance))
        self.assertTrue(callable(zhlink.watch_balance))
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

    def test_config_allows_long_lived_websocket_address_ttl(self) -> None:
        cfg = ZHLinkConfig.public_network(
            address_subscription_ttl_seconds=12 * 60 * 60,
            ws_max_failures=5,
            ws_cooldown_seconds=120,
        )
        self.assertEqual(cfg.address_subscription_ttl_seconds, 43200)
        self.assertEqual(cfg.ws_max_failures, 5)
        self.assertEqual(cfg.ws_cooldown_seconds, 120)

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

    def test_mass_send_plan_and_utxo_estimate(self) -> None:
        import zhlink.mass as mass

        original_fetch = mass._fetch_zeroscan
        plan = mass.load_mass_send_plan(
            {
                "asset": "ZHC",
                "recipients": [
                    {"address": RECIPIENT_ADDRESS, "amount": "1"},
                    {"address": ADMIN_ADDRESS, "amount": "2"},
                ],
            }
        )
        self.assertEqual(plan.asset, "ZHC")
        self.assertEqual(plan.required_tx_count, 2)

        def fake_fetch(config, path):
            return {
                "utxos": [
                    {
                        "txid": "aa" * 32,
                        "vout": 0,
                        "value": 5_000_000_000,
                        "scriptPubKey": ADMIN_SCRIPT,
                        "confirmations": 10,
                    }
                ]
            }

        mass._fetch_zeroscan = fake_fetch
        try:
            estimate = mass.estimate_mass_send(ADMIN_WIF, plan)
        finally:
            mass._fetch_zeroscan = original_fetch
        self.assertTrue(estimate["need_reorg"])
        self.assertEqual(estimate["confirmed_utxo_count"], 1)

    def test_bundled_mass_send_templates_are_available(self) -> None:
        import zhlink

        for name in ("usdz", "zhc", "zrc20"):
            template = zhlink.get_mass_send_template(name)
            plan = zhlink.load_mass_send_plan(template)
            self.assertGreater(plan.required_tx_count, 0)
            self.assertEqual(template["asset"].upper(), plan.asset)

        with tempfile.TemporaryDirectory() as tmp:
            output = zhlink.write_mass_send_template("usdz", Path(tmp) / "mass_send.json")
            self.assertTrue(output.exists())
            self.assertEqual(zhlink.load_mass_send_plan(output).asset, "USDZ")

    def test_prepare_mass_send_utxos_builds_split_without_broadcast(self) -> None:
        import zhlink.mass as mass

        original_fetch = mass._fetch_zeroscan
        plan = mass.load_mass_send_plan(
            {
                "asset": "ZHC",
                "recipients": [
                    {"address": RECIPIENT_ADDRESS, "amount": "1"},
                    {"address": ADMIN_ADDRESS, "amount": "1"},
                    {"address": RECIPIENT_ADDRESS, "amount": "1"},
                ],
            }
        )

        def fake_fetch(config, path):
            return {
                "utxos": [
                    {
                        "txid": "bb" * 32,
                        "vout": 1,
                        "value": 10_000_000_000,
                        "scriptPubKey": ADMIN_SCRIPT,
                        "confirmations": 10,
                    }
                ]
            }

        mass._fetch_zeroscan = fake_fetch
        try:
            result = mass.prepare_mass_send_utxos(
                ADMIN_WIF,
                plan,
                target_utxos=3,
                broadcast=False,
                wait_confirmation=False,
            )
        finally:
            mass._fetch_zeroscan = original_fetch
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "split")
        self.assertEqual(result["output_count"], 3)
        self.assertTrue(result["built"]["rawtx"].startswith("0200000001"))

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
            repo / "examples" / "mass_send.py",
            repo / "examples" / "send_to_contract.py",
            repo / "examples" / "simple_send_zhc.py",
            repo / "examples" / "simple_send_usdz_gas_free.py",
            repo / "examples" / "watch_deposit_and_forward_usdz.py",
            repo / "examples" / "usdz_receiver_service.py",
        ]:
            completed = subprocess.run(
                [sys.executable, str(script)],
                check=True,
                text=True,
                capture_output=True,
                env={"PYTHONPATH": python_path, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertTrue(
                "Refusing to send" in completed.stdout
                or "Usage:" in completed.stdout,
                completed.stdout,
            )

    def test_receiver_delete_command_is_safe_for_missing_address(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        python_path = f"{repo}:{repo / 'zhc_rawtx'}"
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(repo / "examples" / "usdz_receiver_service.py"),
                    "delete",
                    "ZMissingAddressForSmokeTest",
                ],
                check=True,
                text=True,
                capture_output=True,
                env={
                    "PYTHONPATH": python_path,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "ZHLINK_RECEIVER_DB": str(Path(tmp) / "receiver.sqlite3"),
                },
            )
        self.assertIn("not found:", completed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

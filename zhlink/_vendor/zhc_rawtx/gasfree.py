from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from .core import (
    DUST_SAT,
    SIGHASH_ALL,
    ZHC,
    address_from_pubkey,
    b58decode_check,
    build_client_side_payment,
    compressed_pubkey,
    hash256,
    is_spendable_utxo,
    normalize_outpoint,
    p2pkh_script_pubkey,
    private_key_from_wif,
    push_data,
    serialize_tx,
    sign_digest_der,
    sign_gasfree_sender_sighash,
    p2pk_sighash_all,
    to_sats,
    varint,
)


PrepareGasFreeFn = Callable[[dict[str, Any]], dict[str, Any]]
FinishGasFreeFn = Callable[[dict[str, Any]], dict[str, Any]]


class GasFreeStore:
    """Tiny JSON store for gas-free templates and used fee UTXO metadata.

    The admin gas UTXO is selected by the relayer. The current relayer response
    may not expose the outpoint, but this store is ready to remember it when the
    server includes `feeOutpoint` or `outpoint`.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"templates": {}, "txids": {}, "used_outpoints": {}}
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {"templates": {}, "txids": {}, "used_outpoints": {}}
        data.setdefault("templates", {})
        data.setdefault("txids", {})
        data.setdefault("used_outpoints", {})
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(self.path)

    def remember_template(self, prepared: dict[str, Any]) -> None:
        template_id = str(prepared.get("templateId") or "").strip()
        if not template_id:
            return
        data = self.load()
        data["templates"][template_id] = {
            "at": int(time.time()),
            "templateId": template_id,
            "sighashHex": prepared.get("sighashHex"),
            "senderAddress": prepared.get("senderAddress"),
            "toAddress": prepared.get("toAddress"),
            "feeAddress": prepared.get("feeAddress"),
            "feeOutpoint": prepared.get("feeOutpoint") or prepared.get("outpoint"),
            "amountRaw": prepared.get("amountRaw"),
            "expiresAt": prepared.get("expiresAt"),
        }
        fee_outpoint = prepared.get("feeOutpoint") or prepared.get("outpoint")
        if fee_outpoint:
            data["used_outpoints"][str(fee_outpoint).lower()] = {
                "at": int(time.time()),
                "templateId": template_id,
                "status": "reserved",
            }
        self.save(data)

    def remember_result(self, template_id: str, result: dict[str, Any]) -> None:
        data = self.load()
        txid = str(result.get("txid") or result.get("tx_id") or "").strip()
        data["txids"][template_id] = {
            "at": int(time.time()),
            "templateId": template_id,
            "txid": txid,
            "endpoint": result.get("endpoint"),
            "transport": result.get("transport"),
            "raw": result,
        }
        template = data["templates"].get(template_id) or {}
        fee_outpoint = template.get("feeOutpoint")
        if fee_outpoint:
            data["used_outpoints"][str(fee_outpoint).lower()] = {
                "at": int(time.time()),
                "templateId": template_id,
                "txid": txid,
                "status": "broadcast",
            }
        self.save(data)

    def is_template_used(self, template_id: str) -> bool:
        data = self.load()
        return template_id in data.get("txids", {})

    def used_outpoints(self) -> set[str]:
        data = self.load()
        return {str(item).lower() for item in data.get("used_outpoints", {})}


def send_usdz_gas_free(
    *,
    private_key_wif: str,
    sender_address: str,
    to_address: str,
    amount_raw: int | str,
    contract_address_hex: str,
    prepare: PrepareGasFreeFn,
    finish: FinishGasFreeFn,
    store: GasFreeStore | None = None,
) -> dict[str, Any]:
    """Send USDZ with admin-paid ZHC gas using the gas-free relayer protocol.

    `prepare` must create a relayer template and return at least:

    - `templateId`
    - `sighashHex`

    `finish` receives:

    - `templateId`
    - `senderAddress`
    - `senderScriptSigHex`

    The private key never leaves this function.
    """

    prepared = prepare(
        {
            "senderAddress": sender_address,
            "toAddress": to_address,
            "amountRaw": str(amount_raw),
            "contractAddressHex": contract_address_hex.lower().replace("0x", ""),
        }
    )
    if not prepared.get("ok", True):
        raise RuntimeError(prepared.get("message") or prepared.get("error") or prepared)

    template_id = str(prepared.get("templateId") or "").strip()
    sighash_hex = str(prepared.get("sighashHex") or "").strip()
    if not template_id:
        raise RuntimeError(f"gas-free prepare did not return templateId: {prepared}")
    if store and store.is_template_used(template_id):
        raise RuntimeError(f"gas-free template already used locally: {template_id}")
    if store:
        store.remember_template(prepared)

    sender_signature = sign_gasfree_sender_sighash(private_key_wif, sighash_hex)
    result = finish(
        {
            "templateId": template_id,
            "senderAddress": sender_address,
            "senderScriptSigHex": sender_signature["scriptSigHex"],
        }
    )
    if not result.get("ok", True):
        raise RuntimeError(result.get("message") or result.get("error") or result)
    if store:
        store.remember_result(template_id, result)

    return {
        "status": "ok",
        "templateId": template_id,
        "txid": result.get("txid") or result.get("tx_id"),
        "endpoint": result.get("endpoint"),
        "transport": result.get("transport"),
        "prepared": prepared,
        "result": result,
    }


def script_num(value: int) -> bytes:
    if value == 0:
        return b""
    negative = value < 0
    abs_value = -value if negative else value
    out = bytearray()
    while abs_value:
        out.append(abs_value & 0xFF)
        abs_value >>= 8
    if out[-1] & 0x80:
        out.append(0x80 if negative else 0)
    elif negative:
        out[-1] |= 0x80
    return bytes(out)


def varbuf(data: bytes) -> bytes:
    return varint(len(data)) + data


def zhc_address_hash(address: str) -> bytes:
    raw = b58decode_check(address)
    if len(raw) != 21 or raw[0] != ZHC.pubkey_hash:
        raise ValueError("invalid ZHC P2PKH address")
    return raw[1:]


def recipient_address_hex(to_address_or_hex: str) -> str:
    value = to_address_or_hex.strip()
    normalized = value.lower().replace("0x", "")
    if len(normalized) == 40 and all(c in "0123456789abcdef" for c in normalized):
        return normalized
    return zhc_address_hash(value).hex()


def build_transfer_data(to_address_or_hex: str, amount_raw: int | str) -> str:
    amount = int(str(amount_raw))
    if amount <= 0:
        raise ValueError("amount_raw must be positive")
    return (
        "a9059cbb"
        + recipient_address_hex(to_address_or_hex).rjust(64, "0")
        + hex(amount)[2:].rjust(64, "0")
    )


def build_op_sender_call_script(
    *,
    sender_address: str,
    sender_script_sig_hex: str | None,
    gas_limit: int,
    gas_price: int,
    data_hex: str,
    contract_address_hex: str,
) -> bytes:
    sender_script_sig = (
        varbuf(bytes.fromhex(sender_script_sig_hex))
        if sender_script_sig_hex
        else b""
    )
    return b"".join(
        [
            push_data(script_num(1)),
            push_data(zhc_address_hash(sender_address)),
            push_data(sender_script_sig),
            bytes([0xC4]),  # OP_SENDER
            push_data(script_num(4)),
            push_data(script_num(int(gas_limit))),
            push_data(script_num(int(gas_price))),
            push_data(bytes.fromhex(data_hex)),
            push_data(bytes.fromhex(contract_address_hex.lower().replace("0x", ""))),
            bytes([0xC2]),  # OP_CALL
        ]
    )


def gasfree_template_sighash(
    *,
    fee_utxo: dict[str, Any],
    fee_address: str,
    sender_address: str,
    change_sat: int,
    blank_sender_script: bytes,
) -> bytes:
    out_change = {
        "value_sat": int(change_sat),
        "script_pubkey": p2pkh_script_pubkey(fee_address, ZHC),
    }
    out_call_no_sig = {"value_sat": 0, "script_pubkey": blank_sender_script}
    prevouts = bytes.fromhex(str(fee_utxo["txid"]))[::-1] + int(fee_utxo["vout"]).to_bytes(4, "little")
    sequences = (0xFFFFFFFF).to_bytes(4, "little")
    script_code = p2pkh_script_pubkey(sender_address, ZHC)
    outputs_raw = _serialize_output_only(out_change) + _serialize_output_only(out_call_no_sig)
    preimage = b"".join(
        [
            (2).to_bytes(4, "little"),
            hash256(prevouts),
            hash256(sequences),
            _serialize_output_only(out_call_no_sig),
            varbuf(script_code),
            (0).to_bytes(8, "little"),
            hash256(outputs_raw),
            (0).to_bytes(4, "little"),
            (1).to_bytes(4, "little"),
        ]
    )
    return hash256(preimage)


def select_single_fee_utxo(
    utxos: list[dict[str, Any]],
    required_sat: int,
    *,
    excluded_outpoints: set[str] | None = None,
    min_confirmations: int = 1,
) -> dict[str, Any]:
    """Pick the smallest single spendable admin UTXO that can pay gas.

    ZHC gas-free OP_SENDER template signs one real admin input. Using one input
    keeps the template deterministic and avoids accidentally reserving more UTXO
    than the final raw transaction spends.
    """

    excluded = {item.lower() for item in (excluded_outpoints or set())}
    candidates = [
        utxo
        for utxo in utxos
        if normalize_outpoint(utxo) not in excluded
        and is_spendable_utxo(utxo, min_confirmations=min_confirmations)
        and int(utxo.get("value_sat", 0)) >= required_sat
    ]
    candidates.sort(key=lambda item: (int(item["value_sat"]), -int(item.get("confirmations", 0))))
    if not candidates:
        available = sum(
            int(utxo.get("value_sat", 0))
            for utxo in utxos
            if normalize_outpoint(utxo) not in excluded
            and is_spendable_utxo(utxo, min_confirmations=min_confirmations)
        )
        raise RuntimeError(
            f"no single spendable admin fee UTXO: need {required_sat}, available total {available}"
        )
    return candidates[0]


def _serialize_output_only(output: dict[str, Any]) -> bytes:
    from .core import serialize_output

    return serialize_output(int(output["value_sat"]), output["script_pubkey"])


def sign_fee_input(raw_inputs: list[dict[str, Any]], outputs: list[dict[str, Any]], fee_wif: str) -> str:
    private_key = private_key_from_wif(fee_wif, ZHC)
    pubkey = compressed_pubkey(private_key)
    signed_inputs = []
    for index, txin in enumerate(raw_inputs):
        prev_script = bytes.fromhex(str(txin["script_pubkey"]))
        digest = p2pk_sighash_all(raw_inputs, outputs, index, prev_script)
        der = sign_digest_der(private_key, digest)
        script_sig = push_data(der + bytes([SIGHASH_ALL])) + push_data(pubkey)
        signed_inputs.append({**txin, "script_sig": script_sig})
    return serialize_tx(signed_inputs, outputs).hex()


def send_usdz_gas_free_local(
    *,
    sender_private_key_wif: str,
    sender_address: str,
    to_address: str,
    amount_raw: int | str,
    admin_fee_private_key_wif: str,
    admin_fee_address: str,
    admin_fee_utxos: list[dict[str, Any]],
    contract_address_hex: str,
    gas_limit: int = 350_000,
    gas_price: int = 40,
    network_fee_sat: int = 10_000_000,
    store: GasFreeStore | None = None,
    excluded_outpoints: set[str] | None = None,
    reserve_local: bool = True,
) -> dict[str, Any]:
    """Build a complete gas-free USDZ rawtx locally.

    The sender signs OP_SENDER authorization. The admin fee key signs the actual
    UTXO input and pays ZHC gas. No RPC `createrawtransaction` is used.
    """

    sender_key = private_key_from_wif(sender_private_key_wif, ZHC)
    if address_from_pubkey(compressed_pubkey(sender_key), ZHC) != sender_address:
        raise ValueError("sender private key does not match sender_address")
    fee_key = private_key_from_wif(admin_fee_private_key_wif, ZHC)
    if address_from_pubkey(compressed_pubkey(fee_key), ZHC) != admin_fee_address:
        raise ValueError("admin fee private key does not match admin_fee_address")

    required_fee_sat = int(network_fee_sat) + int(gas_limit) * int(gas_price)
    excluded = set(excluded_outpoints or set())
    if store:
        excluded.update(store.used_outpoints())
    fee_utxo = select_single_fee_utxo(
        admin_fee_utxos,
        required_fee_sat + DUST_SAT,
        excluded_outpoints=excluded,
        min_confirmations=1,
    )
    fee_outpoint = normalize_outpoint(fee_utxo)
    total_sat = int(fee_utxo["value_sat"])

    change_sat = total_sat - required_fee_sat
    if change_sat <= DUST_SAT:
        raise RuntimeError("admin fee UTXO does not leave spendable change")

    data_hex = build_transfer_data(to_address, amount_raw)
    contract_hex = contract_address_hex.lower().replace("0x", "")
    blank_sender_script = build_op_sender_call_script(
        sender_address=sender_address,
        sender_script_sig_hex=None,
        gas_limit=gas_limit,
        gas_price=gas_price,
        data_hex=data_hex,
        contract_address_hex=contract_hex,
    )
    sighash = gasfree_template_sighash(
        fee_utxo=fee_utxo,
        fee_address=admin_fee_address,
        sender_address=sender_address,
        change_sat=change_sat,
        blank_sender_script=blank_sender_script,
    )
    sender_sig = sign_gasfree_sender_sighash(sender_private_key_wif, sighash.hex())
    sender_script = build_op_sender_call_script(
        sender_address=sender_address,
        sender_script_sig_hex=sender_sig["scriptSigHex"],
        gas_limit=gas_limit,
        gas_price=gas_price,
        data_hex=data_hex,
        contract_address_hex=contract_hex,
    )
    inputs = [
        {
            "txid": fee_utxo["txid"],
            "vout": int(fee_utxo["vout"]),
            "script_pubkey": fee_utxo["script_pubkey"],
            "sequence": 0xFFFFFFFF,
        }
    ]
    outputs = [
        {
            "value_sat": change_sat,
            "script_pubkey": p2pkh_script_pubkey(admin_fee_address, ZHC),
        },
        {"value_sat": 0, "script_pubkey": sender_script},
    ]
    rawtx = sign_fee_input(inputs, outputs, admin_fee_private_key_wif)
    if store and reserve_local:
        store.remember_template(
            {
                "templateId": f"local:{fee_outpoint}:{sighash.hex()}",
                "sighashHex": sighash.hex(),
                "senderAddress": sender_address,
                "toAddress": to_address,
                "feeAddress": admin_fee_address,
                "feeOutpoint": fee_outpoint,
                "amountRaw": str(amount_raw),
            }
        )
    return {
        "status": "ok",
        "rawtx": rawtx,
        "sighashHex": sighash.hex(),
        "senderScriptSigHex": sender_sig["scriptSigHex"],
        "adminFeeOutpoint": fee_outpoint,
        "adminInputTotalSat": total_sat,
        "requiredFeeSat": required_fee_sat,
        "changeSat": change_sat,
        "dataHex": data_hex,
        "contractAddressHex": contract_hex,
    }


PreflightRawTxFn = Callable[[str], dict[str, Any]]
BroadcastRawTxFn = Callable[[str], dict[str, Any]]


def send_usdz_gas_freee(
    *,
    sender_private_key_wif: str,
    sender_address: str,
    to_address: str,
    amount_raw: int | str,
    admin_fee_private_key_wif: str,
    admin_fee_address: str,
    admin_fee_utxos: list[dict[str, Any]],
    contract_address_hex: str,
    gas_limit: int = 350_000,
    gas_price: int = 40,
    network_fee_sat: int = 10_000_000,
    store: GasFreeStore | None = None,
    preflight: PreflightRawTxFn | None = None,
    broadcast: BroadcastRawTxFn | None = None,
    max_attempts: int = 5,
    reserve_local: bool = True,
) -> dict[str, Any]:
    """Complete one-call USDZ gas-free sender.

    The caller supplies fresh admin UTXO candidates. The function:

    1. validates sender/admin keys;
    2. skips locally used UTXO;
    3. chooses the smallest suitable admin UTXO automatically;
    4. builds and signs the complete rawtx locally;
    5. optionally runs `testmempoolaccept`;
    6. optionally broadcasts;
    7. remembers the selected admin UTXO in `GasFreeStore`.

    The misspelled name is kept intentionally because existing automation asked
    for `send_usdz_gas_freee`.
    """

    excluded: set[str] = set()
    last_reject: Any = None
    for attempt in range(1, max_attempts + 1):
        built = send_usdz_gas_free_local(
            sender_private_key_wif=sender_private_key_wif,
            sender_address=sender_address,
            to_address=to_address,
            amount_raw=amount_raw,
            admin_fee_private_key_wif=admin_fee_private_key_wif,
            admin_fee_address=admin_fee_address,
            admin_fee_utxos=admin_fee_utxos,
            contract_address_hex=contract_address_hex,
            gas_limit=gas_limit,
            gas_price=gas_price,
            network_fee_sat=network_fee_sat,
            store=store,
            excluded_outpoints=excluded,
            reserve_local=False,
        )
        rawtx = built["rawtx"]
        if preflight:
            check = preflight(rawtx)
            allowed = bool(check.get("allowed", False) or check.get("ok", False))
            if not allowed:
                last_reject = check
                reason = str(check.get("reject-reason") or check.get("reason") or check.get("error") or "")
                if (
                    "premature-spend-of-coinbase" in reason
                    or "bad-txns" in reason
                    or "missing-inputs" in reason
                    or "txn-mempool-conflict" in reason
                    or "already in block chain" in reason
                ):
                    excluded.add(str(built["adminFeeOutpoint"]).lower())
                    continue
                raise RuntimeError(f"gas-free preflight rejected rawtx: {reason or check}")

        result = broadcast(rawtx) if broadcast else None
        if store and reserve_local:
            template_id = f"local:{built['adminFeeOutpoint']}:{built['sighashHex']}"
            store.remember_template(
                {
                    "templateId": template_id,
                    "sighashHex": built["sighashHex"],
                    "senderAddress": sender_address,
                    "toAddress": to_address,
                    "feeAddress": admin_fee_address,
                    "feeOutpoint": built["adminFeeOutpoint"],
                    "amountRaw": str(amount_raw),
                }
            )
            if result:
                store.remember_result(template_id, result)
        return {
            "status": "ok",
            "attempt": attempt,
            "built": built,
            "broadcast": result,
        }
    raise RuntimeError(f"could not build accepted gas-free rawtx after {max_attempts} attempts: {last_reject}")

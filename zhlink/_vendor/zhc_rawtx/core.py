from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable, Iterable

from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


SIGHASH_ALL = 1
COIN = Decimal("100000000")
DUST_SAT = 10_000
MIN_RECOMMENDED_FEE_SAT = 10_000_000
DEFAULT_FEE_RATE_SAT_PER_KB = 400_000
P2PKH_INPUT_BYTES = 148
P2PK_INPUT_BYTES = 114
P2PKH_OUTPUT_BYTES = 34
TX_OVERHEAD_BYTES = 10
ZHC_UTXO_REORG_DEFAULT_BATCH_SIZE = 100
ZHC_UTXO_REORG_MAX_BATCH_SIZE = 500
ZHC_UTXO_SPLIT_MAX_OUTPUT_COUNT = 100
SECP256K1_ORDER = int(
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141",
    16,
)


@dataclass(frozen=True)
class UtxoNetwork:
    name: str
    pubkey_hash: int
    script_hash: int
    wif: int


ZHC = UtxoNetwork("ZHCASH", pubkey_hash=0x50, script_hash=0x10, wif=0x80)


BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_INDEX = {char: index for index, char in enumerate(BASE58_ALPHABET)}


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hash256(data: bytes) -> bytes:
    return sha256(sha256(data))


def hash160(data: bytes) -> bytes:
    digest = sha256(data)
    try:
        ripe = hashlib.new("ripemd160")
        ripe.update(digest)
        return ripe.digest()
    except ValueError:
        from Crypto.Hash import RIPEMD160

        ripe = RIPEMD160.new()
        ripe.update(digest)
        return ripe.digest()


def b58decode(value: str) -> bytes:
    number = 0
    for char in value:
        if char not in BASE58_INDEX:
            raise ValueError(f"invalid base58 character: {char!r}")
        number = number * 58 + BASE58_INDEX[char]
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    pad = len(value) - len(value.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    chars = []
    while number:
        number, rem = divmod(number, 58)
        chars.append(BASE58_ALPHABET[rem])
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + "".join(reversed(chars or ["1"])).lstrip("1")


def b58decode_check(value: str) -> bytes:
    raw = b58decode(value)
    if len(raw) < 4:
        raise ValueError("base58check payload too short")
    payload, checksum = raw[:-4], raw[-4:]
    if hash256(payload)[:4] != checksum:
        raise ValueError("bad base58check checksum")
    return payload


def b58encode_check(payload: bytes) -> str:
    return b58encode(payload + hash256(payload)[:4])


def to_sats(value: str | Decimal | int) -> int:
    amount = Decimal(str(value)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    if amount < 0:
        raise ValueError("amount must be non-negative")
    return int(amount * COIN)


def from_sats(value: int) -> str:
    amount = (Decimal(value) / COIN).quantize(Decimal("0.00000001"))
    return format(amount, "f")


def varint(n: int) -> bytes:
    if n < 0:
        raise ValueError("varint cannot be negative")
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def push_data(data: bytes) -> bytes:
    length = len(data)
    if length < 0x4C:
        return bytes([length]) + data
    if length <= 0xFF:
        return b"\x4c" + bytes([length]) + data
    if length <= 0xFFFF:
        return b"\x4d" + struct.pack("<H", length) + data
    return b"\x4e" + struct.pack("<I", length) + data


def decode_wif(wif: str, network: UtxoNetwork) -> tuple[bytes, bool]:
    raw = b58decode_check(wif)
    if not raw or raw[0] != network.wif:
        got = raw[0] if raw else None
        raise ValueError(f"WIF version mismatch: got {got!r}, expected 0x{network.wif:02x}")
    if len(raw) == 34 and raw[-1] == 0x01:
        return raw[1:-1], True
    if len(raw) == 33:
        return raw[1:], False
    raise ValueError("invalid WIF length")


def private_key_from_wif(wif: str, network: UtxoNetwork) -> ec.EllipticCurvePrivateKey:
    secret, _compressed = decode_wif(wif, network)
    number = int.from_bytes(secret, "big")
    if number <= 0 or number >= SECP256K1_ORDER:
        raise ValueError("private key outside secp256k1 range")
    return ec.derive_private_key(number, ec.SECP256K1())


def compressed_pubkey(private_key: ec.EllipticCurvePrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        Encoding.X962,
        PublicFormat.CompressedPoint,
    )


def address_from_pubkey(pubkey: bytes, network: UtxoNetwork) -> str:
    return b58encode_check(bytes([network.pubkey_hash]) + hash160(pubkey))


def p2pkh_script_pubkey_from_hash(pubkey_hash: bytes) -> bytes:
    if len(pubkey_hash) != 20:
        raise ValueError("pubkey hash must be 20 bytes")
    return b"\x76\xa9\x14" + pubkey_hash + b"\x88\xac"


def p2pkh_script_pubkey(address: str, network: UtxoNetwork) -> bytes:
    raw = b58decode_check(address)
    if len(raw) != 21:
        raise ValueError("bad address payload length")
    version, payload = raw[0], raw[1:]
    if version != network.pubkey_hash:
        raise ValueError(f"address is not P2PKH for {network.name}")
    return p2pkh_script_pubkey_from_hash(payload)


def serialize_input(
    txid: str,
    vout: int,
    script_sig: bytes = b"",
    sequence: int = 0xFFFFFFFF,
) -> bytes:
    if len(txid) != 64:
        raise ValueError("txid must be 32-byte hex")
    return (
        bytes.fromhex(txid)[::-1]
        + struct.pack("<I", int(vout))
        + varint(len(script_sig))
        + script_sig
        + struct.pack("<I", int(sequence))
    )


def serialize_output(value_sat: int, script_pubkey: bytes) -> bytes:
    if value_sat < 0:
        raise ValueError("output value cannot be negative")
    return struct.pack("<Q", int(value_sat)) + varint(len(script_pubkey)) + script_pubkey


def serialize_tx(
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    version: int = 2,
    locktime: int = 0,
) -> bytes:
    data = struct.pack("<I", version)
    data += varint(len(inputs))
    for txin in inputs:
        data += serialize_input(
            txid=str(txin["txid"]),
            vout=int(txin["vout"]),
            script_sig=txin.get("script_sig", b""),
            sequence=int(txin.get("sequence", 0xFFFFFFFF)),
        )
    data += varint(len(outputs))
    for txout in outputs:
        data += serialize_output(int(txout["value_sat"]), txout["script_pubkey"])
    data += struct.pack("<I", locktime)
    return data


def p2pk_sighash_all(
    unsigned_inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    input_index: int,
    prev_script_pubkey: bytes,
    version: int = 2,
    locktime: int = 0,
) -> bytes:
    signing_inputs = []
    for index, txin in enumerate(unsigned_inputs):
        signing_inputs.append(
            {
                "txid": txin["txid"],
                "vout": txin["vout"],
                "script_sig": prev_script_pubkey if index == input_index else b"",
                "sequence": txin.get("sequence", 0xFFFFFFFF),
            }
        )
    preimage = serialize_tx(signing_inputs, outputs, version, locktime)
    preimage += struct.pack("<I", SIGHASH_ALL)
    return hash256(preimage)


def sign_digest_der(private_key: ec.EllipticCurvePrivateKey, digest: bytes) -> bytes:
    if len(digest) != 32:
        raise ValueError("digest must be 32 bytes")
    signature = private_key.sign(digest, ec.ECDSA(utils.Prehashed(hashes_sha256())))
    r, s = utils.decode_dss_signature(signature)
    # Bitcoin-like nodes generally require low-S signatures as standard policy.
    if s > SECP256K1_ORDER // 2:
        s = SECP256K1_ORDER - s
    return utils.encode_dss_signature(r, s)


def hashes_sha256():
    # Imported lazily to keep the top-level imports compact.
    from cryptography.hazmat.primitives import hashes

    return hashes.SHA256()


def sign_p2pkh_transaction(
    unsigned_inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    private_key_wif: str,
    network: UtxoNetwork = ZHC,
) -> str:
    private_key = private_key_from_wif(private_key_wif, network)
    pubkey = compressed_pubkey(private_key)
    signed_inputs = []
    for index, txin in enumerate(unsigned_inputs):
        prev_script_pubkey = bytes.fromhex(str(txin["script_pubkey"]))
        digest = p2pk_sighash_all(unsigned_inputs, outputs, index, prev_script_pubkey)
        der = sign_digest_der(private_key, digest)
        script_sig = push_data(der + bytes([SIGHASH_ALL])) + push_data(pubkey)
        signed_inputs.append(
            {
                "txid": txin["txid"],
                "vout": txin["vout"],
                "script_sig": script_sig,
                "sequence": txin.get("sequence", 0xFFFFFFFF),
            }
        )
    return serialize_tx(signed_inputs, outputs).hex()


def script_type(script_pubkey_hex: str) -> str:
    script = (script_pubkey_hex or "").lower()
    if len(script) == 50 and script.startswith("76a914") and script.endswith("88ac"):
        return "p2pkh"
    if len(script) in (70, 134) and script.endswith("ac"):
        return "p2pk"
    raise ValueError(f"unsupported input scriptPubKey: {script_pubkey_hex}")


def sign_utxo_transaction_with_key(
    unsigned_inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    private_key_wif: str,
    network: UtxoNetwork = ZHC,
) -> str:
    private_key = private_key_from_wif(private_key_wif, network)
    pubkey = compressed_pubkey(private_key)
    signed_inputs = []
    for index, txin in enumerate(unsigned_inputs):
        prev_script_pubkey = bytes.fromhex(str(txin["script_pubkey"]))
        digest = p2pk_sighash_all(unsigned_inputs, outputs, index, prev_script_pubkey)
        der = sign_digest_der(private_key, digest)
        signature = push_data(der + bytes([SIGHASH_ALL]))
        kind = script_type(str(txin["script_pubkey"]))
        script_sig = signature + (push_data(pubkey) if kind == "p2pkh" else b"")
        signed_inputs.append(
            {
                "txid": txin["txid"],
                "vout": txin["vout"],
                "script_sig": script_sig,
                "sequence": txin.get("sequence", 0xFFFFFFFF),
            }
        )
    return serialize_tx(signed_inputs, outputs).hex()


def normalize_outpoint(utxo: dict[str, Any]) -> str:
    return f"{str(utxo['txid']).lower()}:{int(utxo['vout'])}"


def is_spendable_utxo(utxo: dict[str, Any], min_confirmations: int = 1) -> bool:
    if int(utxo.get("confirmations", 0)) < min_confirmations:
        return False
    if int(utxo.get("value_sat", 0)) <= DUST_SAT:
        return False
    if utxo.get("spendable") is False or utxo.get("safe") is False:
        return False
    if utxo.get("isStake") or utxo.get("is_stake"):
        # Keep this conservative for general payments. Stake consolidation can
        # be a separate explicit mode.
        return False
    if utxo.get("coinbase") and int(utxo.get("confirmations", 0)) < 101:
        return False
    if utxo.get("coinstake") and int(utxo.get("confirmations", 0)) < 501:
        return False
    return True


def _utxo_script_pubkey_hex(utxo: dict[str, Any]) -> str:
    script = utxo.get("script_pubkey") or utxo.get("scriptPubKeyHex") or utxo.get("scriptPubKey")
    if isinstance(script, dict):
        script = script.get("hex")
    if not script:
        raise ValueError("UTXO script_pubkey/scriptPubKey is required")
    return str(script).lower()


def _utxo_value_sat(utxo: dict[str, Any]) -> int:
    if "value_sat" in utxo:
        return int(utxo["value_sat"])
    if "value" in utxo:
        return int(utxo["value"])
    if "amount" in utxo:
        return to_sats(str(utxo["amount"]))
    raise ValueError("UTXO value_sat/value/amount is required")


def normalize_zhc_utxo(utxo: dict[str, Any]) -> dict[str, Any] | None:
    txid = str(utxo.get("txid") or utxo.get("transactionId") or "")
    vout_raw = utxo.get("vout", utxo.get("outputIndex"))
    if len(txid) != 64 or vout_raw is None:
        return None
    try:
        script_hex = _utxo_script_pubkey_hex(utxo)
        kind = script_type(script_hex)
        value_sat = _utxo_value_sat(utxo)
    except Exception:
        return None
    normalized = {
        "txid": txid.lower(),
        "vout": int(vout_raw),
        "value_sat": value_sat,
        "script_pubkey": script_hex,
        "confirmations": int(utxo.get("confirmations") or 0),
        "isStake": bool(utxo.get("isStake") or utxo.get("is_stake") or utxo.get("coinstake")),
        "coinbase": bool(utxo.get("coinbase")),
        "coinstake": bool(utxo.get("coinstake") or utxo.get("isStake") or utxo.get("is_stake")),
        "script_type": kind,
        "original": utxo,
    }
    if not is_spendable_utxo(normalized):
        return None
    return normalized


def normalize_zhc_utxos(utxos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in (normalize_zhc_utxo(utxo) for utxo in utxos) if item is not None]


def _input_bytes(utxo: dict[str, Any]) -> int:
    return P2PK_INPUT_BYTES if utxo.get("script_type") == "p2pk" else P2PKH_INPUT_BYTES


def estimate_tx_bytes(inputs: list[dict[str, Any]], output_bytes: list[int]) -> int:
    return TX_OVERHEAD_BYTES + sum(_input_bytes(item) for item in inputs) + sum(output_bytes)


def fee_for_bytes(estimated_bytes: int, fee_sat: int | None = None) -> int:
    byte_fee = (max(1, int(estimated_bytes)) * DEFAULT_FEE_RATE_SAT_PER_KB + 999) // 1000
    floor = MIN_RECOMMENDED_FEE_SAT if fee_sat is None else max(int(fee_sat), MIN_RECOMMENDED_FEE_SAT)
    return max(byte_fee, floor)


def _service_fee_output(service_fee_address: str | None, service_fee_sat: int | None) -> dict[str, Any] | None:
    if not service_fee_address or not service_fee_sat or int(service_fee_sat) <= 0:
        return None
    return {
        "value_sat": int(service_fee_sat),
        "script_pubkey": p2pkh_script_pubkey(service_fee_address, ZHC),
    }


def _built_result(
    *,
    rawtx: str,
    selected: list[dict[str, Any]],
    input_total_sat: int,
    target_sat: int,
    fee_sat: int,
    estimated_bytes: int,
    service_fee_sat: int | None = None,
) -> dict[str, Any]:
    result = {
        "rawtx": rawtx,
        "hex": rawtx,
        "selected_outpoints": [normalize_outpoint(utxo) for utxo in selected],
        "selectedOutpoints": [normalize_outpoint(utxo) for utxo in selected],
        "input_total_sat": input_total_sat,
        "inputTotalSat": input_total_sat,
        "target_sat": target_sat,
        "targetSat": target_sat,
        "fee_sat": fee_sat,
        "feeSat": fee_sat,
        "change_sat": 0,
        "changeSat": 0,
        "estimated_bytes": estimated_bytes,
        "estimatedBytes": estimated_bytes,
    }
    if service_fee_sat:
        result["service_fee_sat"] = int(service_fee_sat)
        result["serviceFeeSat"] = int(service_fee_sat)
    return result


def split_largest_utxo(
    *,
    address: str,
    private_key_wif: str,
    utxos: list[dict[str, Any]],
    output_count: int,
    fee_sat: int | None = None,
    service_fee_address: str | None = None,
    service_fee_sat: int | None = None,
    excluded_outpoints: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Split the largest spendable ZHC UTXO into N self outputs."""

    private_key = private_key_from_wif(private_key_wif, ZHC)
    if address_from_pubkey(compressed_pubkey(private_key), ZHC) != address:
        raise ValueError("private key does not match address")
    excluded = {item.lower() for item in (excluded_outpoints or [])}
    output_count = max(2, min(ZHC_UTXO_SPLIT_MAX_OUTPUT_COUNT, int(output_count)))
    candidates = [
        utxo for utxo in normalize_zhc_utxos(utxos) if normalize_outpoint(utxo) not in excluded
    ]
    candidates.sort(key=lambda item: (int(item["value_sat"]), int(item.get("confirmations", 0))), reverse=True)
    if not candidates:
        raise RuntimeError("No mature spendable P2PKH/P2PK UTXO found.")
    selected = candidates[0]
    service_output = _service_fee_output(service_fee_address, service_fee_sat)
    estimated_bytes = estimate_tx_bytes(
        [selected],
        [P2PKH_OUTPUT_BYTES] * output_count + ([P2PKH_OUTPUT_BYTES] if service_output else []),
    )
    actual_fee_sat = fee_for_bytes(estimated_bytes, fee_sat)
    service_sat = int(service_fee_sat or 0) if service_output else 0
    output_total_sat = int(selected["value_sat"]) - actual_fee_sat - service_sat
    base_output_sat = output_total_sat // output_count
    remainder_sat = output_total_sat % output_count
    if base_output_sat <= DUST_SAT:
        raise RuntimeError("Selected UTXO is too small to split into this many spendable outputs.")

    inputs = [
        {
            "txid": selected["txid"],
            "vout": selected["vout"],
            "script_pubkey": selected["script_pubkey"],
            "sequence": 0xFFFFFFFF,
        }
    ]
    outputs = []
    for index in range(output_count):
        outputs.append(
            {
                "value_sat": base_output_sat + (1 if index < remainder_sat else 0),
                "script_pubkey": p2pkh_script_pubkey(address, ZHC),
            }
        )
    if service_output:
        outputs.append(service_output)
    rawtx = sign_utxo_transaction_with_key(inputs, outputs, private_key_wif, ZHC)
    return _built_result(
        rawtx=rawtx,
        selected=[selected],
        input_total_sat=int(selected["value_sat"]),
        target_sat=output_total_sat + actual_fee_sat + service_sat,
        fee_sat=actual_fee_sat,
        estimated_bytes=estimated_bytes,
        service_fee_sat=service_sat or None,
    )


def consolidate_utxos(
    *,
    address: str,
    private_key_wif: str,
    utxos: list[dict[str, Any]],
    fee_sat: int | None = None,
    service_fee_address: str | None = None,
    service_fee_sat: int | None = None,
    excluded_outpoints: Iterable[str] | None = None,
    max_inputs: int = ZHC_UTXO_REORG_DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Consolidate up to N smallest spendable ZHC UTXO into one self output."""

    private_key = private_key_from_wif(private_key_wif, ZHC)
    if address_from_pubkey(compressed_pubkey(private_key), ZHC) != address:
        raise ValueError("private key does not match address")
    excluded = {item.lower() for item in (excluded_outpoints or [])}
    max_inputs = max(1, min(ZHC_UTXO_REORG_MAX_BATCH_SIZE, int(max_inputs)))
    selected = [
        utxo for utxo in normalize_zhc_utxos(utxos) if normalize_outpoint(utxo) not in excluded
    ]
    selected.sort(key=lambda item: (int(item["value_sat"]), -int(item.get("confirmations", 0))))
    selected = selected[:max_inputs]
    if len(selected) < 2:
        raise RuntimeError("Not enough spendable UTXO to reorganize.")
    service_output = _service_fee_output(service_fee_address, service_fee_sat)
    input_total_sat = sum(int(utxo["value_sat"]) for utxo in selected)
    estimated_bytes = estimate_tx_bytes(
        selected,
        [P2PKH_OUTPUT_BYTES] + ([P2PKH_OUTPUT_BYTES] if service_output else []),
    )
    actual_fee_sat = fee_for_bytes(estimated_bytes, fee_sat)
    service_sat = int(service_fee_sat or 0) if service_output else 0
    output_sat = input_total_sat - actual_fee_sat - service_sat
    if output_sat <= DUST_SAT:
        raise RuntimeError("Selected UTXO total does not cover the recommended fee.")
    inputs = [
        {
            "txid": utxo["txid"],
            "vout": utxo["vout"],
            "script_pubkey": utxo["script_pubkey"],
            "sequence": 0xFFFFFFFF,
        }
        for utxo in selected
    ]
    outputs = [{"value_sat": output_sat, "script_pubkey": p2pkh_script_pubkey(address, ZHC)}]
    if service_output:
        outputs.append(service_output)
    rawtx = sign_utxo_transaction_with_key(inputs, outputs, private_key_wif, ZHC)
    return _built_result(
        rawtx=rawtx,
        selected=selected,
        input_total_sat=input_total_sat,
        target_sat=output_sat + actual_fee_sat + service_sat,
        fee_sat=actual_fee_sat,
        estimated_bytes=estimated_bytes,
        service_fee_sat=service_sat or None,
    )


def select_utxos(
    utxos: list[dict[str, Any]],
    target_sat: int,
    excluded_outpoints: Iterable[str] | None = None,
    min_confirmations: int = 1,
) -> tuple[list[dict[str, Any]], int]:
    excluded = {item.lower() for item in (excluded_outpoints or [])}
    candidates = [
        utxo
        for utxo in utxos
        if normalize_outpoint(utxo) not in excluded
        and is_spendable_utxo(utxo, min_confirmations)
    ]
    candidates.sort(key=lambda item: (int(item["value_sat"]), -int(item.get("confirmations", 0))))
    selected: list[dict[str, Any]] = []
    total = 0
    for utxo in candidates:
        selected.append(utxo)
        total += int(utxo["value_sat"])
        if total >= target_sat:
            return selected, total
    raise RuntimeError(f"not enough spendable UTXO: need {target_sat}, have {total}")


def build_client_side_payment(
    *,
    from_address: str,
    to_address: str,
    private_key_wif: str,
    amount: str,
    fee: str,
    utxos: list[dict[str, Any]],
    network: UtxoNetwork = ZHC,
    excluded_outpoints: Iterable[str] | None = None,
    min_confirmations: int = 1,
) -> dict[str, Any]:
    amount_sat = to_sats(amount)
    fee_sat = to_sats(fee)
    if amount_sat <= 0:
        raise ValueError("amount must be positive")
    if fee_sat <= 0:
        raise ValueError("fee must be positive")

    private_key = private_key_from_wif(private_key_wif, network)
    pubkey = compressed_pubkey(private_key)
    derived_address = address_from_pubkey(pubkey, network)
    if derived_address != from_address:
        raise ValueError(f"private key belongs to {derived_address}, not {from_address}")

    selected, input_total = select_utxos(
        utxos,
        amount_sat + fee_sat,
        excluded_outpoints=excluded_outpoints,
        min_confirmations=min_confirmations,
    )
    change_sat = input_total - amount_sat - fee_sat

    unsigned_inputs = [
        {
            "txid": utxo["txid"],
            "vout": int(utxo["vout"]),
            "script_pubkey": utxo["script_pubkey"],
            "sequence": 0xFFFFFFFF,
        }
        for utxo in selected
    ]
    outputs = [
        {
            "value_sat": amount_sat,
            "script_pubkey": p2pkh_script_pubkey(to_address, network),
        }
    ]
    if change_sat >= DUST_SAT:
        outputs.append(
            {
                "value_sat": change_sat,
                "script_pubkey": p2pkh_script_pubkey(from_address, network),
            }
        )
    else:
        # Dust change is left in the network fee.
        change_sat = 0

    rawtx = sign_p2pkh_transaction(unsigned_inputs, outputs, private_key_wif, network)
    fee_actual = input_total - sum(int(output["value_sat"]) for output in outputs)
    return {
        "rawtx": rawtx,
        "input_total_sat": input_total,
        "amount_sat": amount_sat,
        "fee_sat": fee_actual,
        "change_sat": change_sat,
        "selected_outpoints": [normalize_outpoint(utxo) for utxo in selected],
    }


def sign_gasfree_sender_sighash(
    private_key_wif: str,
    sighash_hex: str,
    network: UtxoNetwork = ZHC,
) -> dict[str, str]:
    digest = bytes.fromhex(sighash_hex)
    if len(digest) != 32:
        raise ValueError("gas-free sighash must be 32 bytes")
    private_key = private_key_from_wif(private_key_wif, network)
    pubkey = compressed_pubkey(private_key)
    der = sign_digest_der(private_key, digest)
    script_sig = push_data(der + bytes([SIGHASH_ALL])) + push_data(pubkey)
    return {
        "scriptSigHex": script_sig.hex(),
        "publicKeyHex": pubkey.hex(),
    }


PreflightFn = Callable[[str], dict[str, Any]]
BroadcastFn = Callable[[str], dict[str, Any]]
FetchUtxosFn = Callable[[], list[dict[str, Any]]]


def build_with_reselect(
    *,
    build_once: Callable[[list[dict[str, Any]], set[str]], dict[str, Any]],
    fetch_utxos: FetchUtxosFn,
    preflight: PreflightFn | None = None,
    broadcast: BroadcastFn | None = None,
    max_attempts: int = 5,
) -> dict[str, Any]:
    excluded: set[str] = set()
    last_reject: Any = None
    for attempt in range(1, max_attempts + 1):
        built = build_once(fetch_utxos(), excluded)
        rawtx = built["rawtx"]
        if preflight:
            check = preflight(rawtx)
            if not check.get("allowed", False):
                last_reject = check
                reason = str(check.get("reject-reason") or check.get("reason") or "")
                if (
                    "premature-spend-of-coinbase" in reason
                    or "bad-txns" in reason
                    or "missing-inputs" in reason
                    or "txn-mempool-conflict" in reason
                ):
                    excluded.update(str(item).lower() for item in built.get("selected_outpoints", []))
                    continue
                raise RuntimeError(f"preflight rejected rawtx: {reason or check}")
        if broadcast:
            sent = broadcast(rawtx)
            return {"status": "ok", "attempt": attempt, "built": built, "broadcast": sent}
        return {"status": "ok", "attempt": attempt, "built": built}
    raise RuntimeError(f"could not build accepted rawtx after {max_attempts} attempts: {last_reject}")


__all__ = [
    "ZHC",
    "UtxoNetwork",
    "address_from_pubkey",
    "build_client_side_payment",
    "build_with_reselect",
    "consolidate_utxos",
    "decode_wif",
    "from_sats",
    "p2pkh_script_pubkey",
    "private_key_from_wif",
    "select_utxos",
    "sign_gasfree_sender_sighash",
    "sign_utxo_transaction_with_key",
    "sign_p2pkh_transaction",
    "split_largest_utxo",
    "to_sats",
]

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from . import _rawtx_bridge  # noqa: F401
from zhc_rawtx.core import (  # type: ignore
    SIGHASH_ALL,
    ZHC,
    compressed_pubkey,
    decode_wif,
    private_key_from_wif,
    push_data,
    sign_digest_der,
)


def _sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _read_varint(raw: bytes, offset: int) -> tuple[int, int]:
    first = raw[offset]
    offset += 1
    if first < 0xFD:
        return first, offset
    if first == 0xFD:
        return int.from_bytes(raw[offset : offset + 2], "little"), offset + 2
    if first == 0xFE:
        return int.from_bytes(raw[offset : offset + 4], "little"), offset + 4
    return int.from_bytes(raw[offset : offset + 8], "little"), offset + 8


def _varint(value: int) -> bytes:
    if value < 0xFD:
        return bytes([value])
    if value <= 0xFFFF:
        return b"\xfd" + value.to_bytes(2, "little")
    if value <= 0xFFFFFFFF:
        return b"\xfe" + value.to_bytes(4, "little")
    return b"\xff" + value.to_bytes(8, "little")


def _push(data: bytes) -> bytes:
    return push_data(data)


@dataclass
class TxInput:
    prev_txid_le: bytes
    vout: int
    script_sig: bytes
    sequence: bytes


@dataclass
class ParsedTx:
    version: bytes
    inputs: list[TxInput]
    outputs_raw: bytes
    locktime: bytes


def _parse_raw_tx(raw_hex: str) -> ParsedTx:
    raw = bytes.fromhex(raw_hex)
    offset = 0
    version = raw[offset : offset + 4]
    offset += 4
    input_count, offset = _read_varint(raw, offset)
    inputs: list[TxInput] = []
    for _ in range(input_count):
        prev_txid_le = raw[offset : offset + 32]
        offset += 32
        vout = int.from_bytes(raw[offset : offset + 4], "little")
        offset += 4
        script_len, offset = _read_varint(raw, offset)
        script_sig = raw[offset : offset + script_len]
        offset += script_len
        sequence = raw[offset : offset + 4]
        offset += 4
        inputs.append(TxInput(prev_txid_le, vout, script_sig, sequence))

    outputs_start = offset
    output_count, offset = _read_varint(raw, offset)
    for _ in range(output_count):
        offset += 8
        script_len, offset = _read_varint(raw, offset)
        offset += script_len
    outputs_raw = raw[outputs_start:offset]
    locktime = raw[offset : offset + 4]
    return ParsedTx(version, inputs, outputs_raw, locktime)


def _serialize_for_sig(tx: ParsedTx, input_index: int, script_pub_key: bytes) -> bytes:
    result = bytearray(tx.version)
    result.extend(_varint(len(tx.inputs)))
    for index, txin in enumerate(tx.inputs):
        result.extend(txin.prev_txid_le)
        result.extend(txin.vout.to_bytes(4, "little"))
        script = script_pub_key if index == input_index else b""
        result.extend(_varint(len(script)))
        result.extend(script)
        result.extend(txin.sequence)
    result.extend(tx.outputs_raw)
    result.extend(tx.locktime)
    result.extend(SIGHASH_ALL.to_bytes(4, "little"))
    return bytes(result)


def _serialize_signed_tx(tx: ParsedTx) -> bytes:
    result = bytearray(tx.version)
    result.extend(_varint(len(tx.inputs)))
    for txin in tx.inputs:
        result.extend(txin.prev_txid_le)
        result.extend(txin.vout.to_bytes(4, "little"))
        result.extend(_varint(len(txin.script_sig)))
        result.extend(txin.script_sig)
        result.extend(txin.sequence)
    result.extend(tx.outputs_raw)
    result.extend(tx.locktime)
    return bytes(result)


def _decode_wif(wif: str) -> tuple[bytes, bool]:
    return decode_wif(wif, ZHC)


def _pubkey_from_private_key(private_key: bytes, compressed: bool) -> bytes:
    key = private_key_from_wif(_wif_from_secret(private_key, compressed), ZHC)
    if compressed:
        return compressed_pubkey(key)
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _sign_digest(private_key: bytes, digest: bytes) -> bytes:
    key = private_key_from_wif(_wif_from_secret(private_key, True), ZHC)
    return sign_digest_der(key, digest)


def _wif_from_secret(secret: bytes, compressed: bool) -> str:
    from zhc_rawtx.core import b58encode_check  # type: ignore

    payload = bytes([ZHC.wif]) + secret
    if compressed:
        payload += b"\x01"
    return b58encode_check(payload)


def _script_type(script_pub_key: bytes) -> str:
    if (
        len(script_pub_key) == 25
        and script_pub_key[:3] == bytes.fromhex("76a914")
        and script_pub_key[-2:] == bytes.fromhex("88ac")
    ):
        return "p2pkh"
    if len(script_pub_key) in (35, 67) and script_pub_key[-1:] == b"\xac":
        return "p2pk"
    raise ValueError(f"Unsupported input scriptPubKey: {script_pub_key.hex()}")


def sign_raw_transaction_with_key(
    raw_transaction: str,
    private_key_wif: str,
    utxos: list[dict[str, Any]],
) -> str:
    """Sign a non-witness ZHCash raw transaction locally.

    Supports P2PKH and P2PK UTXOs produced by the current wallet/ZHLink flows.
    Private keys stay in this Python process and are never sent to RPC.
    """

    tx = _parse_raw_tx(raw_transaction)
    if len(tx.inputs) != len(utxos):
        raise ValueError(
            f"UTXO count mismatch: tx has {len(tx.inputs)} inputs, got {len(utxos)}.",
        )

    private_key, compressed = _decode_wif(private_key_wif)
    pubkey = _pubkey_from_private_key(private_key, compressed)

    for index, utxo in enumerate(utxos):
        script_pub_key = bytes.fromhex(str(utxo.get("scriptPubKey") or ""))
        digest = _sha256d(_serialize_for_sig(tx, index, script_pub_key))
        signature = _sign_digest(private_key, digest) + bytes([SIGHASH_ALL])
        kind = _script_type(script_pub_key)
        if kind == "p2pkh":
            tx.inputs[index].script_sig = _push(signature) + _push(pubkey)
        elif kind == "p2pk":
            tx.inputs[index].script_sig = _push(signature)

    return _serialize_signed_tx(tx).hex()

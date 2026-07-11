from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from . import _rawtx_bridge  # noqa: F401
from zhc_rawtx.core import (  # type: ignore
    SECP256K1_ORDER,
    ZHC,
    address_from_pubkey,
    b58encode_check,
    compressed_pubkey,
    decode_wif,
    private_key_from_wif,
)


@dataclass
class WalletKey:
    address: str
    priv_key: str
    public_key_hex: str

    @property
    def private_key_wif(self) -> str:
        """Alias used by the beginner-friendly public API."""

        return self.priv_key


def _wif_from_private_number(number: int, compressed: bool = True) -> str:
    if number <= 0 or number >= SECP256K1_ORDER:
        raise ValueError("private key outside secp256k1 range")
    payload = bytes([ZHC.wif]) + number.to_bytes(32, "big")
    if compressed:
        payload += b"\x01"
    return b58encode_check(payload)


def _public_key(private_key: ec.EllipticCurvePrivateKey, compressed: bool) -> bytes:
    if compressed:
        return compressed_pubkey(private_key)
    return private_key.public_key().public_bytes(
        Encoding.X962,
        PublicFormat.UncompressedPoint,
    )


class BitcoinAddress:
    """ZHCASH address helper used internally by the public zhlink API."""

    def __init__(self, prefix: bytes = b"\x80", suffix: bytes = b"\x50"):
        if prefix != bytes([ZHC.wif]) or suffix != bytes([ZHC.pubkey_hash]):
            raise ValueError("BitcoinAddress now supports only ZHCASH network constants")
        self.private_key = ec.generate_private_key(ec.SECP256K1())

    def refresh_private_key(self) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256K1())

    def set_private_key_number(self, number: int) -> None:
        if number <= 0 or number >= SECP256K1_ORDER:
            raise ValueError("private key outside secp256k1 range")
        self.private_key = ec.derive_private_key(number, ec.SECP256K1())

    def generate_private_key_wif(self, compressed: bool = True) -> str:
        number = self.private_key.private_numbers().private_value
        return _wif_from_private_number(number, compressed=compressed)

    def generate_public_key(self, compressed: bool = True) -> bytes:
        return _public_key(self.private_key, compressed=compressed)

    def generate_address(self, compressed: bool = True) -> str:
        return address_from_pubkey(self.generate_public_key(compressed=compressed), ZHC)

    def address_from_wif(self, wif: str) -> str:
        _secret, compressed = decode_wif(wif, ZHC)
        self.private_key = private_key_from_wif(wif, ZHC)
        return self.generate_address(compressed=compressed)

    def get_address_and_private_key(self) -> dict[str, str]:
        self.refresh_private_key()
        pubkey = self.generate_public_key(compressed=True)
        return {
            "status": "ok",
            "address": address_from_pubkey(pubkey, ZHC),
            "priv_key": self.generate_private_key_wif(compressed=True),
            "public_key": pubkey.hex(),
        }


def create_wallet() -> WalletKey:
    result = BitcoinAddress().get_address_and_private_key()
    return WalletKey(
        address=result["address"],
        priv_key=result["priv_key"],
        public_key_hex=result["public_key"],
    )

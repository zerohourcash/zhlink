from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from . import _rawtx_bridge  # noqa: F401
from zhc_rawtx.core import (  # type: ignore
    SECP256K1_ORDER,
    ZHC,
    address_from_pubkey,
    b58encode_check,
    compressed_pubkey,
)


ZHC_DEFAULT_DERIVATION_BASE = "m/44'/0'/0'/0"
ZHC_DEFAULT_DERIVATION_PATH = f"{ZHC_DEFAULT_DERIVATION_BASE}/0"
DEFAULT_ZHC_SEED_CONFIG_PATH = Path(".zhlink-zhc-seed.json")
VALID_BIP39_WORD_COUNTS = (12, 24)


@dataclass(frozen=True)
class Bip39Wallet:
    mnemonic: str
    words: list[str]
    private_key_wif: str
    address: str
    derivation_path: str = ZHC_DEFAULT_DERIVATION_PATH


@dataclass(frozen=True)
class ZhcSeedConfig:
    mnemonic: str
    derivation_base: str = ZHC_DEFAULT_DERIVATION_BASE
    next_index: int = 0
    passphrase: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_WORDLIST_CACHE: list[str] | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_english_wordlist() -> list[str]:
    global _WORDLIST_CACHE
    if _WORDLIST_CACHE is not None:
        return _WORDLIST_CACHE

    candidates = []
    if os.environ.get("BIP39_ENGLISH_WORDLIST"):
        candidates.append(Path(os.environ["BIP39_ENGLISH_WORDLIST"]))
    candidates.append(Path(__file__).resolve().parent / "_vendor" / "bip39_english.txt")
    candidates.append(_repo_root() / "node_modules" / "@scure" / "bip39" / "wordlists" / "english.js")
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        text = candidate.read_text(encoding="utf-8")
        match = re.search(r"`([\s\S]*?)`", text)
        words = (match.group(1) if match else text).strip().splitlines()
        words = [word.strip() for word in words if word.strip()]
        if len(words) == 2048:
            _WORDLIST_CACHE = words
            return words

    try:
        from mnemonic import Mnemonic  # type: ignore

        words = Mnemonic("english").wordlist
        if len(words) == 2048:
            _WORDLIST_CACHE = list(words)
            return _WORDLIST_CACHE
    except Exception:
        pass

    raise RuntimeError(
        "BIP39 english wordlist not found. Install node dependencies, install "
        "`mnemonic`, or set BIP39_ENGLISH_WORDLIST."
    )


def normalize_mnemonic(mnemonic: str) -> str:
    return " ".join(mnemonic.strip().lower().split())


def _bytes_to_bits(raw: bytes) -> str:
    return "".join(f"{byte:08b}" for byte in raw)


def _bits_to_bytes(bits: str) -> bytes:
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


def generate_bip39_mnemonic(word_count: int = 12) -> str:
    if word_count not in VALID_BIP39_WORD_COUNTS:
        raise ValueError("word_count must be 12 or 24")
    entropy_bits = 128 if word_count == 12 else 256
    entropy = os.urandom(entropy_bits // 8)
    return mnemonic_from_entropy(entropy)


def mnemonic_from_entropy(entropy: bytes) -> str:
    if len(entropy) not in (16, 32):
        raise ValueError("entropy must be 16 bytes for 12 words or 32 bytes for 24 words")
    wordlist = _load_english_wordlist()
    entropy_bits = _bytes_to_bits(entropy)
    checksum_bits_len = len(entropy) * 8 // 32
    checksum_bits = _bytes_to_bits(hashlib.sha256(entropy).digest())[:checksum_bits_len]
    combined = entropy_bits + checksum_bits
    words = [wordlist[int(combined[index : index + 11], 2)] for index in range(0, len(combined), 11)]
    return " ".join(words)


def validate_bip39_mnemonic(mnemonic: str) -> bool:
    words = normalize_mnemonic(mnemonic).split()
    if len(words) not in VALID_BIP39_WORD_COUNTS:
        return False
    wordlist = _load_english_wordlist()
    indexes = []
    try:
        for word in words:
            indexes.append(wordlist.index(word))
    except ValueError:
        return False

    bits = "".join(f"{index:011b}" for index in indexes)
    checksum_bits_len = len(bits) // 33
    entropy_bits_len = len(bits) - checksum_bits_len
    entropy_bits = bits[:entropy_bits_len]
    checksum_bits = bits[entropy_bits_len:]
    entropy = _bits_to_bytes(entropy_bits)
    expected = _bytes_to_bits(hashlib.sha256(entropy).digest())[:checksum_bits_len]
    return checksum_bits == expected


def mnemonic_to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    normalized = normalize_mnemonic(mnemonic)
    return hashlib.pbkdf2_hmac(
        "sha512",
        normalized.encode("utf-8"),
        ("mnemonic" + passphrase).encode("utf-8"),
        2048,
        dklen=64,
    )


def _hmac_sha512(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha512).digest()


def _parse_path(path: str) -> list[int]:
    if not path or path == "m":
        return []
    if not path.startswith("m/"):
        raise ValueError("derivation path must start with m/")
    result = []
    for item in path[2:].split("/"):
        hardened = item.endswith("'") or item.endswith("h") or item.endswith("H")
        number_text = item[:-1] if hardened else item
        if not number_text.isdigit():
            raise ValueError(f"bad derivation path item: {item}")
        number = int(number_text)
        if number >= 0x80000000:
            raise ValueError(f"derivation index is too large: {item}")
        result.append(number + (0x80000000 if hardened else 0))
    return result


def _derive_private_key_bytes(seed: bytes, path: str) -> bytes:
    master = _hmac_sha512(b"Bitcoin seed", seed)
    key = master[:32]
    chain_code = master[32:]
    if int.from_bytes(key, "big") == 0 or int.from_bytes(key, "big") >= SECP256K1_ORDER:
        raise ValueError("invalid BIP32 master private key")

    for index in _parse_path(path):
        parent_number = int.from_bytes(key, "big")
        if index >= 0x80000000:
            data = b"\x00" + key + index.to_bytes(4, "big")
        else:
            parent_key = ec.derive_private_key(parent_number, ec.SECP256K1())
            data = compressed_pubkey(parent_key) + index.to_bytes(4, "big")
        digest = _hmac_sha512(chain_code, data)
        tweak = int.from_bytes(digest[:32], "big")
        if tweak >= SECP256K1_ORDER:
            raise ValueError("invalid BIP32 child tweak")
        child = (tweak + parent_number) % SECP256K1_ORDER
        if child == 0:
            raise ValueError("invalid BIP32 child private key")
        key = child.to_bytes(32, "big")
        chain_code = digest[32:]
    return key


def _wif_from_private_key_bytes(private_key: bytes, compressed: bool = True) -> str:
    payload = bytes([ZHC.wif]) + private_key
    if compressed:
        payload += b"\x01"
    return b58encode_check(payload)


def derive_bip39_zhc_wallet(
    mnemonic: str,
    *,
    passphrase: str = "",
    derivation_path: str = ZHC_DEFAULT_DERIVATION_PATH,
) -> Bip39Wallet:
    normalized = normalize_mnemonic(mnemonic)
    if not validate_bip39_mnemonic(normalized):
        raise ValueError("invalid BIP39 mnemonic")
    seed = mnemonic_to_seed(normalized, passphrase=passphrase)
    private_key = _derive_private_key_bytes(seed, derivation_path)
    key = ec.derive_private_key(int.from_bytes(private_key, "big"), ec.SECP256K1())
    pubkey = compressed_pubkey(key)
    return Bip39Wallet(
        mnemonic=normalized,
        words=normalized.split(),
        private_key_wif=_wif_from_private_key_bytes(private_key, compressed=True),
        address=address_from_pubkey(pubkey, ZHC),
        derivation_path=derivation_path,
    )


def derive_bip39_zhc_wallet_at_index(
    mnemonic: str,
    *,
    index: int = 0,
    passphrase: str = "",
    derivation_base: str = ZHC_DEFAULT_DERIVATION_BASE,
) -> Bip39Wallet:
    """Derive a deterministic native ZHCASH wallet by BIP39 index."""

    if index < 0:
        raise ValueError("index must be greater than or equal to zero")
    return derive_bip39_zhc_wallet(
        mnemonic,
        passphrase=passphrase,
        derivation_path=f"{derivation_base.rstrip('/')}/{int(index)}",
    )


def _normalize_zhc_seed_config(config: ZhcSeedConfig | dict[str, Any]) -> ZhcSeedConfig:
    if isinstance(config, ZhcSeedConfig):
        normalized = config
    else:
        normalized = ZhcSeedConfig(
            mnemonic=str(config.get("mnemonic", "")),
            derivation_base=str(config.get("derivation_base", ZHC_DEFAULT_DERIVATION_BASE)),
            next_index=int(config.get("next_index", 0)),
            passphrase=str(config.get("passphrase", "")),
        )
    mnemonic = normalize_mnemonic(normalized.mnemonic)
    if not validate_bip39_mnemonic(mnemonic):
        raise ValueError("invalid BIP39 mnemonic")
    if normalized.next_index < 0:
        raise ValueError("next_index must be greater than or equal to zero")
    return ZhcSeedConfig(
        mnemonic=mnemonic,
        derivation_base=normalized.derivation_base.rstrip("/"),
        next_index=int(normalized.next_index),
        passphrase=normalized.passphrase,
    )


def save_zhc_seed_config(
    config: ZhcSeedConfig | dict[str, Any],
    config_path: str | Path = DEFAULT_ZHC_SEED_CONFIG_PATH,
) -> ZhcSeedConfig:
    """Save a deterministic ZHCASH BIP39 seed config as local JSON.

    The config contains the seed phrase, so keep this file private. File mode is
    restricted to the current user on POSIX systems.
    """

    normalized = _normalize_zhc_seed_config(config)
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return normalized


def load_zhc_seed_config(
    config_path: str | Path = DEFAULT_ZHC_SEED_CONFIG_PATH,
) -> ZhcSeedConfig:
    path = Path(config_path)
    return _normalize_zhc_seed_config(json.loads(path.read_text(encoding="utf-8")))


def generate_bip39_zhc_seed_config(
    word_count: int = 12,
    *,
    passphrase: str = "",
    derivation_base: str = ZHC_DEFAULT_DERIVATION_BASE,
    config_path: str | Path = DEFAULT_ZHC_SEED_CONFIG_PATH,
    overwrite: bool = False,
) -> ZhcSeedConfig:
    """Generate and save a BIP39 seed config for indexed ZHCASH addresses."""

    path = Path(config_path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"seed config already exists: {path}")
    config = ZhcSeedConfig(
        mnemonic=generate_bip39_mnemonic(word_count),
        derivation_base=derivation_base.rstrip("/"),
        next_index=0,
        passphrase=passphrase,
    )
    return save_zhc_seed_config(config, path)


def derive_zhc_wallet_from_config(
    *,
    index: int,
    config_path: str | Path = DEFAULT_ZHC_SEED_CONFIG_PATH,
) -> Bip39Wallet:
    """Restore one deterministic ZHCASH wallet from config and index."""

    config = load_zhc_seed_config(config_path)
    return derive_bip39_zhc_wallet_at_index(
        config.mnemonic,
        index=index,
        passphrase=config.passphrase,
        derivation_base=config.derivation_base,
    )


def create_next_zhc_wallet_from_config(
    config_path: str | Path = DEFAULT_ZHC_SEED_CONFIG_PATH,
    *,
    increment: bool = True,
) -> Bip39Wallet:
    """Create the next indexed ZHCASH address from a saved BIP39 seed config."""

    path = Path(config_path)
    config = load_zhc_seed_config(path)
    wallet = derive_bip39_zhc_wallet_at_index(
        config.mnemonic,
        index=config.next_index,
        passphrase=config.passphrase,
        derivation_base=config.derivation_base,
    )
    if increment:
        save_zhc_seed_config(
            ZhcSeedConfig(
                mnemonic=config.mnemonic,
                derivation_base=config.derivation_base,
                next_index=config.next_index + 1,
                passphrase=config.passphrase,
            ),
            path,
        )
    return wallet


def generate_bip39_zhc_wallet(
    word_count: int = 12,
    *,
    passphrase: str = "",
    derivation_path: str = ZHC_DEFAULT_DERIVATION_PATH,
) -> Bip39Wallet:
    return derive_bip39_zhc_wallet(
        generate_bip39_mnemonic(word_count),
        passphrase=passphrase,
        derivation_path=derivation_path,
    )

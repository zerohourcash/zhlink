# pip install base58 ecdsa pycryptodome
import hashlib
import base58
import os
from ecdsa import SigningKey, SECP256k1
from binascii import hexlify
from Crypto.Hash import RIPEMD160
import time

# Utility Functions
def sha256(data):
    return hashlib.sha256(data).digest()

def ripemd160(data):
    ripemd = RIPEMD160.new()
    ripemd.update(data)
    return ripemd.digest()

def hash160(data):
    return ripemd160(sha256(data))

def checksum(data):
    return sha256(sha256(data))[:4]

# Generate Bitcoin Address
class BitcoinAddress:
    def __init__(self, prefix=b'\x80', suffix=b'\x50'):
        self.prefix = prefix
        self.suffix = suffix
        self.private_key = os.urandom(32)
        self.signing_key = SigningKey.from_string(self.private_key, curve=SECP256k1)
        self.verifying_key = self.signing_key.verifying_key

    def refresh_private_key(self):
        self.private_key = os.urandom(32)
        # print("Refreshing private key", self.private_key)
        self.signing_key = SigningKey.from_string(self.private_key, curve=SECP256k1)
        self.verifying_key = self.signing_key.verifying_key

    def set_private_key_number(self, number):
        self.private_key = number.to_bytes(32, byteorder='big', signed=False)
        self.signing_key = SigningKey.from_string(self.private_key, curve=SECP256k1)
        self.verifying_key = self.signing_key.verifying_key

    def generate_private_key_wif(self, compressed=False):
        key_with_prefix = self.prefix + self.private_key
        if compressed:
            key_with_prefix += b'\x01'
        return base58.b58encode(key_with_prefix + checksum(key_with_prefix)).decode('utf-8')

    def generate_public_key(self, compressed=False):
        vk = self.verifying_key
        if compressed:
            prefix = b'\x02' if vk.pubkey.point.y() % 2 == 0 else b'\x03'
            return prefix + vk.to_string()[:32]
        return b'\x04' + vk.to_string()

    def generate_address(self, compressed=False):
        public_key = self.generate_public_key(compressed=compressed)
        payload = self.suffix + hash160(public_key)
        return base58.b58encode(payload + checksum(payload)).decode('utf-8')

    def address_from_wif(self, wif):
        decoded_wif = base58.b58decode(wif)
        compressed = len(decoded_wif) == 38  # Check if it's compressed WIF
        private_key = decoded_wif[1:-5] if compressed else decoded_wif[1:-4]
        self.signing_key = SigningKey.from_string(private_key, curve=SECP256k1)
        self.verifying_key = self.signing_key.verifying_key
        return self.generate_address(compressed=compressed)

    def get_address_and_private_key(self):
        self.refresh_private_key()
        address = self.generate_address(compressed=True)
        priv_key = self.generate_private_key_wif(compressed=True)
        return {"status": "ok", "address": address, "priv_key": priv_key}


def main():
    btc_address = BitcoinAddress()
    print(btc_address.get_address_and_private_key())
    '''for i in range(5):
        print("Private Key (HEX):", hexlify(btc_address.private_key).decode('utf-8'))
        print("Private Key (WIF, compressed):", btc_address.generate_private_key_wif(compressed=True))
        print("Public Address (compressed):", btc_address.generate_address(compressed=True))

        # Example usage of address_from_wif

        wif_compressed = btc_address.generate_private_key_wif(compressed=True)
        print('приватный ключ', wif_compressed)
        regenerated_address = btc_address.address_from_wif(wif_compressed)
        print("Regenerated Public Address (compressed) from WIF:", regenerated_address)'''


if __name__ == "__main__":
    main()
    


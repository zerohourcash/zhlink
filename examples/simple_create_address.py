import json

from zhlink import create_address


wallet = create_address()

print(json.dumps({"address": wallet.address, "private_key_wif": wallet.priv_key}, indent=2))

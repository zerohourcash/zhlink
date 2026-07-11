import os

from zhlink import get_balance


address = os.environ.get("ZHLINK_ADDRESS", "")
if not address:
    raise SystemExit("Set ZHLINK_ADDRESS=Z...")

print(get_balance(address))


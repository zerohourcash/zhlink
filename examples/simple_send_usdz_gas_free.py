import os

from zhlink import send_usdz_gas_free


if os.environ.get("RUN_REAL_SEND") != "1":
    print("Refusing to send. Set RUN_REAL_SEND=1 intentionally.")
    raise SystemExit(0)

result = send_usdz_gas_free(
    sender_private_key_wif=os.environ["ZHLINK_SENDER_WIF"],
    admin_private_key_wif=os.environ["ZHLINK_ADMIN_GAS_WIF"],
    to_address=os.environ["ZHLINK_TO_ADDRESS"],
    amount=os.environ.get("ZHLINK_AMOUNT", "0.1"),
)

print(result)


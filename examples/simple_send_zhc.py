import os

from zhlink import send_zhc


if os.environ.get("RUN_REAL_SEND") != "1":
    print("Refusing to send. Set RUN_REAL_SEND=1 intentionally.")
    raise SystemExit(0)

result = send_zhc(
    private_key_wif=os.environ["ZHLINK_FROM_WIF"],
    to_address=os.environ["ZHLINK_TO_ADDRESS"],
    amount=os.environ.get("ZHLINK_AMOUNT", "1"),
)

print(result)


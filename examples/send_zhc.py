import os


def main() -> None:
    if os.environ.get("RUN_REAL_SEND") != "1":
        print("Refusing to send. Set RUN_REAL_SEND=1 intentionally.")
        return

    from zhlink import send_zhc

    to_address = os.environ.get("ZHLINK_TO_ADDRESS", "")
    private_key = os.environ.get("ZHLINK_FROM_WIF", "")
    amount = os.environ.get("ZHLINK_AMOUNT", "0")
    if not to_address or not private_key:
        raise SystemExit("Set ZHLINK_TO_ADDRESS and ZHLINK_FROM_WIF")

    result = send_zhc(private_key, to_address, amount)
    print(result)
    if result.get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

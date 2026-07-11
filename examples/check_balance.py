import os


def main() -> None:
    address = os.environ.get("ZHLINK_ADDRESS", "")
    if not address:
        raise SystemExit("Set ZHLINK_ADDRESS=Z...")

    from zhlink import get_balance

    print(get_balance(address))


if __name__ == "__main__":
    main()

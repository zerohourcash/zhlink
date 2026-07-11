FLAG_SEND_REAL_TX = True
PRIVATE_KEY_WIF = "L..."
TO_ADDRESS = "Z..."
AMOUNT_ZHC = "1"


def main() -> None:
    from zhlink import send_zhc

    if PRIVATE_KEY_WIF == "L..." or TO_ADDRESS == "Z...":
        print("Edit PRIVATE_KEY_WIF, TO_ADDRESS and AMOUNT_ZHC at the top of this file.")
        return

    if not FLAG_SEND_REAL_TX:
        print(
            {
                "dry_run": True,
                "asset": "ZHC",
                "to_address": TO_ADDRESS,
                "amount": AMOUNT_ZHC,
            }
        )
        return

    result = send_zhc(PRIVATE_KEY_WIF, TO_ADDRESS, AMOUNT_ZHC)
    print(result)
    if result.get("status") != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

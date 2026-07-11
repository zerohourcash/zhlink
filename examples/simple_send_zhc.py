from zhlink import send_zhc


FLAG_SEND_REAL_TX = True
PRIVATE_KEY_WIF = "L..."
TO_ADDRESS = "Z..."
AMOUNT_ZHC = "1"


if PRIVATE_KEY_WIF == "L..." or TO_ADDRESS == "Z...":
    print("Edit PRIVATE_KEY_WIF, TO_ADDRESS and AMOUNT_ZHC at the top of this file.")
    raise SystemExit(0)

if not FLAG_SEND_REAL_TX:
    print(
        {
            "dry_run": True,
            "asset": "ZHC",
            "to_address": TO_ADDRESS,
            "amount": AMOUNT_ZHC,
        }
    )
    raise SystemExit(0)

result = send_zhc(
    private_key_wif=PRIVATE_KEY_WIF,
    to_address=TO_ADDRESS,
    amount=AMOUNT_ZHC,
)

print(result)

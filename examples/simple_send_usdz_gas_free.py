from zhlink import send_usdz_gas_free


FLAG_SEND_REAL_TX = True
SENDER_PRIVATE_KEY_WIF = "L..."
ADMIN_GAS_PRIVATE_KEY_WIF = "K..."
TO_ADDRESS = "Z..."
AMOUNT_USDZ = "0.1"


if SENDER_PRIVATE_KEY_WIF == "L..." or ADMIN_GAS_PRIVATE_KEY_WIF == "K..." or TO_ADDRESS == "Z...":
    print("Edit SENDER_PRIVATE_KEY_WIF, ADMIN_GAS_PRIVATE_KEY_WIF, TO_ADDRESS and AMOUNT_USDZ at the top of this file.")
    raise SystemExit(0)

result = send_usdz_gas_free(
    sender_private_key_wif=SENDER_PRIVATE_KEY_WIF,
    admin_private_key_wif=ADMIN_GAS_PRIVATE_KEY_WIF,
    to_address=TO_ADDRESS,
    amount=AMOUNT_USDZ,
    broadcast=FLAG_SEND_REAL_TX,
)

print(result)

import json
import os

from zhlink import call_contract, send_to_contract


PRIVATE_KEY_WIF = os.getenv("ZHLINK_PRIVATE_KEY_WIF", "L...")
CONTRACT_ADDRESS = os.getenv("ZHLINK_CONTRACT_ADDRESS", "a48d0ee7365ce1add8e595de4d54344239f8ca28")
DATA_HEX = os.getenv("ZHLINK_CONTRACT_DATA_HEX", "")
AMOUNT_ZHC = os.getenv("ZHLINK_CONTRACT_AMOUNT_ZHC", "0")
GAS = int(os.getenv("ZHLINK_CONTRACT_GAS", "1000000"))


if os.getenv("RUN_REAL_SEND") != "1":
    print(
        "Refusing to send. Set RUN_REAL_SEND=1, ZHLINK_PRIVATE_KEY_WIF, "
        "ZHLINK_CONTRACT_ADDRESS and ZHLINK_CONTRACT_DATA_HEX to broadcast."
    )
    raise SystemExit(0)

dry_run = call_contract(
    CONTRACT_ADDRESS,
    DATA_HEX,
    gas=GAS,
)
print(json.dumps({"dry_run": dry_run}, ensure_ascii=False, indent=2, default=str))

result = send_to_contract(
    private_key_wif=PRIVATE_KEY_WIF,
    contract_address=CONTRACT_ADDRESS,
    data_hex=DATA_HEX,
    amount=AMOUNT_ZHC,
    gas=GAS,
)

print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

import json
from decimal import Decimal

from zhlink import call_contract, send_to_contract


FLAG_SEND_REAL_TX = True
PRIVATE_KEY_WIF = "L..."
CONTRACT_ADDRESS = "a48d0ee7365ce1add8e595de4d54344239f8ca28"
DATA_HEX = ""
AMOUNT_ZHC = Decimal("0")
GAS = 1_000_000


if PRIVATE_KEY_WIF == "L..." or not CONTRACT_ADDRESS:
    print("Edit PRIVATE_KEY_WIF, CONTRACT_ADDRESS, DATA_HEX, AMOUNT_ZHC and GAS at the top of this file.")
    raise SystemExit(0)

dry_run = call_contract(
    CONTRACT_ADDRESS,
    DATA_HEX,
    gas=GAS,
)
print(json.dumps({"dry_run": dry_run}, ensure_ascii=False, indent=2, default=str))

if not FLAG_SEND_REAL_TX:
    raise SystemExit(0)

result = send_to_contract(
    private_key_wif=PRIVATE_KEY_WIF,
    contract_address=CONTRACT_ADDRESS,
    data_hex=DATA_HEX,
    amount=str(AMOUNT_ZHC),
    gas=GAS,
)

print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

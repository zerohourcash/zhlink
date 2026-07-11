import json
from pathlib import Path

from zhlink import estimate_mass_send, load_mass_send_plan, send_mass


FLAG_SEND_REAL_TX = True
PLAN_PATH = Path(__file__).with_name("mass_send.json")
PRIVATE_KEY_WIF = "L..."


plan = load_mass_send_plan(PLAN_PATH)

if PRIVATE_KEY_WIF == "L...":
    print("Edit PRIVATE_KEY_WIF and PLAN_PATH at the top of this file.")
    print(json.dumps({"plan": plan.asset, "recipient_count": plan.required_tx_count}, indent=2))
    raise SystemExit(0)

estimate = estimate_mass_send(PRIVATE_KEY_WIF, plan)
print(json.dumps({"estimate": estimate}, ensure_ascii=False, indent=2, default=str))

if not FLAG_SEND_REAL_TX:
    raise SystemExit(0)

result = send_mass(PRIVATE_KEY_WIF, plan)
print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

import json
import os
from pathlib import Path

from zhlink import estimate_mass_send, load_mass_send_plan, send_mass


PLAN_PATH = Path(os.getenv("ZHLINK_MASS_SEND_PLAN", Path(__file__).with_name("mass_send.json")))
PRIVATE_KEY_WIF = os.getenv("ZHLINK_PRIVATE_KEY_WIF", "L...")


plan = load_mass_send_plan(PLAN_PATH)

if os.getenv("RUN_REAL_SEND") != "1":
    print("Refusing to send. Set RUN_REAL_SEND=1 and ZHLINK_PRIVATE_KEY_WIF to broadcast.")
    print(json.dumps({"plan": plan.asset, "recipient_count": plan.required_tx_count}, indent=2))
    raise SystemExit(0)

estimate = estimate_mass_send(PRIVATE_KEY_WIF, plan)
print(json.dumps({"estimate": estimate}, ensure_ascii=False, indent=2, default=str))

result = send_mass(PRIVATE_KEY_WIF, plan)
print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

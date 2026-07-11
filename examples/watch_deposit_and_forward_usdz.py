from decimal import Decimal
from pathlib import Path

from zhlink import UsdzReceiverConfig, create_and_forward_usdz_deposit


CONFIG = UsdzReceiverConfig(
    admin_address="ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi",
    admin_gas_wif="K...",
    min_usdz=Decimal("0.00000001"),
    send_real_tx=True,
    wait_timeout_seconds=3600,
    gasfree_store_path=Path(__file__).resolve().parents[1] / ".zhlink-gasfree-utxos.json",
)


if CONFIG.admin_gas_wif == "K...":
    print("Edit admin_gas_wif in CONFIG at the top of this file.")
else:
    print(create_and_forward_usdz_deposit(CONFIG))

from decimal import Decimal
from pathlib import Path

from zhlink import UsdzReceiverConfig, run_usdz_receiver


ACTION = "status"  # "status", "new", "delete", or "serve"
DELETE_ADDRESS = ""

CONFIG = UsdzReceiverConfig(
    admin_address="ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi",
    admin_gas_wif="K...",
    min_usdz=Decimal("0.00000001"),
    send_real_tx=True,
    delete_after_forward=False,
    db_path=Path(__file__).resolve().parents[1] / ".zhlink-usdz-receiver.sqlite3",
    gasfree_store_path=Path(__file__).resolve().parents[1] / ".zhlink-gasfree-utxos.json",
)


run_usdz_receiver(action=ACTION, config=CONFIG, delete_address=DELETE_ADDRESS)

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV = {
    "PYTHONDONTWRITEBYTECODE": "1",
}

ALWAYS_RUN = [
    "create_wallet.py",
    "create_bip39_wallet.py",
    "simple_create_address.py",
    "check_balance.py",
    "simple_balance.py",
]

GUARDED_SEND_EXAMPLES = [
    "send_zhc.py",
    "mass_send.py",
    "send_to_contract.py",
    "simple_send_zhc.py",
    "simple_send_usdz_gas_free.py",
    "watch_deposit_and_forward_usdz.py",
    "usdz_receiver_service.py",
]


def run_example(name: str) -> None:
    script = ROOT / "examples" / name
    print(f"\n--- {name} ---")
    subprocess.run([sys.executable, str(script)], check=True, env=ENV)


def main() -> None:
    for name in ALWAYS_RUN:
        run_example(name)

    for name in GUARDED_SEND_EXAMPLES:
        run_example(name)

    print("\nAll examples were visited. Send examples use FLAG_SEND_REAL_TX in each script.")


if __name__ == "__main__":
    main()

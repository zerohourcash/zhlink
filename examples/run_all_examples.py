import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV = {
    **os.environ,
    "PYTHONPATH": str(ROOT),
    "PYTHONDONTWRITEBYTECODE": "1",
}

ALWAYS_RUN = [
    "create_wallet.py",
    "create_bip39_wallet.py",
    "simple_create_address.py",
]

OPTIONAL_ENV_EXAMPLES = {
    "check_balance.py": ["ZHLINK_ADDRESS"],
    "simple_balance.py": ["ZHLINK_ADDRESS"],
}

GUARDED_SEND_EXAMPLES = [
    "send_zhc.py",
    "simple_send_zhc.py",
    "simple_send_usdz_gas_free.py",
]


def run_example(name: str) -> None:
    script = ROOT / "examples" / name
    print(f"\n--- {name} ---")
    subprocess.run([sys.executable, str(script)], check=True, env=ENV)


def main() -> None:
    for name in ALWAYS_RUN:
        run_example(name)

    for name, required_env in OPTIONAL_ENV_EXAMPLES.items():
        missing = [key for key in required_env if not os.environ.get(key)]
        if missing:
            print(f"\n--- {name} ---")
            print(f"Skipped: set {', '.join(missing)} to run this example.")
            continue
        run_example(name)

    for name in GUARDED_SEND_EXAMPLES:
        run_example(name)

    print("\nAll examples were visited. Live sends stay disabled unless RUN_REAL_SEND=1 is set.")


if __name__ == "__main__":
    main()

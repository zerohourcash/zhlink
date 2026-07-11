from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import (
    ZHC,
    build_client_side_payment,
    sign_gasfree_sender_sighash,
)


def read_json(path: str) -> object:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def cmd_payment(args: argparse.Namespace) -> int:
    utxos = read_json(args.utxos)
    if not isinstance(utxos, list):
        raise SystemExit("UTXO file must contain a JSON array.")
    result = build_client_side_payment(
        network=ZHC,
        from_address=args.from_address,
        to_address=args.to_address,
        private_key_wif=args.wif,
        amount=args.amount,
        fee=args.fee,
        utxos=utxos,
        excluded_outpoints=args.exclude or [],
        min_confirmations=args.min_confirmations,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_gasfree_sign(args: argparse.Namespace) -> int:
    result = sign_gasfree_sender_sighash(
        private_key_wif=args.wif,
        sighash_hex=args.sighash,
        network=ZHC,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhc-rawtx")
    sub = parser.add_subparsers(dest="command", required=True)

    payment = sub.add_parser("payment", help="Build and sign native P2PKH payment")
    payment.add_argument("--from", dest="from_address", required=True)
    payment.add_argument("--to", dest="to_address", required=True)
    payment.add_argument("--wif", required=True)
    payment.add_argument("--amount", required=True)
    payment.add_argument("--fee", required=True)
    payment.add_argument("--utxos", required=True, help="JSON file path or '-'")
    payment.add_argument("--exclude", action="append", default=[])
    payment.add_argument("--min-confirmations", type=int, default=1)
    payment.set_defaults(func=cmd_payment)

    gasfree = sub.add_parser(
        "gasfree-sign",
        help="Sign server-provided gas-free USDZ sighash",
    )
    gasfree.add_argument("--wif", required=True)
    gasfree.add_argument("--sighash", required=True)
    gasfree.set_defaults(func=cmd_gasfree_sign)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

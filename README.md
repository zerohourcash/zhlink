# ZHLink Python Library

`zhlink` is a self-contained Python library for ZHCASH.

The public API is intentionally small:

- create a new ZHC address;
- check ZHC + USDZ balance;
- send native ZHC from a private key;
- send USDZ with admin-paid ZHC gas;
- optionally read extra ZRC-20 token balances.

The raw transaction engine is bundled inside the package. Normal users only
import `zhlink`.

## Install

Recommended Python:

- Python `3.10` or newer is required.
- Python `3.10` and `3.11` are the safest choices for deployment.
- Python `3.12` should work.
- Python `3.9` and older are not supported.

From PyPI:

```bash
pip install zhlink
```

From this folder:

```bash
cd /root/wallet/zhlink
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

For direct local use without install:

```bash
cd /root/wallet/zhlink
export PYTHONPATH=/root/wallet/zhlink
```

## Create Address

```python
from zhlink import create_address

wallet = create_address()

print(wallet.address)
print(wallet.priv_key)
```

Private keys are generated locally. The library never sends private keys to
ZeroScan or RPC.

## Get Balance

By default `get_balance` returns ZHC and USDZ.

```python
from zhlink import get_balance

balance = get_balance("Z...")

print(balance["zhc"])
print(balance["usdz"])
```

Extra ZRC-20 tokens can be requested when needed:

```python
from zhlink import get_balance

balance = get_balance(
    "Z...",
    tokens={"EDS": "dc71958156a945d3071374521e1a7a42f5ba8038"},
)

print(balance["tokens"]["EDS"])
```

## Send ZHC

```python
from zhlink import send_zhc

result = send_zhc(
    private_key_wif="L...",
    to_address="Z...",
    amount="1.25",
)

print(result)
```

The sender address is derived from the private key automatically.

## Send USDZ Gas-Free

`send_usdz_gas_free` signs the USDZ transfer with the sender key and pays ZHC
gas from the admin gas wallet.

```python
from zhlink import send_usdz_gas_free

result = send_usdz_gas_free(
    sender_private_key_wif="L...",
    admin_private_key_wif="K...",
    to_address="Z...",
    amount="0.1",
)

print(result["broadcast"]["txid"])
```

The function does the required checks automatically:

1. derives sender/admin addresses from WIF keys;
2. checks sender USDZ balance;
3. runs `callcontract` dry-run;
4. loads admin gas UTXO from ZeroScan;
5. skips locally used gas UTXO;
6. builds and signs rawtx locally;
7. runs `testmempoolaccept`;
8. if one UTXO is rejected, tries another UTXO;
9. broadcasts through ZeroScan;
10. falls back to RPC `sendrawtransaction`.

For mass sends, prepare independent admin gas UTXO first. One gas-free transfer
needs one spendable admin gas UTXO. The recommended ticket size is `0.5 ZHC`.

```python
from zhlink import admin_gas_wallet_info

info = admin_gas_wallet_info("K...")
print(info["suitable_gas_utxo_count"])
print(info["recommended_split_count"])
```

## Custom RPC / ZeroScan

Defaults:

- ZeroScan API: `https://ws.zeroscan.st`, `https://ws.zeroscan.io`
- RPC: `https://rpc.zeroscan.st`
- USDZ contract: `a48d0ee7365ce1add8e595de4d54344239f8ca28`

Override them when needed:

```python
from zhlink import ZHLinkConfig, get_balance

config = ZHLinkConfig.public_network(
    zeroscan_endpoints=("https://ws.zeroscan.st", "https://ws.zeroscan.io"),
    public_rpc_urls=("https://rpc.zeroscan.st", "https://my-node.example/rpc"),
)

print(get_balance("Z...", config=config))
```

## Examples

Run examples one by one:

```bash
cd /root/wallet/zhlink
PYTHONPATH=. python3 examples/create_wallet.py
PYTHONPATH=. python3 examples/create_bip39_wallet.py
PYTHONPATH=. ZHLINK_ADDRESS="Z..." python3 examples/check_balance.py
```

Run all safe examples at once:

```bash
cd /root/wallet/zhlink
PYTHONPATH=. python3 examples/run_all_examples.py
```

Send examples are guarded by `RUN_REAL_SEND=1` and will not broadcast by
accident. Never commit real private keys.

## Publishing

GitHub Actions workflow `.github/workflows/python-publish.yml` builds, tests,
checks, and publishes the package to PyPI.

Release flow:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The workflow uses PyPI Trusted Publishing, so the PyPI project must allow this
GitHub repository/workflow as a trusted publisher.

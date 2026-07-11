# ZHLink Python Library

[![PyPI version](https://img.shields.io/pypi/v/zhlink.svg)](https://pypi.org/project/zhlink/)

`zhlink` is a self-contained Python library for ZHCASH.

The public API is intentionally small:

- create a new ZHC address;
- check ZHC + USDZ balance;
- send native ZHC from a private key;
- dry-run/read ZHCASH smart contracts with `callcontract`;
- send payable ZHCASH smart-contract calls from a private key;
- send ZHC, USDZ or any ZRC-20 token to many recipients from a JSON plan;
- send USDZ with admin-paid ZHC gas;
- optionally read extra ZRC-20 token balances.

The library is async-first for long-running wallet apps: WSS is used as the
primary realtime signal and automatically falls back to HTTP/RPC polling when
WSS is unavailable. Synchronous wrappers are still provided for simple scripts.

The raw transaction engine is bundled inside the package. Normal users only
import `zhlink`.

## Install

Recommended Python:

- Python `3.10` or newer is required.
- Python `3.10` and `3.11` are the safest choices for deployment.
- Python `3.12` should work.
- Python `3.9` and older are not supported.

### Install With pip

Create a virtual environment and install from PyPI:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install zhlink
```

### Install With uv

Into the current Python environment:

```bash
uv pip install zhlink
```

Inside a `uv` project with `pyproject.toml`:

```bash
uv add zhlink
```

Run a one-off script without manually creating a venv:

```bash
uv run --with zhlink python your_script.py
```

### Install Latest From GitHub

Latest `main` branch:

```bash
pip install git+https://github.com/zerohourcash/zhlink.git
```

With `uv`:

```bash
uv pip install git+https://github.com/zerohourcash/zhlink.git
```

Inside a `uv` project:

```bash
uv add git+https://github.com/zerohourcash/zhlink.git
```

### Verify Install

```bash
python - <<'PY'
import zhlink

wallet = zhlink.create_address()
print(wallet.address)
print(zhlink.get_mass_send_template("usdz")["asset"])
PY
```

### Install From Source

From this repository folder:

```bash
cd /root/wallet/zhlink
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
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
print(balance.get("confirmed_zhc"))
print(balance.get("pending_zhc"))
print(balance["usdz"])
```

`zhc` is the visible balance. When ZeroScan exposes pending data, positive
0-confirmation change is included in `zhc`, while `confirmed_zhc` and
`pending_zhc` are also returned separately. Sending still uses only confirmed
spendable UTXO.

Extra ZRC-20 tokens can be requested when needed:

```python
from zhlink import get_balance

balance = get_balance(
    "Z...",
    tokens={"EDS": "dc71958156a945d3071374521e1a7a42f5ba8038"},
)

print(balance["tokens"]["EDS"])
```

## Cached Balance and WSS Updates

`zhlink` stores public balance snapshots in a small local SQLite cache. Private
keys are never stored there.

```python
from zhlink import force_refresh_balance, get_cached_balance

cached = get_cached_balance("Z...")
fresh = force_refresh_balance("Z...")  # throttled, default: once per 10 sec
```

For a Python wallet UI, use the async client and subscribe to address updates:

```python
import asyncio
from zhlink import watch_balance

async def main():
    async for balance in watch_balance("Z..."):
        print(balance["address"], balance["zhc"], balance["usdz"])

asyncio.run(main())
```

For one-shot scripts, use the sync wrappers:

```python
from zhlink import get_balance, send_zhc

print(get_balance("Z..."))
```

Realtime detection uses the best available channel:

1. one shared WSS connection from configured `block_ws_urls`;
2. ZeroScan `/info` polling if WSS is interrupted;
3. public RPC `getblockcount` fallback if ZeroScan is unavailable.

The default primary endpoint is:

```text
wss://ws.zeroscan.st/ws
```

`zhlink` treats WSS as the primary realtime signal and multiplexes block and
address subscriptions over one shared WebSocket hub per asyncio event loop.
Subscribing 100 addresses does not create 100 sockets; it sends address
subscriptions through the same connection and refreshes only addresses that
receive realtime transaction events.

Address subscriptions have a default lifetime of one hour. Every `getbalance`
call or balance subscription touch extends that lifetime. If an address is not
requested again, `zhlink` sends `unsubscribe` for that address so long-running
wallet processes do not keep stale subscriptions forever. For long-lived wallet
sessions the TTL can be raised, for example to 12 hours:

```python
from zhlink import ZHLinkConfig

config = ZHLinkConfig.public_network(
    address_subscription_ttl_seconds=12 * 60 * 60,
    ws_max_failures=3,
    ws_cooldown_seconds=60,
)
```

WSS is an accelerator, not a hard dependency. If all WSS endpoints fail several
times, the WSS hub enters a short cooldown and the watcher keeps working through
block polling, ZeroScan HTTP and public RPC fallback. When the cooldown expires,
WSS reconnects automatically.

Normal `get_balance()` reads SQLite when the cached snapshot is still valid.
Use `force_refresh_balance()` when the user explicitly presses refresh; the
library will refuse to refresh the same address more often than once per
`force_refresh_seconds` seconds.

UTXO snapshots are cached in the same SQLite file. If a ZeroScan endpoint hangs
or RPC is temporarily unavailable, the library can still read the last known
UTXO set and show useful wallet state. Real sends still use the normal safety
pipeline: local UTXO reservation, dry-run when available, ZeroScan broadcast,
then RPC broadcast fallback.

### Watch a USDZ deposit and forward it gas-free

The one-shot helper below creates a fresh USDZ receiving address, watches it
through the shared WSS hub, and forwards the detected USDZ to the admin address
with `send_usdz_gas_free()`. Heavy watcher/RPC logic stays inside the library.

```python
from decimal import Decimal
from zhlink import UsdzReceiverConfig, create_and_forward_usdz_deposit

config = UsdzReceiverConfig(
    admin_address="ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi",
    admin_gas_wif="K...",
    min_usdz=Decimal("0.00000001"),
    send_real_tx=True,
)

result = create_and_forward_usdz_deposit(config)
print(result)
```

With `send_real_tx=True`, the helper broadcasts the real forwarding transaction.
With `send_real_tx=False`, it only builds/preflights and does not broadcast.

For a production receiver, keep the workflow linear: create one deposit address
for a real request, wait for confirmed USDZ, then forward it gas-free. Heavy
work is handled inside the library: SQLite state, WSS watching, balance checks,
gas-free forwarding, and optional cleanup.

Public receiver controls:

- `UsdzReceiverConfig` - one config object for admin wallet, limits, storage and realtime settings;
- `create_and_forward_usdz_deposit(config)` - one-shot flow: create address, wait for USDZ, forward gas-free;
- `wait_for_usdz_deposit(address, config)` - wait until a stored receiver address receives enough USDZ;
- `forward_usdz_deposit(address, amount, config)` - forward a detected USDZ deposit with admin-paid gas;
- `run_usdz_receiver(action="new" | "delete" | "serve", ...)` - optional managed receiver runner;
- `create_usdz_receiver_address(config)` - create one receiver address on demand;
- `delete_usdz_receiver_address(address, config)` - remove a receiver address and its private key from local state;
- `usdz_receiver_status(config)` - inspect receiver storage and active addresses.

For debugging, pass `debug=True` or `event_callback=...` in the config. The
callback receives plain dictionaries such as `receiver_created`,
`receiver_payment_mempool`, `receiver_payment_accepted`,
`receiver_forward_start`, `receiver_forward_ok`, and `receiver_forward_error`.

```python
from decimal import Decimal
from pathlib import Path
from pprint import pprint
from zhlink import (
    UsdzReceiverConfig,
    create_usdz_receiver_address,
    forward_usdz_deposit,
    wait_for_usdz_deposit,
)

def on_event(event):
    pprint(event)

config = UsdzReceiverConfig(
    admin_address="ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi",
    admin_gas_wif="K...",
    min_usdz=Decimal("0.00000001"),
    send_real_tx=True,
    delete_after_forward=False,
    db_path=Path(".zhlink-usdz-receiver.sqlite3"),
    debug=True,
    event_callback=on_event,
)

receiver = create_usdz_receiver_address(config)
print("Send USDZ to:", receiver["address"])

amount = wait_for_usdz_deposit(receiver["address"], config)
result = forward_usdz_deposit(receiver["address"], amount, config)
print(result)
```

The standalone production example follows the same flow from start to finish:

```bash
python examples/usdz_receiver_service.py
```

It creates a receiver address, waits for USDZ, forwards it, and then prints the
forwarding result. Address deletion is intentionally left as a commented line in
the example, so your application can decide whether to keep or remove receiver
history after a successful forward.

For daemon-style applications, create addresses on explicit user requests and
then start the service loop:

```python
run_usdz_receiver(action="serve", config=config)
```

Delete a receiver address after use:

```python
run_usdz_receiver(action="delete", config=config, delete_address="Z...")
```

`examples/usdz_receiver_service.py` is production-oriented by default:

```python
FLAG_SEND_REAL_TX = True
```

With this flag set to `True`, the service builds, preflights, and broadcasts the
real gas-free USDZ forwarding transaction. Set it to `False` only when you need
a local dry-run/preflight without broadcasting. All production variables are
constants at the top of the example file.

The service does not pre-generate addresses. It only watches addresses that were
created by explicit `new` requests.

When a transaction touching the receiver address appears before confirmation,
the service prints:

```text
payment in mempool: Z... txid
```

When USDZ is visible in confirmed contract state, the service prints:

```text
payment accepted: Z... 1.00000000 USDZ
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

If the previous transaction already used the only large UTXO and the visible
balance now mostly comes from 0-confirmation change, `send_zhc` does not build
a conflicting transaction. It returns:

```python
{
    "status": "error",
    "action_required": "wait_next_block",
    "reason": "Wait for the next block before sending again. ...",
    "diagnostics": {...},
}
```

Handle this by waiting for the next ZHCASH block, then call `send_zhc` again.
This prevents accidental double-spend attempts and confusing mempool errors.

## Smart Contracts

Use `call_contract` before any contract transaction. It calls ZHCASH
`callcontract` through configured public RPC endpoints and returns the raw
execution result plus common fields.

```python
from zhlink import call_contract

result = call_contract(
    contract_address="a48d0ee7365ce1add8e595de4d54344239f8ca28",
    data_hex="70a08231...",
    from_address="Z...",  # optional
    gas=1_000_000,
)

print(result["output"])
```

Use `send_to_contract` for a real payable contract call.

```python
from zhlink import send_to_contract

result = send_to_contract(
    private_key_wif="L...",
    contract_address="...",
    data_hex="...",
    amount="0",        # ZHC sent to the contract, if needed
    gas=1_000_000,
)

print(result)
```

The sender address is derived locally. The function runs `callcontract`
preflight, selects UTXO, signs locally, checks mempool when RPC is available,
broadcasts through ZeroScan and falls back to RPC broadcast.

`send_to_contract` uses the same UTXO reservation rule as `send_zhc`: if the
only suitable gas UTXO is already reserved by a pending local transaction, it
returns `action_required: "wait_next_block"` instead of sending a broken
contract transaction.

## Mass Send

`send_mass` sends ZHC, USDZ or any ZRC-20 token to many recipients from a JSON
plan.

Bundled templates are available directly from the package:

```python
from zhlink import get_mass_send_template, write_mass_send_template

print(get_mass_send_template("usdz"))
write_mass_send_template("usdz", "mass_send.json")
write_mass_send_template("zhc", "mass_send_zhc.json")
write_mass_send_template("zrc20", "mass_send_zrc20.json")
```

```json
{
  "asset": "USDZ",
  "token_contract": "a48d0ee7365ce1add8e595de4d54344239f8ca28",
  "token_decimals": 8,
  "gas": 1000000,
  "recipients": [
    {"address": "Z...", "amount": "0.1"},
    {"address": "Z...", "amount": "0.2"}
  ]
}
```

For native ZHC:

```json
{
  "asset": "ZHC",
  "recipients": [
    {"address": "Z...", "amount": "1"}
  ]
}
```

Run an estimate first:

```python
from zhlink import estimate_mass_send, load_mass_send_plan

plan = load_mass_send_plan("mass_send.json")
print(estimate_mass_send("L...", plan))
```

Then send:

```python
from zhlink import send_mass

result = send_mass("L...", "mass_send.json")
print(result["sent_count"], result["failed_count"])
```

Mass-send UTXO rules:

1. One recipient uses one on-chain transaction.
2. The library never starts more transactions in one batch than confirmed UTXO
   currently available.
3. If there are too few UTXO and `auto_prepare_utxos=True`, the library splits
   the largest UTXO into up to `100` self-outputs.
4. Every prepared output must be at least `1 ZHC`.
5. After a reorg/split, the library waits for the next block before mailing.
6. Between mailing batches it waits for the next block so change outputs become
   confirmed before being reused.

Manual preparation is also available:

```python
from zhlink import prepare_mass_send_utxos

prepare_mass_send_utxos("L...", "mass_send.json", target_utxos=100)
```

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
python3 examples/create_wallet.py
python3 examples/create_bip39_wallet.py
python3 examples/check_balance.py
python3 examples/send_to_contract.py
python3 examples/mass_send.py
```

Each example is a standalone file. Edit the constants at the top of the script,
then run it without extra command-line or environment parameters. Send examples
use:

```python
FLAG_SEND_REAL_TX = True
```

When the flag is `True`, the script performs the real transaction path. When the
flag is `False`, it performs only the available dry-run/preflight/estimate path.

Run all safe examples at once:

```bash
cd /root/wallet/zhlink
python3 examples/run_all_examples.py
```

Never commit real private keys.

## Publishing

GitHub Actions workflow `.github/workflows/python-publish.yml` builds, tests,
checks, and publishes the package to PyPI.

Current package version: `0.1.20`

Release flow:

1. Make sure `pyproject.toml` contains the version you want to publish.
2. Sync the README version line.
3. Create and push a tag from that version:

```bash
python3 scripts/sync_readme_version.py
VERSION=$(grep -m1 '^version = ' pyproject.toml | cut -d '"' -f2)
git tag "v$VERSION"
git push origin "v$VERSION"
```

The workflow uses PyPI Trusted Publishing. The PyPI project must allow this
GitHub repository and workflow as a trusted publisher.

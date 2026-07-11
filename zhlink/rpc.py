# zhlink_lite.py
import asyncio
import contextlib
import json
import logging
import re
import traceback
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from .address import BitcoinAddress
from .cache import SQLiteBalanceCache
from .config import ZHLinkConfig
from .errors import WAIT_NEXT_BLOCK_MESSAGE, WaitNextBlockError
from .realtime import ZeroScanWebSocketHub, get_realtime_hub
from .signer import sign_raw_transaction_with_key
from .zeroscan import ZeroScanRPC

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

SATOSHIS_PER_ZHC = Decimal("100000000")
MIN_RECOMMENDED_FEE = Decimal("0.1")
# Dust threshold for ZHC outputs.  Must be above the node's dust limit,
# which is ~0.0022 ZHC when relayfee is 0.004 ZHC/kB.
DUST_CHANGE = Decimal("0.003")
DEFAULT_FEE_RATE_ZHC_PER_KB = Decimal("0.004")
# Theoretical upper bound: total fee for a ZHC transaction should never exceed 1 ZHC.
# Per-kB rate is capped at 1 ZHC/kB so that typical small transactions stay cheap
# (e.g. ~0.3 ZHC for a 300-byte gift transfer) while the absolute ceiling still
# protects against unexpectedly large transactions.
MAX_FEE_RATE_ZHC_PER_KB = Decimal("1")
MAX_TOTAL_FEE_ZHC = Decimal("1")
P2PKH_INPUT_BYTES = 148
P2PK_INPUT_BYTES = 114
P2PKH_OUTPUT_BYTES = 34
CONTRACT_OUTPUT_BYTES = 120
TX_OVERHEAD_BYTES = 10
PRIVATE_KEY_RPC_METHODS = {"signrawtransactionwithkey"}
MAX_BNB_COINS = 48
MAX_BNB_TRIES = 100_000
KNAPSACK_PASSES = 1_000
BalanceCallback = Callable[[str, Dict[str, Any]], Any | Awaitable[Any]]
def _as_decimal(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def _to_sats(value: Any) -> int:
    amount = _as_decimal(value)
    return int((amount * SATOSHIS_PER_ZHC).to_integral_value(rounding=ROUND_HALF_UP))


def _from_sats(value: int) -> Decimal:
    return (Decimal(int(value)) / SATOSHIS_PER_ZHC).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def _is_p2pkh(script_hex: str) -> bool:
    script = (script_hex or "").lower()
    return script.startswith("76a914") and script.endswith("88ac")


def _is_p2pk(script_hex: str) -> bool:
    script = (script_hex or "").lower()
    return len(script) == 70 and script.startswith("21") and script.endswith("ac")


def _script_type(script_hex: str) -> Optional[str]:
    if _is_p2pkh(script_hex):
        return "p2pkh"
    if _is_p2pk(script_hex):
        return "p2pk"
    return None


def _input_bytes(utxo: Dict[str, Any]) -> int:
    return P2PK_INPUT_BYTES if utxo.get("script_type") == "p2pk" else P2PKH_INPUT_BYTES


def _utxo_outpoint(utxo: Dict[str, Any]) -> str:
    return f"{str(utxo.get('txid', '')).lower()}:{int(utxo.get('vout', 0))}"


def _selection_total(coins: List[Dict[str, Any]]) -> int:
    return sum(int(coin.get("value_sat", _to_sats(coin["amount"]))) for coin in coins)


def _spendable_total_sat(utxos: List[Dict[str, Any]]) -> int:
    return sum(int(utxo.get("value_sat", _to_sats(utxo["amount"]))) for utxo in utxos)


def _compare_selection(
    current: Optional[List[Dict[str, Any]]],
    candidate: List[Dict[str, Any]],
    target_sat: int,
) -> List[Dict[str, Any]]:
    if current is None:
        return candidate
    current_waste = _selection_total(current) - target_sat
    candidate_waste = _selection_total(candidate) - target_sat
    if candidate_waste != current_waste:
        return candidate if candidate_waste < current_waste else current
    if len(candidate) != len(current):
        return candidate if len(candidate) < len(current) else current
    return candidate


def _select_coins_bnb(
    coins: List[Dict[str, Any]],
    target_sat: int,
    cost_of_change_sat: int,
) -> Optional[List[Dict[str, Any]]]:
    sorted_coins = sorted(
        coins,
        key=lambda coin: (
            int(coin.get("value_sat", _to_sats(coin["amount"]))),
            int(coin.get("confirmations", 0)),
        ),
        reverse=True,
    )
    suffix = [0] * (len(sorted_coins) + 1)
    for index in range(len(sorted_coins) - 1, -1, -1):
        suffix[index] = suffix[index + 1] + int(sorted_coins[index].get("value_sat", _to_sats(sorted_coins[index]["amount"])))

    upper_target = target_sat + cost_of_change_sat
    best: Optional[List[Dict[str, Any]]] = None
    tries = 0

    def walk(index: int, total: int, selected: List[Dict[str, Any]]) -> None:
        nonlocal best, tries
        tries += 1
        if tries > MAX_BNB_TRIES:
            return
        if total >= target_sat:
            if total <= upper_target:
                best = _compare_selection(best, selected, target_sat)
            return
        if index >= len(sorted_coins) or total + suffix[index] < target_sat:
            return

        coin = sorted_coins[index]
        walk(index + 1, total + int(coin.get("value_sat", _to_sats(coin["amount"]))), selected + [coin])
        walk(index + 1, total, selected)

    walk(0, 0, [])
    return best


def _deterministic_shuffle(items: List[Dict[str, Any]], seed: int) -> List[Dict[str, Any]]:
    shuffled = list(items)
    state = seed or 1
    for index in range(len(shuffled) - 1, 0, -1):
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        swap_index = state % (index + 1)
        shuffled[index], shuffled[swap_index] = shuffled[swap_index], shuffled[index]
    return shuffled


def _approximate_best_subset(
    coins: List[Dict[str, Any]],
    target_sat: int,
) -> Optional[List[Dict[str, Any]]]:
    best: Optional[List[Dict[str, Any]]] = None
    for pass_index in range(KNAPSACK_PASSES):
        if pass_index == 0:
            ordered = sorted(
                coins,
                key=lambda coin: (
                    int(coin.get("value_sat", _to_sats(coin["amount"]))),
                    int(coin.get("confirmations", 0)),
                ),
                reverse=True,
            )
        else:
            ordered = _deterministic_shuffle(coins, pass_index)

        selected: List[Dict[str, Any]] = []
        total = 0
        for coin in ordered:
            if total >= target_sat:
                break
            selected.append(coin)
            total += int(coin.get("value_sat", _to_sats(coin["amount"])))

        if total >= target_sat:
            best = _compare_selection(best, selected, target_sat)
    return best


def _select_coins_knapsack(
    coins: List[Dict[str, Any]],
    target_sat: int,
) -> Optional[List[Dict[str, Any]]]:
    lowest_larger: Optional[Dict[str, Any]] = None
    smaller: List[Dict[str, Any]] = []

    for coin in coins:
        value_sat = int(coin.get("value_sat", _to_sats(coin["amount"])))
        if value_sat == target_sat:
            return [coin]
        if value_sat > target_sat:
            if lowest_larger is None or value_sat < int(lowest_larger.get("value_sat", _to_sats(lowest_larger["amount"]))):
                lowest_larger = coin
        else:
            smaller.append(coin)

    smaller_total = _selection_total(smaller)
    if smaller_total == target_sat:
        return smaller
    if smaller_total < target_sat:
        return [lowest_larger] if lowest_larger else None

    subset = _approximate_best_subset(smaller, target_sat)
    if subset is None:
        return [lowest_larger] if lowest_larger else None
    if lowest_larger is None:
        return subset
    return _compare_selection([lowest_larger], subset, target_sat)


def _select_optimal_coins(
    coins: List[Dict[str, Any]],
    target_sat: int,
    cost_of_change_sat: int,
) -> Optional[List[Dict[str, Any]]]:
    if len(coins) <= MAX_BNB_COINS:
        bnb = _select_coins_bnb(coins, target_sat, cost_of_change_sat)
        if bnb:
            return bnb
    return _select_coins_knapsack(coins, target_sat)


def _output_amount_sum(outputs: Dict[str, Any]) -> Decimal:
    total = Decimal("0")
    for amount in outputs.values():
        if isinstance(amount, (float, int, str, Decimal)):
            total += _as_decimal(amount)
        elif isinstance(amount, dict) and "contract" in amount:
            total += _as_decimal(amount["contract"].get("amount", "0"))
        elif isinstance(amount, dict) and "amount" in amount:
            total += _as_decimal(amount.get("amount", "0"))
    return total


def _address_hex_arg(address_hex: str) -> str:
    return str(address_hex).lower().rjust(64, "0")


def _build_zrc20_transfer_data(to_hex: str, token_units: int) -> str:
    return f"a9059cbb{_address_hex_arg(to_hex)}{format(int(token_units), '064x')}"


def _build_zrc20_transfer_from_data(from_hex: str, to_hex: str, token_units: int) -> str:
    return (
        "23b872dd"
        f"{_address_hex_arg(from_hex)}"
        f"{_address_hex_arg(to_hex)}"
        f"{format(int(token_units), '064x')}"
    )


class ZHCashRPC:
    def __init__(self, config: Optional[ZHLinkConfig] = None):
        self.config = config or ZHLinkConfig()
        self.zhc_address = BitcoinAddress()
        self.sensitive_keywords = []
        self.admin_address = self.config.admin_address
        self.admin_fee = self.config.admin_fee
        self.extra_fee = self.config.extra_fee
        self.usdz_contract = self.config.usdz_contract
        self.gasfree_admin_private_key = self.config.gasfree_admin_private_key
        self.public_rpc_urls = list(self.config.public_rpc_urls)
        self.block_ws_urls = list(self.config.block_ws_urls)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(self.config.timeout_seconds))
        self.zero_rpc = ZeroScanRPC(
            endpoints=self.config.zeroscan_endpoints,
            timeout=min(float(self.config.timeout_seconds), 10.0),
        )
        self.reserved_utxos: Dict[str, int] = {}
        self.reservation_height: Optional[int] = None
        self.balance_cache: Dict[str, Dict[str, Any]] = {}
        self.sqlite_cache = SQLiteBalanceCache(self.config.cache_path)
        self.last_block_height: Optional[int] = None
        self.last_block_hash: str = ""
        self.block_ws_active_url: Optional[str] = None
        self.block_ws_task: Optional[asyncio.Task] = None
        self.block_poll_task: Optional[asyncio.Task] = None
        self.realtime_hub: Optional[ZeroScanWebSocketHub] = None
        self.realtime_unsubscribers: List[Callable[[], None]] = []
        self.realtime_address_unsubscribers: Dict[str, Callable[[], None]] = {}
        self.utxo_lock = asyncio.Lock()
        self.scan_lock = asyncio.Lock()  # Лок для предотвращения повторного вызова
        self.balance_subscriptions: Dict[str, List[BalanceCallback]] = {}

    async def close(self):
        await self.stop_block_watch()
        await self.client.aclose()
        await self.zero_rpc.close()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(self.config.timeout_seconds))

    def _remember_block_tip(self, payload: Dict[str, Any]) -> bool:
        height = int(payload.get("height") or payload.get("h") or 0)
        if height <= 0:
            return False
        block_hash = str(payload.get("hash") or payload.get("blockHash") or "")
        if height == getattr(self, "last_block_height", None) and (not block_hash or block_hash == getattr(self, "last_block_hash", "")):
            return False
        self.last_block_height = height
        self.last_block_hash = block_hash
        self.reservation_height = height
        if not hasattr(self, "reserved_utxos"):
            self.reserved_utxos = {}
        if not hasattr(self, "balance_cache"):
            self.balance_cache = {}
        self.reserved_utxos.clear()
        self.balance_cache.clear()
        self.sqlite_cache.set_last_block_height(height)
        logger.info("New ZHCash block from websocket: %s %s", height, block_hash)
        return True

    async def _handle_realtime_block(self, payload: Dict[str, Any]) -> None:
        self._remember_block_tip(payload)

    async def _handle_realtime_address(self, payload: Dict[str, Any]) -> None:
        address = str(payload.get("address") or "").strip()
        if not address:
            return
        height = int(
            payload.get("height")
            or payload.get("h")
            or payload.get("payload", {}).get("height")
            or 0
        )
        try:
            snapshot = await self.getbalance(address, force_refresh=True)
            snapshot["realtime"] = True
            snapshot["event"] = payload
        except Exception as exc:
            snapshot = {
                "status": "error",
                "reason": str(exc),
                "height": height or self.last_block_height,
                "address": address,
                "event": payload,
            }
        await self._notify_balance_subscribers(address, snapshot)

    async def _refresh_subscribed_balances(self, height: int) -> None:
        for address, callbacks in list(getattr(self, "balance_subscriptions", {}).items()):
            try:
                snapshot = await self.getbalance(address, force_refresh=True)
            except Exception as exc:
                snapshot = {
                    "status": "error",
                    "reason": str(exc),
                    "height": height,
                    "address": address,
                }
            for callback in list(callbacks):
                try:
                    result = callback(address, snapshot)
                    if hasattr(result, "__await__"):
                        await result
                except Exception as exc:
                    logger.warning("Balance subscriber failed for %s: %s", address, exc)

    async def _notify_balance_subscribers(self, address: str, snapshot: Dict[str, Any]) -> None:
        callbacks = list(getattr(self, "balance_subscriptions", {}).get(address, ()))
        for callback in callbacks:
            try:
                result = callback(address, snapshot)
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.warning("Balance subscriber failed for %s: %s", address, exc)

    def subscribe_balance(self, address: str, callback: BalanceCallback) -> Callable[[], None]:
        """Subscribe to cached balance updates for one address.

        Call ``start_block_watch()`` to receive WSS-driven updates. The returned
        function unsubscribes the callback.
        """

        callbacks = self.balance_subscriptions.setdefault(address, [])
        callbacks.append(callback)
        if self.realtime_hub and address not in self.realtime_address_unsubscribers:
            self.realtime_address_unsubscribers[address] = self.realtime_hub.add_address_callback(
                address,
                self._handle_realtime_address,
            )

        def unsubscribe() -> None:
            current = self.balance_subscriptions.get(address, [])
            with contextlib.suppress(ValueError):
                current.remove(callback)
            if not current:
                self.balance_subscriptions.pop(address, None)
                address_unsub = self.realtime_address_unsubscribers.pop(address, None)
                if address_unsub:
                    address_unsub()

        return unsubscribe

    async def start_block_watch(self) -> bool:
        if self.realtime_hub and (self.block_poll_task and not self.block_poll_task.done()):
            return True
        self.realtime_hub = get_realtime_hub(
            tuple(self.block_ws_urls),
            self.config.address_subscription_ttl_seconds,
        )
        self.realtime_unsubscribers.append(
            self.realtime_hub.add_block_callback(self._handle_realtime_block)
        )
        for address in list(self.balance_subscriptions):
            if address not in self.realtime_address_unsubscribers:
                self.realtime_address_unsubscribers[address] = (
                    self.realtime_hub.add_address_callback(address, self._handle_realtime_address)
                )
        if not self.block_poll_task or self.block_poll_task.done():
            self.block_poll_task = asyncio.create_task(self._block_poll_loop())
        return True

    async def stop_block_watch(self) -> None:
        for unsubscribe in list(self.realtime_unsubscribers):
            unsubscribe()
        self.realtime_unsubscribers.clear()
        for unsubscribe in list(self.realtime_address_unsubscribers.values()):
            unsubscribe()
        self.realtime_address_unsubscribers.clear()
        self.realtime_hub = None
        for task in (self.block_ws_task, self.block_poll_task):
            if not task:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.block_ws_task = None
        self.block_poll_task = None

    async def _block_poll_loop(self) -> None:
        interval = max(5.0, float(self.config.block_poll_seconds))
        while True:
            try:
                try:
                    height = int(await self.zero_rpc.get_block_height())
                except Exception as zeroscan_error:
                    logger.warning("ZeroScan block polling failed, trying RPC: %s", zeroscan_error)
                    height = int(await self.getblockcount())
                self._remember_block_tip({"height": height, "source": "poll"})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Block polling failed: %s", exc)
            await asyncio.sleep(interval)

    async def _block_ws_loop(self, aiohttp_module: Any) -> None:
        delay = 1.0
        while True:
            any_connected = False
            for url in self.block_ws_urls:
                try:
                    await self._consume_block_ws(aiohttp_module, url)
                    any_connected = True
                    delay = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Block websocket failed for %s: %s", url, exc)
                    self.block_ws_active_url = None
            if not any_connected:
                logger.warning("All block websocket endpoints failed; RPC block polling remains active.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _consume_block_ws(self, aiohttp_module: Any, url: str) -> None:
        timeout = aiohttp_module.ClientTimeout(total=None, connect=10, sock_read=90)
        async with aiohttp_module.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(url, heartbeat=30) as ws:
                self.block_ws_active_url = url
                await ws.send_json({"type": "subscribe", "channel": "blocks"})
                async for msg in ws:
                    if msg.type == aiohttp_module.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                        except Exception:
                            continue
                        if data.get("type") == "block":
                            self._remember_block_tip(data)
                    elif msg.type in (aiohttp_module.WSMsgType.CLOSED, aiohttp_module.WSMsgType.ERROR):
                        break

    async def call_rpc(self, method: str, params: Optional[List[Any]] = None) -> Any:
        return await self.call_rpc_any(method, params)

    async def call_public_rpc(self, url: str, method: str, params: Optional[List[Any]] = None) -> Any:
        if params is None:
            params = []
        payload = {
            "method": method,
            "params": params,
            "jsonrpc": "2.0",
            "id": 0,
        }
        response = await self.client.post(
            url,
            data=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        response.raise_for_status()
        result = response.json()
        if result.get("error") is not None:
            raise RuntimeError(f"RPC Error from {url}: {result['error']}")
        return result.get("result", None)

    async def call_rpc_any(self, method: str, params: Optional[List[Any]] = None) -> Any:
        if method in PRIVATE_KEY_RPC_METHODS:
            raise RuntimeError(
                f"{method} is disabled in public-RPC mode. "
                "Private keys must be signed locally by a Python signer.",
            )
        errors = []
        for url in self.public_rpc_urls:
            try:
                return await self.call_public_rpc(url, method, params)
            except Exception as exc:
                errors.append(f"{url}={exc}")
        raise RuntimeError(f"No RPC endpoint responded for {method}: {'; '.join(errors)}")



    async def getblockcount(self) -> Any:
        return await self.call_rpc_any("getblockcount")

    async def gettransactionreceipt(self, tx_hash: str) -> Any:
        return await self.call_rpc_any("gettransactionreceipt", [tx_hash])

    async def gettransaction(self, txid: str, verbose: bool = True) -> Any:
        """Fetch a raw transaction by txid.

        Uses ``getrawtransaction`` because ZHCash nodes do not expose a
        wallet-scoped ``gettransaction`` for arbitrary txids.
        """
        params = [txid]
        if verbose:
            params.append(1)
        return await self.call_rpc_any("getrawtransaction", params)

    async def gettxout(self, txid: str, n: int, include_mempool: bool = True) -> Any:
        res = await self.call_rpc_any("gettxout", [txid, n, include_mempool])
        return {"coinstake": True} if res is None else res

    async def gettxoutproof(self, txids: List[str], blockhash: Optional[str] = None) -> Any:
        params = [txids]
        if blockhash:
            params.append(blockhash)
        return await self.call_rpc_any("gettxoutproof", params)

    async def scantxoutset(self, address: str) -> Any:
        scan_object = {"desc": f"addr({address})", "range": 1000}
        params = ["start", [scan_object]]
        async with self.scan_lock:  # Блокировка выполнения параллельных вызовов
            return await self.call_rpc_any("scantxoutset", params)



    async def gettxoutsetinfo(self) -> Any:
        return await self.call_rpc_any("gettxoutsetinfo")

    async def fromhexaddress(self, hex_address: str) -> str:
        return await self.call_rpc("fromhexaddress", [hex_address])

    def hexify_number(self, number: int) -> str:
        return format(number, '064x')

    async def get_finish_hex_address(self, address: str) -> str:
        hex_address = await self.gethexaddress(address)
        return hex_address.zfill(64)

    async def getnewaddress(self) -> str:
        res = self.zhc_address.get_address_and_private_key()
        # return res
        try:
            isvalid = await self.validateaddress(res['address'])
            if isvalid:
                return res
            else:
                return {'status': "error", "reason": 'Non valid address. Try again', 'data': res}

        except Exception as e:
            traceback.print_exc()
            return {'status': "error", "reason": str(e), 'data': res}

    async def gethexaddress(self, address: str) -> str:
        return await self.call_rpc("gethexaddress", [address])

    async def decodescript(self, script_hex: str) -> Any:
        return await self.call_rpc_any("decodescript", [script_hex])

    async def validateaddress(self, address: str) -> bool:
        return (await self.call_rpc("validateaddress", [address]))['isvalid']

    async def decoderawtransaction(self, hexstring: str, iswitness: Optional[bool] = None) -> Any:
        params = [hexstring]
        if iswitness is not None:
            params.append(iswitness)
        return await self.call_rpc_any("decoderawtransaction", params)

    def normalize_utxos(self, utxos: Any) -> List[Dict[str, Any]]:
        if isinstance(utxos, dict):
            if utxos.get("status") == "error":
                return []
            utxos = utxos.get("unspents") or utxos.get("utxos") or []
        normalized = []
        for utxo in utxos or []:
            script_value = utxo.get("scriptPubKey")
            if isinstance(script_value, dict):
                script_value = script_value.get("hex", "")
            script_pub_key = (
                script_value
                or utxo.get("scriptPubKeyHex")
                or ""
            )
            script_type = _script_type(script_pub_key)
            txid = utxo.get("transactionId") or utxo.get("txid")
            vout = utxo.get("outputIndex", utxo.get("vout"))
            confirmations = int(utxo.get("confirmations") or 0)
            is_stake = bool(utxo.get("isStake") or utxo.get("coinstake"))
            if not script_type or not txid or vout is None:
                continue
            if not re.fullmatch(r"[0-9a-fA-F]{64}", str(txid)):
                continue
            if is_stake and confirmations <= 500:
                continue
            if "value" in utxo:
                amount = (Decimal(str(utxo["value"])) / SATOSHIS_PER_ZHC).quantize(
                    Decimal("0.00000001"),
                    rounding=ROUND_HALF_UP,
                )
            elif "amount" in utxo:
                amount = _as_decimal(utxo["amount"])
            else:
                continue
            if amount <= 0:
                continue
            normalized.append(
                {
                    "amount": amount,
                    "txid": str(txid),
                    "vout": int(vout),
                    "value_sat": _to_sats(amount),
                    "scriptPubKey": script_pub_key,
                    "confirmations": confirmations,
                    "isStake": is_stake,
                    "script_type": script_type,
                }
            )
        return normalized

    async def _filter_rpc_utxos(self, utxos: Any) -> List[Dict[str, Any]]:
        normalized = self.normalize_utxos(utxos)
        result = []
        for utxo in normalized:
            try:
                txout = await self.gettxout(utxo["txid"], utxo["vout"])
                if txout.get("coinstake") and utxo.get("confirmations", 0) <= 500:
                    continue
            except Exception:
                continue
            result.append(utxo)
        return result

    async def get_utxos(self, address: str) -> Any:
        try:
            utxos = await self.zero_rpc.get_utxos(address)
            normalized = self.normalize_utxos(utxos)
            if normalized:
                self.sqlite_cache.put_utxos(
                    address,
                    normalized,
                    getattr(self, "last_block_height", None) or self.sqlite_cache.get_last_block_height(),
                )
                return normalized
        except Exception as e:
            logger.warning("ZeroScan UTXO lookup failed, falling back to RPC: %s", e)

        try:
            res = await self.scantxoutset(address)
            normalized = await self._filter_rpc_utxos(res.get('unspents', []))
            if normalized:
                self.sqlite_cache.put_utxos(
                    address,
                    normalized,
                    getattr(self, "last_block_height", None) or self.sqlite_cache.get_last_block_height(),
                )
                return normalized
        except Exception as e:
            logger.warning("RPC UTXO lookup failed, trying SQLite cache: %s", e)

        cached = self.sqlite_cache.get_utxos(address)
        if cached:
            for utxo in cached:
                utxo["cached"] = True
            return cached
        return []

    def calculate_tx_weight(self, vin_count: int, vout_count: int = 3) -> int:
        avg_input_size = 148
        avg_output_size = 50
        base_size = 4 + 1 + vin_count * avg_input_size + 1 + vout_count * avg_output_size + 4
        return base_size * 3 + base_size

    async def get_min_fee(self) -> Decimal:
        try:
            fee_response = await self.call_rpc('estimatesmartfee', [10])
            fee_rate = Decimal(str(fee_response.get('feerate') or DEFAULT_FEE_RATE_ZHC_PER_KB))
            if fee_rate <= 0:
                return DEFAULT_FEE_RATE_ZHC_PER_KB
            if fee_rate > MAX_FEE_RATE_ZHC_PER_KB:
                return MAX_FEE_RATE_ZHC_PER_KB
            return fee_rate
        except Exception:
            return DEFAULT_FEE_RATE_ZHC_PER_KB

    def estimate_tx_bytes(self, utxos: List[Dict[str, Any]], vout_count: int = 3, contract_outputs: int = 0) -> int:
        return (
            TX_OVERHEAD_BYTES
            + sum(_input_bytes(utxo) for utxo in utxos)
            + (vout_count * P2PKH_OUTPUT_BYTES)
            + (contract_outputs * CONTRACT_OUTPUT_BYTES)
        )

    async def calculate_fee_from_fee_rate(self, vin_count: int, utxos: Optional[List[Dict[str, Any]]] = None, vout_count: int = 3, contract_outputs: int = 0) -> Decimal:
        fee_rate = await self.get_min_fee()
        tx_size = Decimal(self.estimate_tx_bytes(utxos or [{} for _ in range(vin_count)], vout_count, contract_outputs))
        calculated_fee = (fee_rate * tx_size / Decimal('1000')).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)
        # Enforce the theoretical total-fee ceiling regardless of transaction size.
        return min(max(calculated_fee, MIN_RECOMMENDED_FEE), MAX_TOTAL_FEE_ZHC)

    async def _current_block_height_for_reservations(self) -> Optional[int]:
        if getattr(self, "last_block_height", None):
            return int(self.last_block_height)
        try:
            height = int(await self.zero_rpc.get_block_height())
            if height > 0:
                return height
        except Exception as exc:
            logger.warning("Could not read block height from ZeroScan: %s", exc)
        try:
            return int(await self.getblockcount())
        except Exception as exc:
            logger.warning("Could not read block height for UTXO reservations: %s", exc)
            return None

    async def _clear_reservations_on_new_block(self) -> None:
        if not hasattr(self, "reserved_utxos"):
            self.reserved_utxos = {}
        height = await self._current_block_height_for_reservations()
        if height is None:
            return
        if getattr(self, "reservation_height", None) is None:
            self.reservation_height = height
            return
        if height > self.reservation_height:
            self.reserved_utxos.clear()
            self.reservation_height = height

    def _reserve_utxos(self, utxos: List[Dict[str, Any]]) -> None:
        if not hasattr(self, "reserved_utxos"):
            self.reserved_utxos = {}
        height = self.reservation_height or 0
        for utxo in utxos:
            self.reserved_utxos[_utxo_outpoint(utxo)] = height

    def _release_utxos(self, utxos: List[Dict[str, Any]]) -> None:
        if not hasattr(self, "reserved_utxos"):
            return
        for utxo in utxos:
            self.reserved_utxos.pop(_utxo_outpoint(utxo), None)

    def _select_utxos_inner(
        self,
        utxos: List[Dict[str, Any]],
        min_fee: Decimal,
        amount: Decimal,
        admin_fee: Optional[Decimal] = None,
        extra_fee: Optional[Decimal] = None,
    ) -> List[Dict[str, Any]]:
        reserved = set(getattr(self, "reserved_utxos", {}).keys())
        normalized_utxos = self.normalize_utxos(utxos)
        eligible_utxos = [
            utxo for utxo in normalized_utxos if _utxo_outpoint(utxo) not in reserved
        ]
        if not eligible_utxos:
            reserved_count = len(normalized_utxos) - len(eligible_utxos)
            if normalized_utxos and reserved_count > 0:
                raise WaitNextBlockError(
                    {
                        "action_required": "wait_next_block",
                        "reason": "all_spendable_utxos_reserved",
                        "fetched_utxos": len(utxos),
                        "spendable_utxos": len(normalized_utxos),
                        "reserved_utxos": reserved_count,
                        "available_utxos": 0,
                        "spendable_zhc_before_reservations": str(_from_sats(_spendable_total_sat(normalized_utxos))),
                        "available_zhc_after_reservations": "0.00000000",
                    }
                )
            raise ValueError(f"No suitable UTXOs available. Total UTXOs: {len(utxos)}")

        min_fee = max(_as_decimal(min_fee), MIN_RECOMMENDED_FEE)
        admin_fee = _as_decimal(self.admin_fee if admin_fee is None else admin_fee)
        extra_fee = _as_decimal(self.extra_fee if extra_fee is None else extra_fee)
        required = _as_decimal(amount) + min_fee + admin_fee + extra_fee
        target_sat = _to_sats(required)
        spendable_sat = _spendable_total_sat(normalized_utxos)
        available_sat = _spendable_total_sat(eligible_utxos)
        if spendable_sat >= target_sat and available_sat < target_sat:
            raise WaitNextBlockError(
                {
                    "action_required": "wait_next_block",
                    "reason": "reserved_utxo_required_for_new_transaction",
                    "fetched_utxos": len(utxos),
                    "spendable_utxos": len(normalized_utxos),
                    "reserved_utxos": len(normalized_utxos) - len(eligible_utxos),
                    "available_utxos": len(eligible_utxos),
                    "required_zhc": str(required),
                    "spendable_zhc_before_reservations": str(_from_sats(spendable_sat)),
                    "available_zhc_after_reservations": str(_from_sats(available_sat)),
                }
            )
        selected = _select_optimal_coins(eligible_utxos, target_sat, _to_sats(DUST_CHANGE))
        if not selected:
            total_available = sum(_as_decimal(utxo["amount"]) for utxo in eligible_utxos)
            raise ValueError(f"Insufficient funds. Total: {total_available}, Required: {required} min_fee {min_fee} admin_fee {admin_fee} extra_fee {extra_fee} amount {amount} lenUTXO {len(eligible_utxos)}")
        return selected

    async def select_utxos(
        self,
        utxos: List[Dict[str, Any]],
        min_fee: Decimal,
        amount: Decimal,
        reserve: bool = False,
        admin_fee: Optional[Decimal] = None,
        extra_fee: Optional[Decimal] = None,
    ) -> List[Dict[str, Any]]:
        lock = getattr(self, "utxo_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self.utxo_lock = lock
        async with lock:
            await self._clear_reservations_on_new_block()
            selected = self._select_utxos_inner(utxos, min_fee, amount, admin_fee, extra_fee)
            if reserve:
                self._reserve_utxos(selected)
            return selected

    async def create_raw_transaction(self, utxos: List[Dict[str, Any]], outputs: Dict[str, Any], min_fee: Decimal, change_address: str) -> str:
        tx_outputs = dict(outputs)
        inputs = [{"txid": utxo['txid'], "vout": utxo['vout']} for utxo in utxos]
        total_input = sum(_as_decimal(utxo['amount']) for utxo in utxos)
        total_output = _output_amount_sum(tx_outputs)
        min_fee = max(_as_decimal(min_fee), MIN_RECOMMENDED_FEE)
        change = (total_input - total_output - min_fee).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)
        if change < 0:
            raise ValueError(f"Selected UTXO do not cover outputs and fee. input={total_input} output={total_output} fee={min_fee}")
        if change >= DUST_CHANGE:
            if change_address in tx_outputs:
                tx_outputs[change_address] = str(_as_decimal(tx_outputs[change_address]) + change)
            else:
                tx_outputs[change_address] = str(change)
        try:
            raw_tx = await self.call_rpc_any('createrawtransaction', [inputs, tx_outputs])
            if not isinstance(raw_tx, str):
                raise ValueError(f"createrawtransaction returned unexpected response: {raw_tx}")
            await self.decoderawtransaction(raw_tx)
            return raw_tx
        except Exception as e:
            logger.error("createrawtransaction failed: %s", e)
            raise

    async def sign_transaction(
        self,
        raw_transaction: str,
        private_key: str,
        utxos: List[Dict[str, Any]],
    ) -> str:
        if not utxos:
            raise ValueError("UTXO metadata is required for local signing.")
        signed_tx = sign_raw_transaction_with_key(raw_transaction, private_key, utxos)
        await self.decoderawtransaction(signed_tx)
        return signed_tx

    async def test_mempool_accept(self, signed_raw_transaction: str, allow_high_fees: bool = False) -> Dict[str, Any]:
        """Dry-run the signed transaction against a node's mempool.

        Returns a dict with ``tested`` (bool), ``allowed`` (bool/None),
        ``txid`` and ``reject_reason``. If no public RPC endpoint can be
        reached, ``tested`` is False and broadcasting may still proceed.
        """
        try:
            result = await self.call_rpc_any(
                "testmempoolaccept",
                [[signed_raw_transaction], allow_high_fees],
            )
            if not isinstance(result, list) or not result:
                return {"tested": False, "allowed": None, "reason": "empty response"}
            entry = result[0]
            return {
                "tested": True,
                "allowed": bool(entry.get("allowed", False)),
                "txid": entry.get("txid"),
                "reject_reason": entry.get("reject-reason"),
            }
        except Exception as exc:
            return {"tested": False, "allowed": None, "reason": str(exc)}

    async def send_transaction(self, signed_raw_transaction: str) -> Dict[str, Any]:
        # Pre-flight mempool acceptance check before any broadcast.
        mempool_check = await self.test_mempool_accept(signed_raw_transaction)
        reject_reason = (mempool_check.get("reject_reason") or "").lower()
        if (
            mempool_check.get("tested")
            and not mempool_check.get("allowed")
            and "absurdly-high-fee" not in reject_reason
        ):
            return {
                "status": "error",
                "reason": (
                    f"Mempool rejected the transaction: "
                    f"{mempool_check.get('reject_reason') or 'unknown reason'}"
                ),
            }
        if "absurdly-high-fee" in reject_reason:
            logger.warning(
                "testmempoolaccept reports absurdly-high-fee; "
                "proceeding with allowhighfees broadcast"
            )

        scan_result = await self.zero_rpc.send_raw_transaction(signed_raw_transaction)
        if isinstance(scan_result, dict) and scan_result.get("status") == "ok":
            return {
                "status": "ok",
                "tx_id": scan_result["txid"],
                "tx_url": scan_result.get("tx_url"),
                "via": scan_result.get("api") or "zeroscan",
                "broadcast": "zeroscan",
                "mempool_check": mempool_check,
            }
        try:
            tx_id = await self.call_rpc_any('sendrawtransaction', [signed_raw_transaction, True])
            return {
                "status": "ok",
                "tx_id": tx_id,
                "tx_url": self.zero_rpc.tx_url(tx_id),
                "via": "public_rpc",
                "broadcast": "rpc",
                "zeroscan_error": scan_result,
                "mempool_check": mempool_check,
            }
        except Exception as rpc_error:
            return {
                "status": "error",
                "reason": f"Broadcast failed. zeroscan={scan_result}; rpc={rpc_error}",
            }

    async def preflight_contract_call(self, contract_address: str, data_hex: str, from_address: str, gas: int, require_bool_success: bool = False) -> Dict[str, Any]:
        result = await self.call_rpc_any("callcontract", [contract_address, data_hex, from_address, int(gas)])
        execution = (result or {}).get("executionResult", {})
        excepted = execution.get("excepted")
        if excepted not in (None, "", "None", "none"):
            output = execution.get("output", "")
            message = execution.get("exceptedMessage") or output or excepted
            raise ValueError(f"Contract dry-run reverted: {message}")
        output = (execution.get("output") or "").lower()
        if require_bool_success and output:
            try:
                if int(output[-64:], 16) == 0:
                    raise ValueError("Contract dry-run returned false.")
            except ValueError:
                raise
        return result

    async def call_contract_uint(self, contract_address: str, data_hex: str, from_address: Optional[str] = None, gas: int = 1000000) -> int:
        caller = from_address or self.admin_address
        result = await self.call_rpc_any("callcontract", [contract_address, data_hex, caller, int(gas)])
        execution = (result or {}).get("executionResult", {})
        excepted = execution.get("excepted")
        if excepted not in (None, "", "None", "none"):
            raise ValueError(f"Contract read reverted: {excepted}")
        output = execution.get("output") or "0"
        return int(output[-64:] or "0", 16)

    async def get_zrc20_balance_raw(self, contract_address: str, address: str) -> int:
        address_hex = await self.gethexaddress(address)
        return await self.call_contract_uint(contract_address, "70a08231" + _address_hex_arg(address_hex))

    async def get_zrc20_allowance_raw(self, contract_address: str, owner_address: str, spender_address: str) -> int:
        owner_hex = await self.gethexaddress(owner_address)
        spender_hex = await self.gethexaddress(spender_address)
        return await self.call_contract_uint(
            contract_address,
            "dd62ed3e" + _address_hex_arg(owner_hex) + _address_hex_arg(spender_hex),
            from_address=spender_address,
        )

    async def send_zhc(self, from_address: str, to_address: str, amount: float, private_key: str) -> Dict[str, Any]:
        selected_utxos: List[Dict[str, Any]] = []
        try:
            amount = _as_decimal(amount)
            if amount <= 0:
                raise ValueError("Amount must be greater than zero.")
            if amount < DUST_CHANGE:
                raise ValueError(
                    f"Amount {amount} ZHC is below the dust threshold "
                    f"({DUST_CHANGE} ZHC) and cannot be sent on-chain."
                )
            if not await self.validateaddress(from_address):
                raise ValueError("Invalid from_address.")
            if not await self.validateaddress(to_address):
                raise ValueError("Invalid to_address.")
            utxos = await self.get_utxos(from_address)
            min_fee = await self.calculate_fee_from_fee_rate(len(utxos), utxos, vout_count=3)
            logger.info(f"Minimum fee: {min_fee} ZHC")
            selected_utxos = await self.select_utxos(utxos, min_fee, amount, reserve=True)
            outputs = {to_address: str(amount)}
            available_balance = sum(_as_decimal(u['amount']) for u in utxos)
            if self.admin_fee > 0 and available_balance >= Decimal("10"):
                if not self.admin_address:
                    raise ValueError("admin_address is required when admin_fee > 0.")
                outputs[self.admin_address] = str(self.admin_fee)
            raw_tx = await self.create_raw_transaction(
                utxos=selected_utxos,
                outputs=outputs,
                min_fee=min_fee,
                change_address=from_address
            )
            signed_tx = await self.sign_transaction(raw_tx, private_key, selected_utxos)
            send_result = await self.send_transaction(signed_tx)
            if send_result.get("status") != "ok":
                self._release_utxos(selected_utxos)
                return send_result
            logger.info(f"Transaction sent successfully. TXID: {send_result['tx_id']}")
            return send_result
        except WaitNextBlockError as e:
            self._release_utxos(selected_utxos)
            logger.warning("ZHC send is waiting for next block: %s", e.diagnostics)
            return {
                "status": "error",
                "reason": WAIT_NEXT_BLOCK_MESSAGE,
                "action_required": "wait_next_block",
                "diagnostics": e.diagnostics,
            }
        except Exception as e:
            self._release_utxos(selected_utxos)
            logger.error("Error sending ZHC: %s", e)
            return {'status': "error", "reason": str(e)}
        finally:
            logger.info("send_zhc completed")

    async def send_to_contract(
        self,
        contract_address: str,
        from_address: str,
        to_address: str,
        amount: float,
        private_key: str,
        hex_command: str,
        gas: int,
        require_bool_success: bool = False,
        service_fee: Optional[Decimal] = None,
        selection_buffer: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        selected_utxos: List[Dict[str, Any]] = []
        try:
            amount = _as_decimal(amount)
            effective_admin_fee = _as_decimal(self.admin_fee if service_fee is None else service_fee)
            effective_extra_fee = _as_decimal(self.extra_fee if selection_buffer is None else selection_buffer)
            if not await self.validateaddress(from_address):
                raise ValueError("Invalid from_address.")
            await self.preflight_contract_call(
                contract_address=contract_address,
                data_hex=hex_command,
                from_address=from_address,
                gas=int(gas),
                require_bool_success=require_bool_success,
            )
            utxos = await self.get_utxos(from_address)
            min_fee = await self.calculate_fee_from_fee_rate(len(utxos), utxos, vout_count=2, contract_outputs=1)
            gas_price = Decimal('0.00000040')
            gas_fee = (Decimal(gas) * gas_price).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)

            selected_utxos = await self.select_utxos(
                utxos,
                min_fee + gas_fee,
                amount,
                reserve=True,
                admin_fee=effective_admin_fee,
                extra_fee=effective_extra_fee,
            )
            selected_amount = sum(_as_decimal(utxo['amount']) for utxo in selected_utxos)

            outputs = {}
            if effective_admin_fee > 0:
                outputs[self.admin_address] = str(effective_admin_fee)
            contract_info = {
                "contract": {
                    "contractAddress": contract_address,
                    "data": hex_command,
                    "amount": str(amount),
                    "gasLimit": int(gas),
                    "gasPrice": '0.00000040'
                }
            }
            outputs.update(contract_info)
            raw_tx = await self.create_raw_transaction(
                utxos=selected_utxos,
                outputs=outputs,
                min_fee=min_fee + gas_fee,
                change_address=from_address
            )
            signed_tx = await self.sign_transaction(raw_tx, private_key, selected_utxos)
            send_result = await self.send_transaction(signed_tx)
            if send_result.get("status") != "ok":
                self._release_utxos(selected_utxos)
                return send_result
            logger.info(f"Contract transaction sent successfully. TXID: {send_result['tx_id']}")
            send_result["input_total"] = str(selected_amount)
            return send_result
        except WaitNextBlockError as e:
            self._release_utxos(selected_utxos)
            logger.warning("Contract send is waiting for next block: %s", e.diagnostics)
            return {
                "status": "error",
                "reason": WAIT_NEXT_BLOCK_MESSAGE,
                "action_required": "wait_next_block",
                "diagnostics": e.diagnostics,
            }
        except Exception as e:
            self._release_utxos(selected_utxos)
            logger.error("Error sending to contract: %s", e)
            return {'status': "error", "reason": str(e)}
        finally:
            logger.info("send_to_contract completed")

    async def send_token(
        self,
        contract_address: str,
        from_address: str,
        to_address: str,
        amount_or_id: float,
        private_key: str,
        gas: int = 1000000
    ) -> Dict[str, Any]:
        token_units = int((_as_decimal(amount_or_id) * SATOSHIS_PER_ZHC).to_integral_value(rounding=ROUND_HALF_UP))
        hex_command = _build_zrc20_transfer_data(await self.gethexaddress(to_address), token_units)
        return await self.send_to_contract(
            contract_address=contract_address,
            from_address=from_address,
            to_address=to_address,
            amount=0.0,
            private_key=private_key,
            hex_command=hex_command,
            gas=int(gas),
            require_bool_success=True,
        )

    async def send_usdz(
        self,
        from_address: str,
        to_address: str,
        amount: float,
        private_key: str,
        gas: int = 1000000,
    ) -> Dict[str, Any]:
        return await self.send_token(
            contract_address=self.usdz_contract,
            from_address=from_address,
            to_address=to_address,
            amount_or_id=amount,
            private_key=private_key,
            gas=gas,
        )

    async def send_usdz_gasfree(
        self,
        owner_address: str,
        to_address: str,
        amount_or_id: float,
        admin_private_key: Optional[str] = None,
        admin_address: Optional[str] = None,
        gas: int = 1000000,
    ) -> Dict[str, Any]:
        selected_admin_key = (
            admin_private_key or self.gasfree_admin_private_key or ""
        ).strip()
        if not selected_admin_key:
            return {"status": "error", "reason": "Gas-free USDZ admin private key is not configured."}

        relayer_address = admin_address or BitcoinAddress().address_from_wif(selected_admin_key)
        try:
            if not await self.validateaddress(owner_address):
                raise ValueError("Invalid owner_address.")
            if not await self.validateaddress(to_address):
                raise ValueError("Invalid to_address.")
            if not await self.validateaddress(relayer_address):
                raise ValueError("Invalid gas-free admin address.")

            token_units = int((_as_decimal(amount_or_id) * SATOSHIS_PER_ZHC).to_integral_value(rounding=ROUND_HALF_UP))
            if token_units <= 0:
                raise ValueError("Amount must be greater than zero.")

            balance_raw = await self.get_zrc20_balance_raw(
                self.usdz_contract,
                owner_address,
            )
            if balance_raw < token_units:
                return {
                    "status": "error",
                    "reason": "Insufficient USDZ balance for gas-free transfer.",
                    "balance_raw": str(balance_raw),
                    "required_raw": str(token_units),
                }

            allowance_raw = await self.get_zrc20_allowance_raw(
                self.usdz_contract,
                owner_address,
                relayer_address,
            )
            if allowance_raw < token_units:
                return {
                    "status": "error",
                    "reason": "USDZ allowance is too low. User must approve the gas-free admin first.",
                    "allowance_raw": str(allowance_raw),
                    "required_raw": str(token_units),
                    "spender": relayer_address,
                }

            hex_command = _build_zrc20_transfer_from_data(
                await self.gethexaddress(owner_address),
                await self.gethexaddress(to_address),
                token_units,
            )
            result = await self.send_to_contract(
                contract_address=self.usdz_contract,
                from_address=relayer_address,
                to_address=to_address,
                amount=0,
                private_key=selected_admin_key,
                hex_command=hex_command,
                gas=int(gas),
                require_bool_success=True,
                service_fee=Decimal("0"),
                selection_buffer=Decimal("0"),
            )
            if result.get("status") == "ok":
                result["gasfree"] = True
                result["token"] = "USDZ"
                result["owner_address"] = owner_address
                result["relayer_address"] = relayer_address
            return result
        except Exception as e:
            logger.error("Error sending gas-free USDZ: %s", e)
            return {"status": "error", "reason": str(e)}

    async def _utxo_count(self, address: str) -> int:
        """Return the number of spendable UTXOs for an address."""
        try:
            utxos = await self.zero_rpc.get_utxos(address)
            return len(utxos) if isinstance(utxos, list) else 0
        except Exception as exc:
            logger.warning("Could not fetch UTXO count for %s: %s", address, exc)
            return 0

    def _extract_address_balance_view(self, data: Dict[str, Any]) -> Dict[str, Decimal]:
        confirmed_sat = int(data.get("balance") or data.get("confirmedBalance") or data.get("confirmed_balance") or 0)
        pending_raw = (
            data.get("unconfirmedBalance")
            if "unconfirmedBalance" in data
            else data.get("unconfirmed_balance")
            if "unconfirmed_balance" in data
            else data.get("pending")
            if "pending" in data
            else 0
        )
        pending_sat = int(pending_raw or 0)
        visible_sat = confirmed_sat + max(0, pending_sat)
        return {
            "balance": _from_sats(visible_sat),
            "confirmed_zhc": _from_sats(confirmed_sat),
            "pending_zhc": _from_sats(pending_sat),
        }

    async def getbalance(self, address, force_refresh: bool = False):
        if self.realtime_hub:
            self.realtime_hub.touch_address(str(address))
        height = getattr(self, "last_block_height", None) or self.sqlite_cache.get_last_block_height()
        cached_sqlite = self.sqlite_cache.get_balance(address)
        if not force_refresh and cached_sqlite:
            cached_height = int(cached_sqlite.get("height") or 0)
            known_height = height
            if not known_height or cached_height >= int(known_height):
                cached_sqlite["cached"] = True
                return cached_sqlite
        if force_refresh and cached_sqlite and not self.sqlite_cache.can_force_refresh(
            address,
            self.config.force_refresh_seconds,
        ):
            cached_sqlite["cached"] = True
            cached_sqlite["throttled"] = True
            cached_sqlite["min_force_refresh_seconds"] = self.config.force_refresh_seconds
            return cached_sqlite
        if height is None:
            height = await self._current_block_height_for_reservations()
        if not hasattr(self, "balance_cache"):
            self.balance_cache = {}
        cached = self.balance_cache.get(address)
        if not force_refresh and height is not None and cached and cached.get("height") == height:
            return {
                "status": "ok",
                "balance": cached["balance"],
                "confirmed_balance": cached.get("confirmed_zhc"),
                "pending_balance": cached.get("pending_zhc"),
                "utxo_len": cached.get("utxo_len", 0),
                "height": height,
                "cached": True,
            }
        try:
            raw_address = await self.zero_rpc._request_json("GET", f"/address/{address}")
            if isinstance(raw_address, dict) and raw_address.get("status") == "error":
                raise RuntimeError(raw_address.get("reason", "ZeroScan balance failed"))
            if not isinstance(raw_address, dict):
                raise RuntimeError(f"Unexpected ZeroScan address response: {raw_address}")
            balance_view = self._extract_address_balance_view(raw_address)
            utxo_len = await self._utxo_count(address)
            nfts = await self.zero_rpc.get_nft_balances(address)
            payload = {
                "status": "ok",
                "balance": balance_view["balance"],
                "confirmed_balance": balance_view["confirmed_zhc"],
                "pending_balance": balance_view["pending_zhc"],
                "utxo_len": utxo_len,
                "nfts": nfts,
                "height": height,
                "cached": False,
            }
            if height is not None:
                self.balance_cache[address] = {
                    "height": height,
                    "balance": balance_view["balance"],
                    "confirmed_zhc": balance_view["confirmed_zhc"],
                    "pending_zhc": balance_view["pending_zhc"],
                    "utxo_len": utxo_len,
                }
            self.sqlite_cache.put_balance(address, payload, height)
            return payload
        except Exception as e:
            logger.error("ZeroScan balance lookup failed, falling back to scantxoutset: %s", e)
        try:
            res = await self.scantxoutset(address)
            balance = res['total_amount']
            unspents = res.get('unspents', [])
            utxo_len = len(unspents) if isinstance(unspents, list) else 0
            payload = {
                "status": "ok",
                "balance": balance,
                "confirmed_balance": balance,
                "pending_balance": Decimal("0"),
                "utxo_len": utxo_len,
                "height": height,
                "cached": False,
            }
            if height is not None:
                self.balance_cache[address] = {"height": height, "balance": balance, "utxo_len": utxo_len}
            self.sqlite_cache.put_balance(address, payload, height)
            return payload
        except Exception as e:
            logger.exception("scantxoutset balance lookup failed")
            return {"status": "error", "reason": str(e)}
    # Смарты

    async def get_smartcontract_balanceOf(self,contract_hex, address):
        try:
            command = "70a08231" + (await self.get_finish_hex_address(address))
            raw_ans = await self.call_rpc("callcontract", [contract_hex, command])
            hex_ans = raw_ans['executionResult']['output']
            balance = int(hex_ans, 16) / 10 ** 8

            command = "06fdde03"
            raw_ans = await self.call_rpc("callcontract", [contract_hex, command])
            hex_ans = raw_ans['executionResult']['output'][128:]
            name = bytes.fromhex(hex_ans).decode('utf8').rstrip("\x00")

            return {"status": "ok",
                    "tokens_zrc20_balance": balance,
                    "contract": contract_hex,
                    "name":name}
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ZeroScanRPC.py

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional
from urllib.parse import urlparse

import httpx

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)


@dataclass
class ZeroScanEndpoint:
    api_base: str
    web_base: str
    failures: int = 0
    last_ok_ts: float = 0.0
    last_latency_ms: float = 0.0
    last_height: int = 0


def _normalize_base(url: str) -> str:
    return url.strip().rstrip("/")


def _web_base_from_api(api_base: str) -> str:
    host = urlparse(api_base).netloc or api_base.replace("https://", "").replace("http://", "")
    if host.startswith("ws."):
        host = host[3:]
    return f"https://{host}".rstrip("/")


class ZeroScanRPC:
    def __init__(self, endpoints: Optional[Iterable[str]] = None, timeout: float = 8.0):
        urls = list(endpoints) if endpoints is not None else []
        self.endpoints: List[ZeroScanEndpoint] = [
            ZeroScanEndpoint(api_base=_normalize_base(url), web_base=_web_base_from_api(url))
            for url in urls
        ]
        if not self.endpoints:
            raise ValueError("At least one ZeroScan endpoint is required.")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        self._active_index = 0
        self._probe_lock = asyncio.Lock()

    @property
    def active_endpoint(self) -> ZeroScanEndpoint:
        return self.endpoints[self._active_index]

    async def close(self):
        await self.client.aclose()
        self.client = httpx.AsyncClient(timeout=self.client.timeout)

    def tx_url(self, txid: str) -> str:
        return f"{self.active_endpoint.web_base}/tx/{txid}"

    def address_url(self, address: str) -> str:
        return f"{self.active_endpoint.web_base}/address/{address}"

    async def get_best_endpoint(self) -> ZeroScanEndpoint:
        async with self._probe_lock:
            probes = await asyncio.gather(
                *(self._probe_endpoint(index) for index in range(len(self.endpoints))),
                return_exceptions=True,
            )
            best_index: Optional[int] = None
            for index, result in enumerate(probes):
                if isinstance(result, Exception) or result is False:
                    continue
                if best_index is None:
                    best_index = index
                    continue
                best = self.endpoints[best_index]
                current = self.endpoints[index]
                if current.last_height > best.last_height:
                    best_index = index
                elif current.last_height == best.last_height and current.last_latency_ms < best.last_latency_ms:
                    best_index = index
            if best_index is not None:
                self._active_index = best_index
            return self.active_endpoint

    async def _probe_endpoint(self, index: int) -> bool:
        endpoint = self.endpoints[index]
        started = time.monotonic()
        try:
            response = await self.client.get(f"{endpoint.api_base}/info")
            response.raise_for_status()
            data = response.json()
            endpoint.last_latency_ms = (time.monotonic() - started) * 1000
            endpoint.last_ok_ts = time.time()
            endpoint.failures = 0
            endpoint.last_height = int(
                data.get("height")
                or data.get("blockHeight")
                or data.get("blocks")
                or data.get("block_height")
                or 0
            )
            return True
        except Exception as exc:
            endpoint.failures += 1
            logger.warning("ZeroScan probe failed for %s: %s", endpoint.api_base, exc)
            return False

    def _ordered_indexes(self) -> List[int]:
        indexes = list(range(len(self.endpoints)))

        def score(index: int):
            endpoint = self.endpoints[index]
            active_penalty = 0 if index == self._active_index else 1
            return (
                endpoint.failures,
                active_penalty,
                -endpoint.last_height,
                endpoint.last_latency_ms or 999999,
            )

        indexes.sort(key=score)
        return indexes

    async def _request_json(self, method: str, path: str, **kwargs) -> Any:
        last_error: Optional[Exception] = None
        normalized_path = path if path.startswith("/") else f"/{path}"
        for index in self._ordered_indexes():
            endpoint = self.endpoints[index]
            started = time.monotonic()
            try:
                response = await self.client.request(
                    method,
                    f"{endpoint.api_base}{normalized_path}",
                    **kwargs,
                )
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and data.get("status") == "error":
                    raise RuntimeError(data.get("reason") or data.get("message") or "ZeroScan error")
                endpoint.last_latency_ms = (time.monotonic() - started) * 1000
                endpoint.last_ok_ts = time.time()
                endpoint.failures = 0
                self._active_index = index
                return data
            except Exception as exc:
                endpoint.failures += 1
                last_error = exc
                logger.warning("ZeroScan request failed for %s%s: %s", endpoint.api_base, normalized_path, exc)
        return {"status": "error", "reason": str(last_error or "All ZeroScan endpoints failed")}

    async def get_utxos(self, address: str):
        return await self._request_json("GET", f"/address/{address}/utxo")

    async def get_balance(self, address: str):
        data = await self._request_json("GET", f"/address/{address}")
        if isinstance(data, dict) and data.get("status") == "error":
            return data
        try:
            return round(int(data["balance"]) / 10 ** 8, 8)
        except Exception as exc:
            logger.error("ZeroScan balance parse error: %s", exc)
            return {"status": "error", "reason": str(exc)}

    async def get_transactions(self, address: str, limit: int = 500) -> list[str]:
        """Return a list of transaction IDs involving ``address``."""
        data = await self._request_json("GET", f"/address/{address}/txs")
        if isinstance(data, dict) and data.get("status") == "error":
            return []
        transactions = data.get("transactions") if isinstance(data, dict) else None
        if not isinstance(transactions, list):
            return []
        return transactions[:limit]

    async def get_nft_balances(self, address: str, limit: int = 500) -> list[dict]:
        """Return currently held ZRC721 NFTs for ``address``.

        The result is computed by scanning the latest ``limit`` transactions and
        tracking incoming/outgoing transfers. It is a best-effort view; very old
        sends may not be reflected if the transaction history is truncated.
        """
        txids = await self.get_transactions(address, limit=limit)
        if not txids:
            return []

        batch_size = 50
        owned: dict[str, dict] = {}

        def _process_tx(tx: Any) -> None:
            transfers = tx.get("zrc721TokenTransfers") if isinstance(tx, dict) else None
            if not isinstance(transfers, list):
                return
            for transfer in transfers:
                token_id = str(transfer.get("tokenId") or "")
                contract = str(transfer.get("address") or transfer.get("addressHex") or "")
                if not token_id or not contract:
                    continue
                key = f"{contract}:{token_id}"
                to = transfer.get("to")
                from_ = transfer.get("from")
                if to == address:
                    owned[key] = {
                        "contract": contract,
                        "token_id": token_id,
                        "name": transfer.get("name", ""),
                        "symbol": transfer.get("symbol", ""),
                    }
                elif from_ == address and key in owned:
                    del owned[key]

        for index in range(0, len(txids), batch_size):
            batch = txids[index : index + batch_size]
            tx_string = ",".join(batch)
            txs = await self._request_json("GET", f"/txs/{tx_string}?brief=")
            if isinstance(txs, dict) and txs.get("status") == "error":
                continue
            if not isinstance(txs, list):
                continue
            for tx in txs:
                _process_tx(tx)

        return list(owned.values())

    async def send_raw_transaction(self, raw_tx: str):
        data = await self._request_json("POST", "/tx/send", json={"rawtx": raw_tx})
        if isinstance(data, dict) and data.get("status") == "error":
            return data
        txid = self._extract_txid(data)
        if not txid:
            return {"status": "error", "reason": f"Unexpected broadcast response: {data}"}
        return {
            "status": "ok",
            "txid": txid,
            "id": txid,
            "scan": self.active_endpoint.web_base,
            "api": self.active_endpoint.api_base,
            "tx_url": self.tx_url(txid),
            "raw": data,
        }

    @staticmethod
    def _extract_txid(data: Any) -> Optional[str]:
        if isinstance(data, str) and len(data) == 64:
            return data
        if not isinstance(data, dict):
            return None
        for key in ("txid", "id", "hash", "result"):
            value = data.get(key)
            if isinstance(value, str) and len(value) == 64:
                return value
        result = data.get("result")
        if isinstance(result, dict):
            for key in ("txid", "id", "hash"):
                value = result.get(key)
                if isinstance(value, str) and len(value) == 64:
                    return value
        return None

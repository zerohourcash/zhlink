from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

JsonCallback = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class ZeroScanWebSocketHub:
    """Shared ZeroScan WebSocket connection for one asyncio event loop.

    The hub keeps one socket for block and address subscriptions.  Individual
    ``ZHCashRPC`` clients register callbacks; they do not open their own socket.
    HTTP/RPC polling remains the fallback when WSS is unavailable.
    """

    def __init__(
        self,
        urls: tuple[str, ...],
        address_ttl_seconds: float = 3600.0,
        max_failures: int = 3,
        cooldown_seconds: float = 60.0,
    ):
        self.urls = tuple(urls)
        self.address_ttl_seconds = max(60.0, float(address_ttl_seconds))
        self.max_failures = max(1, int(max_failures))
        self.cooldown_seconds = max(5.0, float(cooldown_seconds))
        self.block_callbacks: set[JsonCallback] = set()
        self.address_callbacks: dict[str, set[JsonCallback]] = defaultdict(set)
        self.address_last_used: dict[str, float] = {}
        self.server_subscribed_addresses: set[str] = set()
        self.task: asyncio.Task | None = None
        self._send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.active_url: str | None = None
        self._stopping = False
        self.consecutive_failures = 0
        self.disabled_until = 0.0

    def add_block_callback(self, callback: JsonCallback) -> Callable[[], None]:
        self.block_callbacks.add(callback)
        self.ensure_started()

        def unsubscribe() -> None:
            self.block_callbacks.discard(callback)
            self._maybe_stop()

        return unsubscribe

    def add_address_callback(self, address: str, callback: JsonCallback) -> Callable[[], None]:
        normalized = str(address).strip()
        self.address_callbacks[normalized].add(callback)
        self.touch_address(normalized)
        self.ensure_started()

        def unsubscribe() -> None:
            callbacks = self.address_callbacks.get(normalized)
            if callbacks:
                callbacks.discard(callback)
                if not callbacks:
                    self.address_callbacks.pop(normalized, None)
                    self.address_last_used.pop(normalized, None)
                    self._queue_address_unsubscribe(normalized)
            self._maybe_stop()

        return unsubscribe

    def touch_address(self, address: str) -> None:
        """Mark an address as active and subscribe it over the current socket."""

        normalized = str(address).strip()
        if not normalized:
            return
        self.address_last_used[normalized] = time.monotonic()
        self._queue_address_subscribe(normalized)

    def _queue_address_subscribe(self, address: str) -> None:
        if address in self.server_subscribed_addresses:
            return
        self.server_subscribed_addresses.add(address)
        self._send_queue.put_nowait(
            {"type": "subscribe", "channel": "address", "address": address}
        )

    def _queue_address_unsubscribe(self, address: str) -> None:
        if address not in self.server_subscribed_addresses:
            return
        self.server_subscribed_addresses.discard(address)
        self._send_queue.put_nowait(
            {"type": "unsubscribe", "channel": "address", "address": address}
        )

    def prune_stale_addresses(self) -> list[str]:
        now = time.monotonic()
        stale = [
            address
            for address, last_used in list(self.address_last_used.items())
            if now - last_used > self.address_ttl_seconds
        ]
        for address in stale:
            self.address_last_used.pop(address, None)
            self._queue_address_unsubscribe(address)
        return stale

    def ensure_started(self) -> None:
        if self.task and not self.task.done():
            return
        self._stopping = False
        self.task = asyncio.create_task(self._run())

    def _maybe_stop(self) -> None:
        if self.block_callbacks or self.address_callbacks:
            return
        self._stopping = True
        if self.task and not self.task.done():
            self.task.cancel()

    async def close(self) -> None:
        self._stopping = True
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.task = None

    async def _dispatch(self, callback: JsonCallback, payload: dict[str, Any]) -> None:
        try:
            result = callback(payload)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:
            logger.warning("ZeroScan WSS callback failed: %s", exc)

    async def _emit_block(self, payload: dict[str, Any]) -> None:
        for callback in list(self.block_callbacks):
            await self._dispatch(callback, payload)

    async def _emit_address(self, payload: dict[str, Any]) -> None:
        address = str(payload.get("address") or "").strip()
        if not address:
            return
        for callback in list(self.address_callbacks.get(address, ())):
            await self._dispatch(callback, payload)

    async def _run(self) -> None:
        try:
            import aiohttp  # type: ignore
        except Exception as exc:
            logger.warning("ZeroScan WSS disabled: aiohttp unavailable: %s", exc)
            return

        delay = 1.0
        while not self._stopping:
            now = time.monotonic()
            if self.disabled_until > now:
                await asyncio.sleep(min(self.disabled_until - now, 30.0))
                continue
            connected = False
            for url in self.urls:
                if self._stopping:
                    break
                try:
                    await self._connect(aiohttp, url)
                    connected = True
                    delay = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("ZeroScan WSS failed for %s: %s", url, exc)
                    self.active_url = None
            if not connected:
                self.consecutive_failures += 1
                logger.warning("All ZeroScan WSS endpoints failed; HTTP/RPC fallback remains active.")
                if self.consecutive_failures >= self.max_failures:
                    self.disabled_until = time.monotonic() + self.cooldown_seconds
                    logger.warning(
                        "ZeroScan WSS cooldown for %.1fs after %s failed rounds; HTTP/RPC fallback remains active.",
                        self.cooldown_seconds,
                        self.consecutive_failures,
                    )
                    self.consecutive_failures = 0
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _connect(self, aiohttp_module: Any, url: str) -> None:
        timeout = aiohttp_module.ClientTimeout(total=None, connect=10, sock_read=90)
        async with aiohttp_module.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(url, heartbeat=30) as ws:
                self.active_url = url
                self.consecutive_failures = 0
                self.disabled_until = 0.0
                self.server_subscribed_addresses.clear()
                await ws.send_json({"type": "subscribe", "channel": "blocks"})
                self.prune_stale_addresses()
                for address in list(self.address_last_used):
                    self._queue_address_subscribe(address)
                sender = asyncio.create_task(self._sender(ws))
                pruner = asyncio.create_task(self._prune_loop())
                try:
                    async for msg in ws:
                        if msg.type == aiohttp_module.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except Exception:
                                continue
                            if data.get("type") == "block":
                                await self._emit_block(data)
                            elif data.get("type") == "address:transaction":
                                await self._emit_address(data)
                        elif msg.type in (
                            aiohttp_module.WSMsgType.CLOSED,
                            aiohttp_module.WSMsgType.ERROR,
                        ):
                            break
                finally:
                    pruner.cancel()
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pruner
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender

    async def _sender(self, ws: Any) -> None:
        while True:
            payload = await self._send_queue.get()
            await ws.send_json(payload)

    async def _prune_loop(self) -> None:
        interval = min(300.0, max(30.0, self.address_ttl_seconds / 4))
        while True:
            await asyncio.sleep(interval)
            self.prune_stale_addresses()


_HUBS: dict[tuple[Any, ...], ZeroScanWebSocketHub] = {}


def get_realtime_hub(
    urls: tuple[str, ...],
    address_ttl_seconds: float = 3600.0,
    max_failures: int = 3,
    cooldown_seconds: float = 60.0,
) -> ZeroScanWebSocketHub:
    loop = asyncio.get_running_loop()
    key = (
        id(loop),
        tuple(urls),
        int(max(60.0, float(address_ttl_seconds))),
        int(max(1, int(max_failures))),
        int(max(5.0, float(cooldown_seconds))),
    )
    hub = _HUBS.get(key)
    if hub is None:
        hub = ZeroScanWebSocketHub(
            tuple(urls),
            address_ttl_seconds,
            max_failures,
            cooldown_seconds,
        )
        _HUBS[key] = hub
    return hub

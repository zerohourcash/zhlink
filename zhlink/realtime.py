from __future__ import annotations

import asyncio
import contextlib
import json
import logging
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

    def __init__(self, urls: tuple[str, ...]):
        self.urls = tuple(urls)
        self.block_callbacks: set[JsonCallback] = set()
        self.address_callbacks: dict[str, set[JsonCallback]] = defaultdict(set)
        self.task: asyncio.Task | None = None
        self._send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.active_url: str | None = None
        self._stopping = False

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
        self.ensure_started()
        self._send_queue.put_nowait(
            {"type": "subscribe", "channel": "address", "address": normalized}
        )

        def unsubscribe() -> None:
            callbacks = self.address_callbacks.get(normalized)
            if callbacks:
                callbacks.discard(callback)
                if not callbacks:
                    self.address_callbacks.pop(normalized, None)
                    self._send_queue.put_nowait(
                        {
                            "type": "unsubscribe",
                            "channel": "address",
                            "address": normalized,
                        }
                    )
            self._maybe_stop()

        return unsubscribe

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
                logger.warning("All ZeroScan WSS endpoints failed; HTTP/RPC fallback remains active.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _connect(self, aiohttp_module: Any, url: str) -> None:
        timeout = aiohttp_module.ClientTimeout(total=None, connect=10, sock_read=90)
        async with aiohttp_module.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(url, heartbeat=30) as ws:
                self.active_url = url
                await ws.send_json({"type": "subscribe", "channel": "blocks"})
                for address in list(self.address_callbacks):
                    await ws.send_json(
                        {"type": "subscribe", "channel": "address", "address": address}
                    )
                sender = asyncio.create_task(self._sender(ws))
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
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender

    async def _sender(self, ws: Any) -> None:
        while True:
            payload = await self._send_queue.get()
            await ws.send_json(payload)


_HUBS: dict[tuple[int, tuple[str, ...]], ZeroScanWebSocketHub] = {}


def get_realtime_hub(urls: tuple[str, ...]) -> ZeroScanWebSocketHub:
    loop = asyncio.get_running_loop()
    key = (id(loop), tuple(urls))
    hub = _HUBS.get(key)
    if hub is None:
        hub = ZeroScanWebSocketHub(tuple(urls))
        _HUBS[key] = hub
    return hub


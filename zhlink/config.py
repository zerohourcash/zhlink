from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

# Hardcoded network defaults. The application can override them, but these
# values keep the library usable out-of-the-box without any external config.
DEFAULT_ZEROSCAN_ENDPOINTS = (
    "https://ws.zeroscan.st",
    "https://ws.zeroscan.io",
)

DEFAULT_PUBLIC_RPC_URLS = (
    "https://rpc.zeroscan.st",
)

DEFAULT_BLOCK_WS_URLS = (
    "wss://ws.zeroscan.st/ws",
    "wss://wallet.zeroscan.st/ws",
)

DEFAULT_USDZ_CONTRACT = "a48d0ee7365ce1add8e595de4d54344239f8ca28"
DEFAULT_LOG_DIR = "logs"
DEFAULT_ADMIN_ADDRESS = "ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi"
DEFAULT_ADMIN_FEE = Decimal("0")
DEFAULT_EXTRA_FEE = Decimal("0")
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_CACHE_PATH = ".zhlink-cache.sqlite3"
DEFAULT_FORCE_REFRESH_SECONDS = 10.0
DEFAULT_BLOCK_POLL_SECONDS = 30.0

# Production-test keys requested for gas-free USDZ validation.
# These are test wallets for controlled sends only.
TEST_USDZ_SENDER_PRIVATE_KEY = "L1ezcSr7R8V2uvnL2hvpGmZtiqogXNiQFcRdfCbYk4xqng8nL5qQ"
TEST_USDZ_SENDER_ADDRESS = "ZRbvpQGRP4ZkcJMunaSD1pmTbmz9HaSgYX"
TEST_GASFREE_ADMIN_PRIVATE_KEY = "Kwmo8yjJMr4HU8iraDSH73FW7yzMV6n2w4RRcoXqf1yxBwapjixo"
TEST_GASFREE_ADMIN_ADDRESS = "ZTKgQA9E8JtxkxtENbHeBwDmxnKZQj4p9X"
TEST_USDZ_RECIPIENT_ADDRESS = "ZGqDPGCds5CBRHLZZCnYWsYWYPF3i9NCvi"
TEST_USDZ_AMOUNT = Decimal("0.1")


@dataclass(frozen=True)
class ZHLinkConfig:
    """Runtime configuration for the pure Python ZHLink library.

    The library ships with sensible hardcoded defaults so it works without an
    external config file. Applications can override any value through the
    ``public_network`` helper or by constructing the dataclass directly.
    """

    zeroscan_endpoints: tuple[str, ...] = DEFAULT_ZEROSCAN_ENDPOINTS
    public_rpc_urls: tuple[str, ...] = DEFAULT_PUBLIC_RPC_URLS
    block_ws_urls: tuple[str, ...] = DEFAULT_BLOCK_WS_URLS
    usdz_contract: str = DEFAULT_USDZ_CONTRACT
    log_dir: str = DEFAULT_LOG_DIR
    admin_address: str = DEFAULT_ADMIN_ADDRESS
    admin_fee: Decimal = DEFAULT_ADMIN_FEE
    extra_fee: Decimal = DEFAULT_EXTRA_FEE
    gasfree_admin_private_key: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    cache_path: str = DEFAULT_CACHE_PATH
    force_refresh_seconds: float = DEFAULT_FORCE_REFRESH_SECONDS
    block_poll_seconds: float = DEFAULT_BLOCK_POLL_SECONDS

    @classmethod
    def public_network(
        cls,
        *,
        zeroscan_endpoints: Iterable[str] = DEFAULT_ZEROSCAN_ENDPOINTS,
        public_rpc_urls: Iterable[str] = DEFAULT_PUBLIC_RPC_URLS,
        block_ws_urls: Iterable[str] = DEFAULT_BLOCK_WS_URLS,
        usdz_contract: str = DEFAULT_USDZ_CONTRACT,
        log_dir: str = DEFAULT_LOG_DIR,
        admin_address: str = DEFAULT_ADMIN_ADDRESS,
        admin_fee: str | Decimal = DEFAULT_ADMIN_FEE,
        extra_fee: str | Decimal = DEFAULT_EXTRA_FEE,
        gasfree_admin_private_key: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cache_path: str = DEFAULT_CACHE_PATH,
        force_refresh_seconds: float = DEFAULT_FORCE_REFRESH_SECONDS,
        block_poll_seconds: float = DEFAULT_BLOCK_POLL_SECONDS,
    ) -> "ZHLinkConfig":
        return cls(
            admin_address=admin_address,
            admin_fee=Decimal(str(admin_fee)),
            extra_fee=Decimal(str(extra_fee)),
            zeroscan_endpoints=tuple(zeroscan_endpoints),
            public_rpc_urls=tuple(public_rpc_urls),
            block_ws_urls=tuple(block_ws_urls),
            usdz_contract=usdz_contract,
            gasfree_admin_private_key=gasfree_admin_private_key,
            timeout_seconds=timeout_seconds,
            log_dir=log_dir,
            cache_path=cache_path,
            force_refresh_seconds=force_refresh_seconds,
            block_poll_seconds=block_poll_seconds,
        )

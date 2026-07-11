from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


class SQLiteBalanceCache:
    """Small persistent cache for address snapshots.

    The cache stores already-normalized public balance data only. It never
    stores private keys, mnemonics or signed transactions.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS balance_cache (
                    address TEXT PRIMARY KEY,
                    height INTEGER,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS utxo_cache (
                    address TEXT PRIMARY KEY,
                    height INTEGER,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def get_balance(self, address: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload, height, updated_at FROM balance_cache WHERE address = ?",
                (address,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload"]))
        payload.setdefault("cached", True)
        payload.setdefault("height", row["height"])
        payload.setdefault("cache_updated_at", row["updated_at"])
        return payload

    def put_balance(self, address: str, payload: dict[str, Any], height: int | None) -> None:
        stored = dict(payload)
        stored["cached"] = False
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO balance_cache(address, height, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    height = excluded.height,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (address, int(height or 0), json.dumps(stored, default=str), now),
            )
            conn.commit()

    def get_last_refresh_at(self, address: str) -> float:
        cached = self.get_balance(address)
        if not cached:
            return 0.0
        return float(cached.get("cache_updated_at") or 0.0)

    def can_force_refresh(self, address: str, min_interval_seconds: float) -> bool:
        last = self.get_last_refresh_at(address)
        return last <= 0 or time.time() - last >= float(min_interval_seconds)

    def set_meta(self, key: str, value: str | int | float) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO cache_meta(key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )
            conn.commit()

    def get_meta(self, key: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM cache_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def get_utxos(self, address: str) -> list[dict[str, Any]] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload, height, updated_at FROM utxo_cache WHERE address = ?",
                (address,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, list):
            return None
        return payload

    def put_utxos(self, address: str, utxos: list[dict[str, Any]], height: int | None) -> None:
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO utxo_cache(address, height, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    height = excluded.height,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (address, int(height or 0), json.dumps(utxos, default=str), now),
            )
            conn.commit()

    def get_last_block_height(self) -> int | None:
        value = self.get_meta("last_block_height")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def set_last_block_height(self, height: int) -> None:
        if int(height) > 0:
            self.set_meta("last_block_height", int(height))

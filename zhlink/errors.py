from __future__ import annotations

from typing import Any


WAIT_NEXT_BLOCK_MESSAGE = (
    "Wait for the next block before sending again. The visible balance may "
    "include 0-confirmation change, but there is no confirmed spendable UTXO "
    "available for a new transaction yet."
)


class WaitNextBlockError(RuntimeError):
    """Raised when confirmed funds exist only in locally reserved/pending UTXO."""

    def __init__(self, diagnostics: dict[str, Any]):
        self.diagnostics = diagnostics
        super().__init__(f"{WAIT_NEXT_BLOCK_MESSAGE} diagnostics={diagnostics}")

from __future__ import annotations

import sys
from pathlib import Path


def ensure_zhc_rawtx_path() -> None:
    """Make the bundled or sibling zhc_rawtx importable.

    `zhlink` vendors `zhc_rawtx` under `_vendor` so it can be copied or
    zipped as one self-contained library. The sibling source-tree package stays
    as a development fallback.
    """

    package_dir = Path(__file__).resolve().parent
    candidates = [
        package_dir / "_vendor",
        package_dir.parents[0] / "zhc_rawtx",
    ]
    for root in reversed(candidates):
        if root.exists():
            path = str(root)
            if path not in sys.path:
                sys.path.insert(0, path)


ensure_zhc_rawtx_path()

import zhc_rawtx  # noqa: F401,E402

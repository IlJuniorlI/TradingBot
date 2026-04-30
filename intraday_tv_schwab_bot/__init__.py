# SPDX-License-Identifier: MIT
from pathlib import Path as _Path


def _read_version() -> str:
    # version.txt lives at the repo root alongside this package.
    candidate = _Path(__file__).resolve().parent.parent / "version.txt"
    if candidate.exists():
        try:
            return candidate.read_text(encoding="utf-8").strip() or "0.0.0"
        except Exception:
            return "0.0.0"
    return "0.0.0"


__version__ = _read_version()

__all__ = ["__version__"]

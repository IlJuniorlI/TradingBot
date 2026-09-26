# SPDX-License-Identifier: MIT
"""Writing state to disk."""
from __future__ import annotations

from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` via tmp+rename so a mid-write crash leaves
    the prior-good file intact instead of truncating it. ``Path.replace`` is
    atomic on both POSIX and Windows for same-volume renames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding=encoding)
    tmp_path.replace(path)

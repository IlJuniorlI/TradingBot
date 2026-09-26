# SPDX-License-Identifier: MIT
from pathlib import Path as _Path

# VERSION ships inside the package as package data, and pyproject.toml's
# dynamic version reads the same file at build time, so a source checkout, an
# editable install and a wheel all report the one version. A missing file is a
# broken install and fails the import.
__version__ = _Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()

__all__ = ["__version__"]

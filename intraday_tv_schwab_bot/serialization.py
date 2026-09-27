# SPDX-License-Identifier: MIT
"""Reading and writing files: the atomic state write, and the YAML read the
config load and the event calendar share."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` via tmp+rename so a mid-write crash leaves
    the prior-good file intact instead of truncating it. ``Path.replace`` is
    atomic on both POSIX and Windows for same-volume renames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding=encoding)
    tmp_path.replace(path)


def _not_a(node: yaml.ScalarNode, what: str, why: str = "") -> yaml.constructor.ConstructorError:
    """The YAML error for a scalar that does not read as *what*, naming the
    node's line and column (a blank value, or one with spaces around it, is
    shown quoted)."""
    mark = node.start_mark
    text = node.value if node.value and node.value == node.value.strip() else repr(node.value)
    return yaml.constructor.ConstructorError(
        None, None, f"line {mark.line + 1}, column {mark.column + 1}: {text} is not {what}" + (f" ({why})" if why else ""),
    )


class LineNamingSafeLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` whose scalar that does not read as its type fails
    as a YAML error naming its line and column: an unquoted date that cannot
    exist (``2026-11-31``, ``2026-13-01``), and a value behind an explicit
    tag that is not one (``!!timestamp 2026-9-30x``, ``!!bool maybe``,
    ``!!int x``, ``!!float y``, ``!!null 2026-09-16``; an unquoted ``0x_``
    or ``0b_`` too). PyYAML's
    own raise a bare ``ValueError`` ("day is out of range for month"),
    ``AttributeError``, ``KeyError`` or ``IndexError`` naming neither the
    file nor the line. As YAML errors, ``read_yaml`` names the file, and the
    event calendar keeps the rows last read on a runtime edit carrying one."""

    def construct_yaml_timestamp(self, node: yaml.ScalarNode) -> date | datetime:
        if self.timestamp_regexp.match(self.construct_scalar(node)) is None:
            raise _not_a(node, "a date", "a !!timestamp is YYYY-MM-DD, or a date and a time")
        try:
            return super().construct_yaml_timestamp(node)
        except ValueError as exc:
            raise _not_a(node, "a date", str(exc)) from exc

    def construct_yaml_bool(self, node: yaml.ScalarNode) -> bool:
        if self.construct_scalar(node).lower() not in self.bool_values:
            raise _not_a(node, "true or false", "a !!bool is true, false, yes, no, on or off")
        return super().construct_yaml_bool(node)

    def construct_yaml_int(self, node: yaml.ScalarNode) -> int:
        # IndexError: PyYAML reads the first character of a blank value.
        try:
            return super().construct_yaml_int(node)
        except (ValueError, IndexError) as exc:
            raise _not_a(node, "an integer") from exc

    def construct_yaml_float(self, node: yaml.ScalarNode) -> float:
        try:
            return super().construct_yaml_float(node)
        except (ValueError, IndexError) as exc:
            raise _not_a(node, "a number") from exc

    def construct_yaml_null(self, node: yaml.ScalarNode) -> None:
        # PyYAML ignores the text: ``date: !!null 2026-09-16`` read as no
        # date, a blackout row that applied every day.
        if self.construct_scalar(node) not in ("", "~", "null", "Null", "NULL"):
            raise _not_a(node, "null", "a !!null is empty, ~ or null")
        return super().construct_yaml_null(node)


LineNamingSafeLoader.add_constructor("tag:yaml.org,2002:timestamp", LineNamingSafeLoader.construct_yaml_timestamp)
LineNamingSafeLoader.add_constructor("tag:yaml.org,2002:bool", LineNamingSafeLoader.construct_yaml_bool)
LineNamingSafeLoader.add_constructor("tag:yaml.org,2002:int", LineNamingSafeLoader.construct_yaml_int)
LineNamingSafeLoader.add_constructor("tag:yaml.org,2002:float", LineNamingSafeLoader.construct_yaml_float)
LineNamingSafeLoader.add_constructor("tag:yaml.org,2002:null", LineNamingSafeLoader.construct_yaml_null)


def read_yaml(path: Path) -> Any:
    """The YAML in *path*, read with ``LineNamingSafeLoader``; ``None`` for
    an empty file. ``ValueError`` naming the file when it cannot be read, is
    not YAML or holds a value that does not read as its type, naming its
    line (``earnings.yaml: not a readable YAML file: line 3, column 10:
    2026-11-31 is not a date (day is out of range for month)``)."""
    try:
        return yaml.load(path.read_text(), Loader=LineNamingSafeLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: not a readable YAML file: {exc}") from exc

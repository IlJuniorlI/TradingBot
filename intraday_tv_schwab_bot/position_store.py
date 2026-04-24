# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit_logger import _json_ready
from .models import Position, Side

LOG = logging.getLogger(__name__)


class ReconcileMetadataStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._initialized = False

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS open_position_metadata (
                    position_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    stop_price REAL NOT NULL,
                    target_price REAL,
                    trail_pct REAL,
                    highest_price REAL,
                    lowest_price REAL,
                    pair_id TEXT,
                    reference_symbol TEXT,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _ensure_ready(self) -> None:
        if self._initialized:
            return
        self._ensure_schema()
        self._initialized = True

    @staticmethod
    def _serialize(position_key: str, position: Position) -> dict[str, Any]:
        return {
            "position_key": str(position_key),
            "symbol": str(position.symbol),
            "strategy": str(position.strategy),
            "side": str(position.side.value),
            "qty": int(position.qty),
            "entry_price": float(position.entry_price),
            "entry_time": position.entry_time.isoformat(),
            "stop_price": float(position.stop_price),
            "target_price": float(position.target_price) if position.target_price is not None else None,
            "trail_pct": float(position.trail_pct) if position.trail_pct is not None else None,
            "highest_price": float(position.highest_price) if position.highest_price is not None else None,
            "lowest_price": float(position.lowest_price) if position.lowest_price is not None else None,
            "pair_id": position.pair_id,
            "reference_symbol": position.reference_symbol,
            "metadata_json": json.dumps(_json_ready(position.metadata or {}), sort_keys=True, separators=(",", ":")),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }

    @staticmethod
    def _deserialize(row: dict[str, Any]) -> Position:
        metadata_raw = row.get("metadata_json") or "{}"
        try:
            metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else dict(metadata_raw or {})
        except Exception:
            metadata = {}
        entry_time = row.get("entry_time")
        parsed_entry_time = entry_time if isinstance(entry_time, datetime) else datetime.fromisoformat(str(entry_time))
        return Position(
            symbol=str(row.get("symbol") or row.get("position_key") or ""),
            strategy=str(row.get("strategy") or ""),
            side=Side(str(row.get("side"))),
            qty=int(row.get("qty") or 0),
            entry_price=float(row.get("entry_price") or 0.0),
            entry_time=parsed_entry_time,
            stop_price=float(row.get("stop_price") or 0.0),
            target_price=float(row["target_price"]) if row.get("target_price") not in {None, "", "None"} else None,
            trail_pct=float(row["trail_pct"]) if row.get("trail_pct") not in {None, "", "None"} else None,
            highest_price=float(row["highest_price"]) if row.get("highest_price") not in {None, "", "None"} else None,
            lowest_price=float(row["lowest_price"]) if row.get("lowest_price") not in {None, "", "None"} else None,
            pair_id=str(row.get("pair_id") or "") or None,
            reference_symbol=str(row.get("reference_symbol") or "") or None,
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def save_positions(self, positions: dict[str, Position]) -> None:
        self._ensure_ready()
        rows = [self._serialize(key, pos) for key, pos in positions.items()]
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DELETE FROM open_position_metadata")
            if rows:
                conn.executemany(
                    """
                    INSERT INTO open_position_metadata (
                        position_key, symbol, strategy, side, qty, entry_price, entry_time,
                        stop_price, target_price, trail_pct, highest_price, lowest_price,
                        pair_id, reference_symbol, metadata_json, updated_at
                    ) VALUES (
                        :position_key, :symbol, :strategy, :side, :qty, :entry_price, :entry_time,
                        :stop_price, :target_price, :trail_pct, :highest_price, :lowest_price,
                        :pair_id, :reference_symbol, :metadata_json, :updated_at
                    )
                    """,
                    rows,
                )
            conn.commit()
        finally:
            conn.close()

    def load_positions(self) -> dict[str, Position]:
        if not self.path.exists():
            return {}
        self._ensure_ready()
        conn = sqlite3.connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM open_position_metadata ORDER BY position_key").fetchall()
        except Exception as exc:
            LOG.warning("Could not load startup reconcile metadata from %s: %s", self.path, exc)
            return {}
        finally:
            conn.close()
        out: dict[str, Position] = {}
        for row in rows:
            try:
                record = dict(row)
                key = str(record.get("position_key") or record.get("symbol") or "").strip()
                if not key:
                    continue
                out[key] = self._deserialize(record)
            except Exception as exc:
                LOG.warning("Skipping startup reconcile metadata row due to parse error: %s", exc)
        return out

    def delete_unmatched_positions(self, keep_position_keys: set[str]) -> int:
        if not self.path.exists():
            return 0
        self._ensure_ready()
        keep = {str(key).strip() for key in keep_position_keys if str(key).strip()}
        conn = sqlite3.connect(self.path)
        try:
            if keep:
                placeholders = ",".join("?" for _ in keep)
                cur = conn.execute(
                    f"DELETE FROM open_position_metadata WHERE position_key NOT IN ({placeholders})",
                    tuple(sorted(keep)),
                )
            else:
                cur = conn.execute("DELETE FROM open_position_metadata")
            conn.commit()
            return int(cur.rowcount or 0)
        except Exception as exc:
            LOG.warning("Could not prune startup reconcile metadata in %s: %s", self.path, exc)
            return 0
        finally:
            conn.close()

"""DuckDB I/O for the ingester.

Kept intentionally small: connect, bootstrap schema, upsert raw_tx /
kami_action batches, read/write the ingest cursor, prune by window.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import duckdb

from .decoder import DecodedAction

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class RawTx:
    tx_hash: str
    block_number: int
    block_timestamp: int           # unix seconds
    tx_index: int
    from_addr: str
    to_addr: str
    method_sig: str
    system_id: str | None
    raw_calldata: bytes
    status: int
    gas_used: int | None
    gas_price_wei: int | None


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(db_path))

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # Bootstrap.
    # ------------------------------------------------------------------

    def bootstrap(self, schema_sql: str) -> None:
        self.conn.execute(schema_sql)

    # ------------------------------------------------------------------
    # Cursor.
    # ------------------------------------------------------------------

    def get_cursor(self) -> int | None:
        row = self.conn.execute(
            "SELECT last_block_scanned FROM ingest_cursor WHERE id = 1"
        ).fetchone()
        return int(row[0]) if row else None

    def set_cursor(
        self,
        *,
        block_number: int,
        block_timestamp: int | None,
        vendor_sha: str | None,
    ) -> None:
        ts = _ts(block_timestamp) if block_timestamp is not None else None
        self.conn.execute(
            """
            INSERT INTO ingest_cursor
                (id, last_block_scanned, last_block_timestamp, schema_version, vendor_sha, updated_at)
            VALUES
                (1, ?, ?, ?, ?, now())
            ON CONFLICT (id) DO UPDATE SET
                last_block_scanned = excluded.last_block_scanned,
                last_block_timestamp = excluded.last_block_timestamp,
                schema_version = excluded.schema_version,
                vendor_sha = excluded.vendor_sha,
                updated_at = now()
            """,
            [block_number, ts, SCHEMA_VERSION, vendor_sha],
        )

    # ------------------------------------------------------------------
    # Upserts.
    # ------------------------------------------------------------------

    def upsert_raw_txs(self, txs: Iterable[RawTx]) -> int:
        rows = [
            (
                t.tx_hash,
                t.block_number,
                _ts(t.block_timestamp),
                t.tx_index,
                t.from_addr,
                t.to_addr,
                t.method_sig,
                t.system_id,
                t.raw_calldata,
                t.status,
                t.gas_used,
                t.gas_price_wei,
            )
            for t in txs
        ]
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT INTO raw_tx
                (tx_hash, block_number, block_timestamp, tx_index, from_addr, to_addr,
                 method_sig, system_id, raw_calldata, status, gas_used, gas_price_wei)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tx_hash) DO NOTHING
            """,
            rows,
        )
        return len(rows)

    def upsert_actions(self, actions: Iterable[DecodedAction]) -> int:
        rows = [
            (
                f"{a.tx_hash}:{a.sub_index}",
                a.tx_hash,
                a.sub_index,
                a.block_number,
                _ts(a.block_timestamp),
                a.action_type,
                a.system_id,
                a.from_addr,
                a.kami_id,
                a.target_kami_id,
                a.node_id,
                a.amount,
                a.item_index,
                a.metadata_json(),
                a.status,
            )
            for a in actions
        ]
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT INTO kami_action
                (id, tx_hash, sub_index, block_number, block_timestamp,
                 action_type, system_id, from_addr, kami_id, target_kami_id,
                 node_id, amount, item_index, metadata_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            rows,
        )
        return len(rows)

    # ------------------------------------------------------------------
    # Prune.
    # ------------------------------------------------------------------

    def prune_older_than(self, cutoff_ts: int) -> tuple[int, int]:
        cutoff = _ts(cutoff_ts)
        n_actions = self.conn.execute(
            "SELECT COUNT(*) FROM kami_action WHERE block_timestamp < ?", [cutoff]
        ).fetchone()[0]
        self.conn.execute(
            "DELETE FROM kami_action WHERE block_timestamp < ?", [cutoff]
        )
        n_raw = self.conn.execute(
            "SELECT COUNT(*) FROM raw_tx WHERE block_timestamp < ?", [cutoff]
        ).fetchone()[0]
        self.conn.execute(
            "DELETE FROM raw_tx WHERE block_timestamp < ?", [cutoff]
        )
        return int(n_actions), int(n_raw)


def _ts(unix_s: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(unix_s), tz=dt.timezone.utc)


def read_schema_sql(repo_root: Path) -> str:
    return (repo_root / "schema" / "schema.sql").read_text()

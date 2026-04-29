"""DuckDB I/O for the ingester.

Kept intentionally small: connect, bootstrap schema, upsert raw_tx /
kami_action batches, read/write the ingest cursor, prune by window.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import duckdb

from .decoder import DecodedAction

log = logging.getLogger(__name__)

SCHEMA_VERSION = 9


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
    """DuckDB I/O, thread-safe via ``self.lock``.

    The Stage-1 co-hosted service (session 3) runs the ingest poller and
    the FastAPI query layer inside the same process sharing one DuckDB
    connection — DuckDB holds a per-process exclusive file lock, so two
    processes cannot open the file simultaneously. Within one process
    multiple threads ARE allowed, but ``duckdb.DuckDBPyConnection`` calls
    must be serialized; ``self.lock`` guards every method that touches
    ``self.conn``. Callers that need to run a raw query inside the same
    transaction boundary should acquire ``self.lock`` explicitly.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(db_path))
        self.lock = threading.Lock()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------------
    # Bootstrap.
    # ------------------------------------------------------------------

    def bootstrap(self, schema_sql: str) -> None:
        with self.lock:
            self.conn.execute(schema_sql)
            self._apply_migrations()

    def _apply_migrations(self) -> None:
        """Run any pending schema migrations under ``self.lock``.

        Each migration is idempotent and bumps ``ingest_cursor.schema_version``
        on success. Boot order: schema.sql (CREATE IF NOT EXISTS) first, then
        migrations to ALTER existing tables for installs that pre-date a
        column.
        """
        import importlib.util

        cur = self.conn.execute(
            "SELECT schema_version FROM ingest_cursor WHERE id = 1"
        ).fetchone()
        version = int(cur[0]) if cur and cur[0] is not None else 0

        repo_root = Path(__file__).resolve().parent.parent

        def _load(rel: str, mod_name: str):
            spec = importlib.util.spec_from_file_location(mod_name, repo_root / rel)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        m002 = _load("migrations/002_add_harvest_id_column.py", "migration_002")
        m003 = _load("migrations/003_add_account_name_columns.py", "migration_003")
        m004 = _load("migrations/004_add_build_columns.py", "migration_004")
        m005 = _load("migrations/005_add_modifier_columns.py", "migration_005")
        m006 = _load("migrations/006_add_affinity_columns.py", "migration_006")
        m007 = _load("migrations/007_add_items_catalog.py", "migration_007")
        m008 = _load("migrations/008_add_kami_equipment_view.py", "migration_008")
        m009 = _load("migrations/009_add_skills_catalog.py", "migration_009")

        # Each migration is idempotent — safe to run on a fresh install where
        # the columns may already exist via schema.sql.
        if version < m002.TARGET_SCHEMA_VERSION:
            log.info("storage: applying migration 002 (add harvest_id)")
            stats = m002.run(self.conn)
            log.info("storage: migration 002 complete: %s", stats)

        if version < m003.TARGET_SCHEMA_VERSION:
            log.info("storage: applying migration 003 (add account_index, account_name)")
            stats = m003.run(self.conn)
            log.info("storage: migration 003 complete: %s", stats)

        if version < m004.TARGET_SCHEMA_VERSION:
            log.info("storage: applying migration 004 (add build columns)")
            stats = m004.run(self.conn)
            log.info("storage: migration 004 complete: %s", stats)

        if version < m005.TARGET_SCHEMA_VERSION:
            log.info("storage: applying migration 005 (add modifier columns)")
            stats = m005.run(self.conn)
            log.info("storage: migration 005 complete: %s", stats)

        if version < m006.TARGET_SCHEMA_VERSION:
            log.info("storage: applying migration 006 (add affinity columns)")
            stats = m006.run(self.conn)
            log.info("storage: migration 006 complete: %s", stats)

        if version < m007.TARGET_SCHEMA_VERSION:
            log.info("storage: applying migration 007 (add items_catalog table)")
            stats = m007.run(self.conn)
            log.info("storage: migration 007 complete: %s", stats)

        if version < m008.TARGET_SCHEMA_VERSION:
            log.info("storage: applying migration 008 (add kami_equipment view)")
            stats = m008.run(self.conn)
            log.info("storage: migration 008 complete: %s", stats)

        if version < m009.TARGET_SCHEMA_VERSION:
            log.info("storage: applying migration 009 (add skills_catalog table)")
            stats = m009.run(self.conn)
            log.info("storage: migration 009 complete: %s", stats)

    # ------------------------------------------------------------------
    # Cursor.
    # ------------------------------------------------------------------

    def get_cursor(self) -> int | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT last_block_scanned FROM ingest_cursor WHERE id = 1"
            ).fetchone()
        return int(row[0]) if row else None

    def get_cursor_state(self) -> dict[str, Any] | None:
        """Full cursor row for health checks — block, timestamp, vendor sha."""
        with self.lock:
            row = self.conn.execute(
                """
                SELECT last_block_scanned, last_block_timestamp, schema_version,
                       vendor_sha, updated_at
                FROM ingest_cursor WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "last_block_scanned": int(row[0]),
            "last_block_timestamp": row[1].isoformat() if row[1] is not None else None,
            "schema_version": int(row[2]),
            "vendor_sha": row[3],
            "updated_at": row[4].isoformat() if row[4] is not None else None,
        }

    def set_cursor(
        self,
        *,
        block_number: int,
        block_timestamp: int | None,
        vendor_sha: str | None,
    ) -> None:
        ts = _ts(block_timestamp) if block_timestamp is not None else None
        with self.lock:
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
        with self.lock:
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
                a.harvest_id,
                a.metadata_json(),
                a.status,
            )
            for a in actions
        ]
        if not rows:
            return 0
        with self.lock:
            self.conn.executemany(
                """
                INSERT INTO kami_action
                    (id, tx_hash, sub_index, block_number, block_timestamp,
                     action_type, system_id, from_addr, kami_id, target_kami_id,
                     node_id, amount, item_index, harvest_id, metadata_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO NOTHING
                """,
                rows,
            )
        return len(rows)

    # ------------------------------------------------------------------
    # System address snapshot.
    # ------------------------------------------------------------------

    def upsert_system_address_snapshot(
        self,
        rows: Iterable[tuple[str, str, str, int | None, int | None]],
    ) -> int:
        """Upsert (system_id, address, abi_name, first_seen, last_seen) rows.

        On conflict, widens first_seen to the min and last_seen to the max
        across what's stored and what's incoming — supports incremental
        snapshot extension as the poller re-probes over time.
        """
        rows = list(rows)
        if not rows:
            return 0
        with self.lock:
            self.conn.executemany(
                """
                INSERT INTO system_address_snapshot
                    (system_id, address, abi_name, first_seen_block, last_seen_block, ingested_at)
                VALUES (?, ?, ?, ?, ?, now())
                ON CONFLICT (system_id, address) DO UPDATE SET
                    first_seen_block = LEAST(
                        COALESCE(system_address_snapshot.first_seen_block, excluded.first_seen_block),
                        COALESCE(excluded.first_seen_block, system_address_snapshot.first_seen_block)
                    ),
                    last_seen_block = GREATEST(
                        COALESCE(system_address_snapshot.last_seen_block, excluded.last_seen_block),
                        COALESCE(excluded.last_seen_block, system_address_snapshot.last_seen_block)
                    ),
                    ingested_at = now()
                """,
                rows,
            )
        return len(rows)

    def load_system_address_snapshot(
        self,
    ) -> list[tuple[str, str, str, int | None, int | None]]:
        """Return all stored (system_id, address, abi_name, first, last) rows."""
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT system_id, address, abi_name, first_seen_block, last_seen_block
                FROM system_address_snapshot
                """
            ).fetchall()
        return [
            (r[0], r[1], r[2], int(r[3]) if r[3] is not None else None,
             int(r[4]) if r[4] is not None else None)
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Prune.
    # ------------------------------------------------------------------

    def prune_older_than(self, cutoff_ts: int) -> tuple[int, int]:
        """Delete kami_action and raw_tx rows with block_timestamp < cutoff.

        Returns ``(kami_action_deleted, raw_tx_deleted)``. Wrapped in a
        single transaction so a crash mid-prune can't leave one table
        stale against the other.
        """
        cutoff = _ts(cutoff_ts)
        with self.lock:
            self.conn.execute("BEGIN TRANSACTION")
            try:
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
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return int(n_actions), int(n_raw)

    # ------------------------------------------------------------------
    # Generic read helpers (for the co-hosted FastAPI query layer).
    # ------------------------------------------------------------------

    def fetchall(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        with self.lock:
            return self.conn.execute(sql, params or []).fetchall()

    def fetchone(self, sql: str, params: list[Any] | None = None) -> tuple | None:
        with self.lock:
            return self.conn.execute(sql, params or []).fetchone()

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        """Run a statement that returns no rows (e.g. EXPORT DATABASE).

        Held inside ``self.lock`` like every other write so concurrent
        ingest writes don't race the admin call.
        """
        with self.lock:
            self.conn.execute(sql, params or [])


def _ts(unix_s: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(unix_s), tz=dt.timezone.utc)


def read_schema_sql(repo_root: Path) -> str:
    return (repo_root / "schema" / "schema.sql").read_text()

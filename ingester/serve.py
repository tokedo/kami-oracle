"""Co-hosted service: poller loop + read-only HTTP API in one process.

Why co-host: DuckDB holds a per-process exclusive file lock on its
database file. A separate read-only query process would race the
ingester for the lock. Keeping both the poller and the FastAPI app in
one process — sharing a single ``Storage`` instance whose methods
serialize on ``storage.lock`` — sidesteps the issue entirely.

Threading layout:

* Main thread runs ``uvicorn`` (blocks until SIGTERM/SIGINT).
* A daemon thread runs the poller loop, which is a thin refactor of
  ``ingester.poller`` around the ``process_block_range`` core so the
  ingest logic itself stays single-sourced.

Graceful shutdown: the main thread's signal handler flips
``stop_event`` (read by the poller each iteration) and then tells
uvicorn to stop; uvicorn drains in-flight HTTP requests, after which
we join the poller and close the DB.

HTTP bind: ``KAMI_ORACLE_API_BIND`` (default ``127.0.0.1:8787``).
Do NOT bind to a public interface — there's no auth layer yet.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path

import uvicorn

from .api import build_app
from .chain_client import ChainClient
from .config import configure_logging, load_config
from .decoder import Decoder
from .ingest import process_block_range
from .poller import REGISTRY_REPROBE_INTERVAL_S
from .storage import Storage, read_schema_sql
from .system_registry import SystemRegistry, resolve_systems

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
UNKNOWN_LOG = REPO_ROOT / "memory" / "unknown-systems.md"

DEFAULT_BIND = "127.0.0.1:8787"
MAX_CATCHUP = 50


def _parse_bind(bind: str) -> tuple[str, int]:
    host, port = bind.rsplit(":", 1)
    # Guard against bind-all misconfigurations. There's no auth layer on
    # the HTTP endpoints yet; exposure decision is Phase D + ADR.
    if host in ("0.0.0.0", "", "*", "::"):
        raise ValueError(
            f"refusing to bind API to {host!r}; must be a loopback-only host "
            "(set KAMI_ORACLE_API_BIND=127.0.0.1:PORT). Public exposure is "
            "gated on a Phase-D auth layer."
        )
    return host, int(port)


def _poller_loop(
    *,
    client: ChainClient,
    decoder: Decoder,
    registry: SystemRegistry,
    storage: Storage,
    cfg,
    vendor_sha: str | None,
    stop_event: threading.Event,
) -> None:
    """Tail Yominet and write decoded rows. Mirrors ingester.poller.main."""
    cursor = storage.get_cursor()
    if cursor is not None:
        next_block = cursor + 1
    else:
        next_block = client.block_number()
        log.info("serve/poller: fresh start — beginning at head block %d", next_block)

    last_reprobe_ts = time.time()

    while not stop_event.is_set():
        try:
            now_ts = time.time()
            if now_ts - last_reprobe_ts >= REGISTRY_REPROBE_INTERVAL_S:
                try:
                    fresh = resolve_systems(client, cfg.world_address, cfg.abi_dir)
                    new_addrs = registry.extend(fresh)
                    if new_addrs:
                        for a in sorted(new_addrs):
                            info = registry.get_by_address(a)
                            log.info(
                                "registry: new system address observed: %s -> %s",
                                info.system_id if info else "?", a,
                            )
                        storage.upsert_system_address_snapshot(
                            registry.to_snapshot_rows()
                        )
                except Exception as e:  # noqa: BLE001
                    log.warning("serve/poller: registry re-probe failed: %s", e)
                last_reprobe_ts = now_ts

            try:
                head = client.block_number()
            except Exception as e:  # noqa: BLE001
                log.warning("serve/poller: block_number failed: %s", e)
                if stop_event.wait(cfg.poll_interval_s):
                    break
                continue

            if next_block > head:
                if stop_event.wait(cfg.poll_interval_s):
                    break
                continue

            end = min(head, next_block + MAX_CATCHUP - 1)
            log.info("serve/poller: scanning blocks %d..%d (head=%d)", next_block, end, head)
            try:
                stats = process_block_range(
                    client=client,
                    decoder=decoder,
                    registry=registry,
                    storage=storage,
                    start_block=next_block,
                    end_block=end,
                    vendor_sha=vendor_sha,
                    unknown_log_path=UNKNOWN_LOG,
                )
                log.info(
                    "serve/poller: blocks=%d txs_seen=%d matched=%d decoded=%d "
                    "actions=%d unknown=%d errors=%d",
                    stats.blocks_scanned, stats.txs_seen, stats.txs_matched,
                    stats.txs_decoded, stats.actions, stats.unknown_selector,
                    stats.decode_errors,
                )
                next_block = end + 1
            except Exception as e:  # noqa: BLE001
                log.exception(
                    "serve/poller: range %d..%d failed, will retry: %s",
                    next_block, end, e,
                )
                if stop_event.wait(cfg.poll_interval_s):
                    break
        except Exception as e:  # noqa: BLE001
            log.exception(
                "serve/poller: unhandled exception, sleeping 60s before resume: %s", e,
            )
            if stop_event.wait(60):
                break
            db_cur = storage.get_cursor()
            if db_cur is not None and db_cur + 1 > next_block:
                log.info(
                    "serve/poller: advancing next_block %d -> %d from DB cursor",
                    next_block, db_cur + 1,
                )
                next_block = db_cur + 1

    log.info("serve/poller: stop_event set; exiting poller loop")


def main() -> int:
    cfg = load_config()
    configure_logging(cfg.log_level)

    bind = os.environ.get("KAMI_ORACLE_API_BIND", DEFAULT_BIND)
    host, port = _parse_bind(bind)

    client = ChainClient(cfg.rpc_url)
    if not client.is_connected():
        log.error("serve: RPC %s not reachable", cfg.rpc_url)
        return 1

    storage = Storage(cfg.db_path)
    storage.bootstrap(read_schema_sql(REPO_ROOT))

    registry = resolve_systems(client, cfg.world_address, cfg.abi_dir)
    prior = storage.load_system_address_snapshot()
    if prior:
        prior_reg = SystemRegistry.from_snapshot_rows(prior)
        registry.extend(prior_reg)
        log.info(
            "serve: registry extended with %d prior-snapshot address(es)",
            len(prior_reg),
        )
    storage.upsert_system_address_snapshot(registry.to_snapshot_rows())

    decoder = Decoder(cfg.abi_dir, registry)
    vendor_sha = (
        cfg.vendor_sha_path.read_text().strip()
        if cfg.vendor_sha_path.exists() else None
    )

    stop_event = threading.Event()
    poller_thread = threading.Thread(
        target=_poller_loop,
        kwargs=dict(
            client=client,
            decoder=decoder,
            registry=registry,
            storage=storage,
            cfg=cfg,
            vendor_sha=vendor_sha,
            stop_event=stop_event,
        ),
        name="kami-poller",
        daemon=True,
    )
    poller_thread.start()

    app = build_app(storage, registry)

    # Build uvicorn server manually so we can drive a graceful shutdown
    # ourselves when the poller signals (and vice versa).
    ucfg = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=cfg.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(ucfg)

    def _handle_signal(signum, _frame):
        log.info("serve: signal %d received; beginning graceful shutdown", signum)
        stop_event.set()
        server.should_exit = True

    # uvicorn.Server.run installs its own handlers; install ours first so
    # we set stop_event alongside server.should_exit.
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("serve: starting HTTP on %s:%d (poller thread live)", host, port)
    try:
        server.run()
    finally:
        stop_event.set()
        poller_thread.join(timeout=30)
        if poller_thread.is_alive():
            log.warning("serve: poller thread did not exit within 30s")
        try:
            storage.close()
        except Exception as e:  # noqa: BLE001
            log.warning("serve: storage close failed: %s", e)
        log.info("serve: shutdown complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

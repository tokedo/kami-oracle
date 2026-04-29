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
* A daemon thread runs the prune loop, which wakes every
  ``cfg.prune_interval_s`` seconds and drops rows older than
  ``cfg.window_days`` via ``Storage.prune_older_than``.

Concurrency: all three consumers (poller, prune, API) share one
DuckDB connection. DuckDB's own internal lock serializes writes on
a single connection; ``Storage.lock`` additionally serializes each
method call, which is what every caller in this process relies on.

Graceful shutdown: the main thread's signal handler flips
``stop_event`` (read by the poller and prune loops each iteration)
and then tells uvicorn to stop; uvicorn drains in-flight HTTP
requests, after which we join the background threads and close the
DB.

HTTP bind: ``KAMI_ORACLE_API_BIND`` (default ``127.0.0.1:8787``).
Binding to a non-loopback host requires both KAMI_ORACLE_API_TOKEN
and KAMI_ORACLE_ALLOW_NONLOOPBACK=1 — see ``_parse_bind`` below.
"""

from __future__ import annotations

import datetime as dt
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
from .harvest_resolver import HarvestResolver
from .ingest import process_block_range
from .items_catalog import ensure_loaded as ensure_items_catalog_loaded
from .skills_catalog import ensure_loaded as ensure_skills_catalog_loaded
from .kami_static import KamiStaticReader, refresh_stale
from .poller import REGISTRY_REPROBE_INTERVAL_S
from .storage import Storage, read_schema_sql
from .system_registry import SystemRegistry, resolve_systems

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
UNKNOWN_LOG = REPO_ROOT / "memory" / "unknown-systems.md"

DEFAULT_BIND = "127.0.0.1:8787"
MAX_CATCHUP = 50

# How often the kami_static refresh thread sweeps for stale rows (seconds).
KAMI_STATIC_REFRESH_INTERVAL_S = 6 * 60 * 60   # 6 hours
KAMI_STATIC_MAX_AGE_HOURS = 24                  # re-read kamis older than 1 day

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _parse_bind(bind: str, *, allow_nonloopback: bool, has_token: bool) -> tuple[str, int]:
    """Parse KAMI_ORACLE_API_BIND into (host, port).

    Non-loopback binds are opt-in: caller must set
    KAMI_ORACLE_ALLOW_NONLOOPBACK=1 AND KAMI_ORACLE_API_TOKEN before
    we'll accept a bind that isn't 127.0.0.1 / localhost / ::1.
    """
    host, port = bind.rsplit(":", 1)
    if host in _LOOPBACK_HOSTS:
        return host, int(port)
    if host in ("", "*"):
        raise ValueError(
            f"KAMI_ORACLE_API_BIND host is empty or wildcard ({host!r})"
        )
    if not allow_nonloopback:
        raise ValueError(
            f"refusing to bind API to non-loopback host {host!r}. Set "
            "KAMI_ORACLE_ALLOW_NONLOOPBACK=1 (and KAMI_ORACLE_API_TOKEN) "
            "to opt in."
        )
    if not has_token:
        raise ValueError(
            f"refusing to bind API to non-loopback host {host!r} without a "
            "token. Set KAMI_ORACLE_API_TOKEN."
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
    resolver: HarvestResolver,
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
                    resolver=resolver,
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


def _kami_static_loop(
    *,
    reader: KamiStaticReader,
    storage: Storage,
    stop_event: threading.Event,
) -> None:
    """Periodic refresh of kami_static.

    Sleeps ``KAMI_STATIC_REFRESH_INTERVAL_S`` between sweeps. Each sweep
    refreshes kamis that are missing from ``kami_static`` or whose
    ``last_refreshed_ts`` is older than ``KAMI_STATIC_MAX_AGE_HOURS``.

    First sweep runs immediately (after a 60s warm-up that lets the poller
    register the latest kami_ids); on a fresh DB this is the bootstrap.
    """
    log.info(
        "serve/kami_static: starting — sweep every %ds, max_age=%dh",
        KAMI_STATIC_REFRESH_INTERVAL_S, KAMI_STATIC_MAX_AGE_HOURS,
    )
    if stop_event.wait(60):  # initial warm-up
        return
    while not stop_event.is_set():
        try:
            stats = refresh_stale(
                storage, reader,
                max_age_hours=KAMI_STATIC_MAX_AGE_HOURS,
            )
            log.info("serve/kami_static: sweep done — %s", stats)
        except Exception as e:  # noqa: BLE001
            log.exception("serve/kami_static: sweep failed: %s", e)
        if stop_event.wait(KAMI_STATIC_REFRESH_INTERVAL_S):
            break
    log.info("serve/kami_static: stop_event set; exiting")


def _prune_loop(
    *,
    storage: Storage,
    cfg,
    stop_event: threading.Event,
) -> None:
    """Periodically prune rows outside the configured retention window.

    Wakes every ``cfg.prune_interval_s`` seconds, computes a cutoff
    of ``now - cfg.window_days``, and calls ``prune_older_than``.
    Exceptions are caught and logged — the loop never dies, so a
    transient DB hiccup can't disable pruning until the next process
    restart.
    """
    log.info(
        "serve/prune: starting — interval=%.0fs, window=%d days",
        cfg.prune_interval_s, cfg.window_days,
    )
    while not stop_event.is_set():
        if stop_event.wait(cfg.prune_interval_s):
            break
        try:
            cutoff_dt = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=cfg.window_days)
            cutoff_ts = int(cutoff_dt.timestamp())
            n_actions, n_raw = storage.prune_older_than(cutoff_ts)
            log.info(
                "serve/prune: deleted %d kami_action, %d raw_tx, cutoff=%s",
                n_actions, n_raw, cutoff_dt.isoformat(),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("serve/prune: sweep failed, retrying in 60s: %s", e)
            if stop_event.wait(60):
                break
    log.info("serve/prune: stop_event set; exiting prune loop")


def main() -> int:
    cfg = load_config()
    configure_logging(cfg.log_level)

    bind = os.environ.get("KAMI_ORACLE_API_BIND", DEFAULT_BIND)
    allow_nonloopback = os.environ.get("KAMI_ORACLE_ALLOW_NONLOOPBACK", "").strip() == "1"
    host, port = _parse_bind(
        bind,
        allow_nonloopback=allow_nonloopback,
        has_token=cfg.api_token is not None,
    )
    if host not in _LOOPBACK_HOSTS:
        log.warning(
            "serve: API listening on %s:%d — auth required (non-loopback bind)",
            host, port,
        )

    client = ChainClient(cfg.rpc_url)
    if not client.is_connected():
        log.error("serve: RPC %s not reachable", cfg.rpc_url)
        return 1

    storage = Storage(cfg.db_path)
    storage.bootstrap(read_schema_sql(REPO_ROOT))

    # Session 13: hydrate items_catalog on first boot (idempotent — no-op
    # once rows exist). The founder runs `python -m ingester.items_catalog
    # --reload` after re-vendoring kami_context.
    catalogs_dir = REPO_ROOT / "kami_context" / "catalogs"
    with storage.lock:
        try:
            ensure_items_catalog_loaded(storage.conn, catalogs_dir)
        except Exception:
            log.exception("serve: items_catalog initial load failed")
        try:
            ensure_skills_catalog_loaded(storage.conn, catalogs_dir)
        except Exception:
            log.exception("serve: skills_catalog initial load failed")

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

    resolver = HarvestResolver()
    resolver.bootstrap_from_db(storage)

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
            resolver=resolver,
        ),
        name="kami-poller",
        daemon=True,
    )
    poller_thread.start()

    prune_thread = threading.Thread(
        target=_prune_loop,
        kwargs=dict(storage=storage, cfg=cfg, stop_event=stop_event),
        name="kami-prune",
        daemon=True,
    )
    prune_thread.start()

    # kami_static refresh thread. Lazy: only starts the sweeps if the
    # GetterSystem address resolved in the registry — otherwise log and
    # skip so the rest of the service still runs.
    kami_static_thread: threading.Thread | None = None
    try:
        ks_reader = KamiStaticReader(client, registry, cfg.abi_dir)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "serve: kami_static refresh thread NOT started — %s. "
            "Re-vendor kami_context (system.getter ABI) and restart.", e,
        )
        ks_reader = None
    if ks_reader is not None:
        kami_static_thread = threading.Thread(
            target=_kami_static_loop,
            kwargs=dict(
                reader=ks_reader,
                storage=storage,
                stop_event=stop_event,
            ),
            name="kami-static",
            daemon=True,
        )
        kami_static_thread.start()

    app = build_app(
        storage,
        registry,
        api_token=cfg.api_token,
        bind_host=host,
        rate_limit_per_min=cfg.rate_limit_per_min,
    )

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
        prune_thread.join(timeout=5)
        if prune_thread.is_alive():
            log.warning("serve: prune thread did not exit within 5s")
        if kami_static_thread is not None:
            kami_static_thread.join(timeout=10)
            if kami_static_thread.is_alive():
                log.warning("serve: kami_static thread did not exit within 10s")
        try:
            storage.close()
        except Exception as e:  # noqa: BLE001
            log.warning("serve: storage close failed: %s", e)
        log.info("serve: shutdown complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

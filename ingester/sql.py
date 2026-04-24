"""Ad-hoc read-only SQL query plane for the ``/sql`` endpoint.

Two layers:

* ``validate_readonly_sql`` — parse-lite guard that rejects anything
  that isn't a single read-only statement. Strips SQL comments for
  the leading-keyword check, rejects stacked queries (``;`` between
  statements), and blacklists write / admin tokens.
* ``run_readonly`` — executes a cleaned statement on the shared
  DuckDB connection under a row cap and a wall-clock timeout. Uses
  a watcher thread that calls ``con.interrupt()`` on expiry; the
  DuckDB ``statement_timeout`` session variable would be simpler but
  isn't reliable across all versions we support, so we use the
  interrupt fallback unconditionally.

Both helpers deliberately keep zero state — ``Storage`` owns the
connection and its lock, and we borrow both per call.

Known caveat on the token blacklist: the regex is a whole-word match,
so a SELECT that mentions a blacklisted keyword inside a quoted
string literal (e.g. ``WHERE note = 'DROP table'``) will be rejected
as a false positive. Accepted tradeoff for v1 — adding a full
tokenizer isn't worth the complexity. Callers that need to query
such literals can rewrite the predicate.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from .storage import Storage

log = logging.getLogger(__name__)


class SqlValidationError(ValueError):
    """Raised when a supplied statement fails the static validator."""


class SqlExecutionError(RuntimeError):
    """Raised when DuckDB fails to execute a validated statement."""


class SqlTimeoutError(RuntimeError):
    """Raised when a query exceeds the configured wall-clock budget."""


@dataclass
class SqlResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    latency_ms: int


MAX_QUERY_CHARS = 10_000

# Statements that are safe to accept as the leading keyword of a
# single read-only query. Everything else is rejected.
_ALLOWED_LEADING = frozenset({
    "SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN", "PRAGMA", "SUMMARIZE",
})

# Whole-word blacklist — rejects anything that could write state,
# side-load files, or escape the read-only envelope.
_BLACKLIST = (
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE",
    "ATTACH", "DETACH", "LOAD", "INSTALL", "COPY", "IMPORT", "EXPORT",
    "CHECKPOINT", "CALL",
)
_BLACKLIST_RE = re.compile(
    r"\b(" + "|".join(_BLACKLIST) + r")\b", re.IGNORECASE,
)

# Line comments (-- ...) and block comments (/* ... */). Block comments
# are non-greedy so nested markers don't swallow trailing SQL.
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(q: str) -> str:
    return _BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub(" ", q))


def validate_readonly_sql(q: str) -> str:
    """Return a cleaned single statement, or raise ``SqlValidationError``.

    The returned string has trailing semicolons and whitespace stripped;
    callers pass it straight to DuckDB.
    """
    if q is None:
        raise SqlValidationError("query is required")
    stripped = q.strip()
    if not stripped:
        raise SqlValidationError("query is empty")
    if len(stripped) > MAX_QUERY_CHARS:
        raise SqlValidationError(
            f"query is longer than {MAX_QUERY_CHARS} characters"
        )

    # Stacked-query check: after splitting on `;`, only one non-empty
    # chunk may remain. Strip comments first so a `--` suffix doesn't
    # count.
    sans_comments = _strip_comments(stripped)
    chunks = [c for c in (p.strip() for p in sans_comments.split(";")) if c]
    if len(chunks) == 0:
        raise SqlValidationError("query is empty after comment stripping")
    if len(chunks) > 1:
        raise SqlValidationError("multiple statements are not allowed")

    body = chunks[0]
    first_word_match = re.match(r"\s*([A-Za-z_]+)", body)
    if not first_word_match:
        raise SqlValidationError("could not find a leading keyword")
    first_word = first_word_match.group(1).upper()
    if first_word not in _ALLOWED_LEADING:
        raise SqlValidationError(
            f"leading keyword {first_word!r} is not allowed; must be one of "
            f"{sorted(_ALLOWED_LEADING)}"
        )

    blacklist_hit = _BLACKLIST_RE.search(body)
    if blacklist_hit:
        raise SqlValidationError(
            f"token {blacklist_hit.group(1).upper()!r} is not allowed in "
            "read-only queries"
        )

    return body


def run_readonly(
    storage: Storage,
    q: str,
    *,
    row_cap: int,
    timeout_s: float,
) -> SqlResult:
    """Execute a validated read-only query against the shared connection.

    Row cap is enforced by fetching ``row_cap + 1`` rows and trimming;
    the caller gets a ``truncated`` flag back when more rows were
    available. We avoid wrapping the query with ``LIMIT`` so an
    explicit LIMIT in the user query still wins, and so the columns
    returned exactly match what the user wrote.

    Timeout is enforced by a watcher thread that calls
    ``con.interrupt()`` once ``timeout_s`` seconds have elapsed; the
    DuckDB session-level ``statement_timeout`` varies by build and we
    keep things portable. On interrupt DuckDB raises
    ``InterruptException`` which we translate to ``SqlTimeoutError``.
    """
    if row_cap < 1:
        raise ValueError("row_cap must be >= 1")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0")

    con = storage.conn
    # Share the Storage lock so the poller and the API can't issue
    # concurrent queries on the same connection.
    with storage.lock:
        interrupted = threading.Event()

        def _watcher() -> None:
            if not interrupted.wait(timeout_s):
                try:
                    con.interrupt()
                except Exception as exc:  # noqa: BLE001
                    log.warning("sql: interrupt failed: %s", exc)

        watcher = threading.Thread(target=_watcher, name="sql-timeout", daemon=True)
        watcher.start()

        t0 = time.monotonic()
        try:
            cur = con.execute(q)
        except Exception as exc:  # noqa: BLE001
            interrupted.set()
            watcher.join(timeout=0.1)
            elapsed = time.monotonic() - t0
            if _looks_like_interrupt(exc) or elapsed >= timeout_s:
                raise SqlTimeoutError(f"query exceeded {timeout_s:.0f}s")
            raise SqlExecutionError(_sanitize(exc))

        try:
            columns = [d[0] for d in (cur.description or [])]
            try:
                raw_rows = cur.fetchmany(row_cap + 1)
            except Exception as exc:  # noqa: BLE001
                elapsed = time.monotonic() - t0
                if _looks_like_interrupt(exc) or elapsed >= timeout_s:
                    raise SqlTimeoutError(f"query exceeded {timeout_s:.0f}s")
                raise SqlExecutionError(_sanitize(exc))
        finally:
            interrupted.set()
            watcher.join(timeout=0.1)

        latency_ms = int((time.monotonic() - t0) * 1000)
        truncated = len(raw_rows) > row_cap
        if truncated:
            raw_rows = raw_rows[:row_cap]
        rows = [[_jsonify(v) for v in row] for row in raw_rows]

        return SqlResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            latency_ms=latency_ms,
        )


def _looks_like_interrupt(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    if "interrupt" in name:
        return True
    msg = str(exc).lower()
    return "interrupt" in msg or "cancell" in msg


def _sanitize(exc: BaseException) -> str:
    """Return an error message safe to echo back to the client.

    DuckDB error messages can include the full query text, which the
    caller already has; we truncate to the first line and drop anything
    that looks like an echoed SQL body.
    """
    msg = str(exc)
    first_line = msg.split("\n", 1)[0].strip()
    return first_line[:500]


def _jsonify(v: Any) -> Any:
    """Coerce DuckDB scalar types into JSON-safe primitives."""
    if v is None:
        return None
    if isinstance(v, (bool, int, float, str)):
        return v
    # datetime.datetime and datetime.date both carry .isoformat().
    iso = getattr(v, "isoformat", None)
    if callable(iso):
        return iso()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    # Decimal, HUGEINT, and other exotic numeric types — string-encode
    # so downstream JSON consumers don't lose precision.
    return str(v)

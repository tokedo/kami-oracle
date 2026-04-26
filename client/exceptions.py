"""Exception hierarchy for the kami-oracle client.

``OracleError`` is the root. Catch it for "anything client-related."

- ``OracleAuthError``: 401 — token missing, wrong, or expired.
- ``OracleHTTPError``: any other non-2xx; ``status_code`` distinguishes
  4xx vs 5xx for callers that want to retry transient server errors.

Network errors (DNS, connection, read timeout) bubble up as
``requests.RequestException`` subclasses unchanged — callers that care
about retries can wrap those themselves. v1 has no built-in retries.
"""

from __future__ import annotations


class OracleError(Exception):
    """Base class for all client errors."""


class OracleAuthError(OracleError):
    """Authentication failed (HTTP 401)."""

    def __init__(self, message: str = "authentication failed", *, body: str | None = None) -> None:
        super().__init__(message)
        self.body = body
        self.status_code = 401


class OracleHTTPError(OracleError):
    """Non-401 HTTP error from the oracle.

    ``status_code`` lets callers distinguish 4xx (request was bad) from
    5xx (server failed) without parsing the message.
    """

    def __init__(self, status_code: int, message: str, *, body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body

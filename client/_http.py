"""Thin requests wrapper for the kami-oracle client.

One ``requests.Session`` per ``OracleClient`` so we get connection
reuse without juggling sessions in the public API. Adds the bearer
header on every protected call, maps HTTP status codes to the typed
exception hierarchy, and respects per-call timeouts.
"""

from __future__ import annotations

from typing import Any

import requests

from .exceptions import OracleAuthError, OracleHTTPError


class HttpSession:
    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._session = requests.Session()

    def _timeout(self, override: float | None) -> tuple[float, float] | float:
        if override is not None:
            return override
        return (self._connect_timeout, self._read_timeout)

    def _headers(self, *, auth_required: bool) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if auth_required and self._token is not None:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        auth_required: bool = True,
    ) -> Any:
        r = self._session.get(
            self._base_url + path,
            params=params,
            headers=self._headers(auth_required=auth_required),
            timeout=self._timeout(timeout),
        )
        return _parse(r)

    def post(
        self,
        path: str,
        *,
        json_body: dict[str, Any],
        timeout: float | None = None,
        auth_required: bool = True,
    ) -> Any:
        headers = self._headers(auth_required=auth_required)
        headers["Content-Type"] = "application/json"
        r = self._session.post(
            self._base_url + path,
            json=json_body,
            headers=headers,
            timeout=self._timeout(timeout),
        )
        return _parse(r)


def _parse(r: requests.Response) -> Any:
    if r.status_code == 401:
        raise OracleAuthError(body=_safe_body(r))
    if 400 <= r.status_code < 600:
        raise OracleHTTPError(
            r.status_code,
            f"oracle returned HTTP {r.status_code}",
            body=_safe_body(r),
        )
    try:
        return r.json()
    except ValueError as e:
        raise OracleHTTPError(
            r.status_code,
            f"oracle returned non-JSON body: {e}",
            body=_safe_body(r),
        ) from e


def _safe_body(r: requests.Response) -> str | None:
    try:
        text = r.text
    except Exception:  # noqa: BLE001
        return None
    if len(text) > 4096:
        return text[:4096] + "...[truncated]"
    return text

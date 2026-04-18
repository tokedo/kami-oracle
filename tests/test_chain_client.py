"""Tests for the RPC retry wrapper.

The backfill died in session 2 when `requests.exceptions.ConnectionError`
escaped `TRANSIENT_EXC`. These tests pin the behavior: every transport-
layer fault surfaced by `requests`/`web3`/the stdlib socket layer must be
retried.

No network I/O — `_retry` is exercised directly with synthetic failures.
"""

from __future__ import annotations

import socket

import pytest
import requests.exceptions
from web3.exceptions import Web3Exception

from ingester.chain_client import RetryPolicy, _retry


def _make_flaky(exc: BaseException, succeeds_on_attempt: int = 2):
    """Return a zero-arg callable that raises ``exc`` until ``succeeds_on_attempt``."""
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] < succeeds_on_attempt:
            raise exc
        return "ok"

    return fn, state


FAST_POLICY = RetryPolicy(max_attempts=5, base_delay_s=0.0, max_delay_s=0.0)


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectionError("Remote end closed connection"),
        requests.exceptions.ChunkedEncodingError("incomplete chunked read"),
        requests.exceptions.ReadTimeout("read timed out"),
        requests.exceptions.SSLError("bad handshake"),
        # The generic parent — any future requests exception subclass is caught.
        requests.exceptions.RequestException("generic transport fault"),
        TimeoutError("stdlib timeout"),
        ConnectionError("stdlib connection error"),
        socket.error("raw socket error"),
        Web3Exception("web3-layer transient"),
    ],
)
def test_retry_recovers_from_transient(exc):
    fn, state = _make_flaky(exc, succeeds_on_attempt=2)
    result = _retry(fn, FAST_POLICY, desc="test")
    assert result == "ok"
    assert state["n"] == 2  # failed once, succeeded on retry


def test_retry_reraises_non_transient():
    # ValueError is not a transport fault; the wrapper must not swallow it.
    def fn():
        raise ValueError("programmer error")

    with pytest.raises(ValueError):
        _retry(fn, FAST_POLICY, desc="test")


def test_retry_gives_up_after_max_attempts():
    exc = requests.exceptions.ConnectionError("permanent")

    def fn():
        raise exc

    with pytest.raises(requests.exceptions.ConnectionError):
        _retry(fn, FAST_POLICY, desc="test")

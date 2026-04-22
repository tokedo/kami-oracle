"""Yominet RPC wrapper with retry / backoff."""

from __future__ import annotations

import logging
import random
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import requests.exceptions
from web3 import Web3
from web3.exceptions import Web3Exception
from web3.types import BlockData, TxData, TxReceipt

log = logging.getLogger(__name__)

T = TypeVar("T")

# RPC faults we treat as transient. Non-transient faults (malformed request,
# reverted view call) bubble up to the caller.
#
# The session-2 backfill died on `requests.exceptions.ConnectionError`
# ("Remote end closed connection without response") after ~6h — it was not
# covered by the previous narrower tuple. `RequestException` is the parent
# of ConnectionError / ChunkedEncodingError / ReadTimeout / SSLError and
# covers every transport-layer fault web3's HTTPProvider can raise.
TRANSIENT_EXC = (
    ConnectionError,              # stdlib (subclass of OSError on py3.3+)
    TimeoutError,                 # stdlib
    socket.error,                 # stdlib, covers raw socket faults
    requests.exceptions.RequestException,
    Web3Exception,
)


@dataclass
class RetryPolicy:
    # Tuned for long-running backfills against the public Yominet RPC. A
    # multi-day backfill traverses millions of RPC calls; transient
    # timeouts are inevitable. 8 attempts × exp-backoff capped at 60s
    # rides out most short outages without exploding wall time.
    max_attempts: int = 8
    base_delay_s: float = 0.5
    max_delay_s: float = 60.0


def _retry(fn: Callable[[], T], policy: RetryPolicy, desc: str) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except TRANSIENT_EXC as e:
            attempt += 1
            if attempt >= policy.max_attempts:
                log.error("rpc: %s failed after %d attempts: %s", desc, attempt, e)
                raise
            delay = min(
                policy.max_delay_s,
                policy.base_delay_s * (2 ** (attempt - 1)),
            )
            # Jitter: ±25% to avoid thundering-herd against the public RPC.
            delay *= 0.75 + random.random() * 0.5
            log.warning(
                "rpc: %s attempt %d failed (%s); retrying in %.2fs",
                desc, attempt, e, delay,
            )
            time.sleep(delay)


class ChainClient:
    """Thin wrapper around ``web3.py`` with retry on transient RPC faults."""

    def __init__(self, rpc_url: str, retry: RetryPolicy | None = None):
        self.rpc_url = rpc_url
        self.retry = retry or RetryPolicy()
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))

    def is_connected(self) -> bool:
        try:
            return bool(self.w3.is_connected())
        except TRANSIENT_EXC:
            return False

    def chain_id(self) -> int:
        return _retry(lambda: self.w3.eth.chain_id, self.retry, "chain_id")

    def block_number(self) -> int:
        return _retry(lambda: self.w3.eth.block_number, self.retry, "block_number")

    def get_block(self, n: int, *, full: bool = True) -> BlockData:
        return _retry(
            lambda: self.w3.eth.get_block(n, full_transactions=full),
            self.retry,
            f"get_block({n})",
        )

    def get_tx_receipt(self, tx_hash: Any) -> TxReceipt:
        return _retry(
            lambda: self.w3.eth.get_transaction_receipt(tx_hash),
            self.retry,
            "get_tx_receipt",
        )

    def get_transaction(self, tx_hash: Any) -> TxData:
        return _retry(
            lambda: self.w3.eth.get_transaction(tx_hash),
            self.retry,
            "get_transaction",
        )

    def call_contract_fn(
        self,
        contract: Any,
        fn_name: str,
        *args: Any,
        block_identifier: int | str | None = None,
    ) -> Any:
        if block_identifier is None:
            return _retry(
                lambda: getattr(contract.functions, fn_name)(*args).call(),
                self.retry,
                f"{fn_name}(...)",
            )
        return _retry(
            lambda: getattr(contract.functions, fn_name)(*args).call(
                block_identifier=block_identifier
            ),
            self.retry,
            f"{fn_name}(...)@{block_identifier}",
        )

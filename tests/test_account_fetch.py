"""Tests for the GetterSystem.getAccount fan-out in kami_static.

We exercise three behaviours:

1. ``KamiStaticReader.fetch_account`` caches lookups by ``account_id`` so
   a population pass across N kamis sharing one account makes one chain
   call, not N. Critical for the bpeon case (one operator owns dozens of
   kamis).
2. A revert / RPC fault on ``getAccount`` resolves to ``(None, None)``
   instead of failing the whole pass — anonymous and unhydrated accounts
   are tolerable.
3. An empty name string from the chain (``getAccount`` default shape for
   un-named accounts) maps to NULL ``account_name``.

The bpeon end-to-end check (does ``account_name`` actually come back as
"bpeon") is a separate live-DB cross-check recorded in
``memory/decoder-notes.md`` after the backfill runs — not gated by
unit tests because the RPC is the source of truth.
"""

from __future__ import annotations

from ingester.kami_static import KamiStaticReader


class _StubFn:
    def __init__(self, parent, fn_name):
        self.parent = parent
        self.fn_name = fn_name

    def __call__(self, *args):
        # Mimic web3 contract function -> .call() chain: store args, return self.
        self._args = args
        return self

    def call(self):  # noqa: D401
        return self.parent._call(self.fn_name, self._args)


class _StubFunctions:
    def __init__(self, parent):
        self._parent = parent

    def __getattr__(self, name):
        return _StubFn(self._parent, name)


class _StubContract:
    """Records every getAccount call so the test can assert call count.

    ``shape_for(account_id_int)`` returns the (index, name, stamina, room)
    tuple to hand back; raises ``RuntimeError`` if not registered to
    simulate a revert.
    """

    def __init__(self, shapes: dict[int, tuple]):
        self._shapes = shapes
        self.calls: list[tuple[str, tuple]] = []
        self.functions = _StubFunctions(self)

    def _call(self, fn_name, args):
        self.calls.append((fn_name, args))
        if fn_name != "getAccount":
            raise RuntimeError(f"unexpected fn {fn_name}")
        (acct_id_int,) = args
        if acct_id_int not in self._shapes:
            raise RuntimeError(f"revert: account {acct_id_int}")
        return self._shapes[acct_id_int]


class _StubClient:
    """Stand-in for ChainClient that just calls .functions.<fn>(...).call()."""

    def call_contract_fn(self, contract, fn_name, *args, block_identifier=None):
        return getattr(contract.functions, fn_name)(*args).call()


def _make_reader(contract: _StubContract) -> KamiStaticReader:
    reader = KamiStaticReader.__new__(KamiStaticReader)
    reader.client = _StubClient()
    reader.contract = contract
    reader._account_cache = {}
    return reader


def test_fetch_account_caches_per_account_id():
    bpeon_id_int = 766652271399468889391879684419720168355448418214
    contract = _StubContract({bpeon_id_int: (42, "bpeon", 100, 7)})
    reader = _make_reader(contract)

    # 5 kamis, all sharing one account.
    for _ in range(5):
        idx, name = reader.fetch_account(str(bpeon_id_int))
        assert idx == 42
        assert name == "bpeon"

    assert len(contract.calls) == 1, "cache must dedupe within a pass"


def test_fetch_account_revert_returns_none():
    contract = _StubContract({})  # every call reverts
    reader = _make_reader(contract)

    idx, name = reader.fetch_account("12345")
    assert (idx, name) == (None, None), "revert must not propagate"
    # Cache the failure so a flaky account doesn't re-hit the RPC each pass.
    _ = reader.fetch_account("12345")
    assert len(contract.calls) == 1


def test_fetch_account_empty_name_maps_to_null():
    anon_id_int = 9999
    contract = _StubContract({anon_id_int: (0, "", 0, 0)})
    reader = _make_reader(contract)
    idx, name = reader.fetch_account(str(anon_id_int))
    assert idx == 0
    assert name is None, "empty name must become NULL, not the empty string"

"""In-process map of harvest_id → kami_id.

Why this exists: ``system.harvest.stop`` and ``system.harvest.collect`` only
carry the harvest entity id in their calldata (the kami sits behind a
component lookup). Resolving on-chain per-tx via ``IDOwnsKamiComponent`` was
the initial idea, but the entity id is computed deterministically as
``keccak256(b"harvest" || kami_id_be)`` (see
``kamigotchi-context/integration/architecture.md``). So the resolver just
maintains the inverse map computed offline from every kami_id we have ever
observed.

Usage:

    resolver = HarvestResolver()
    resolver.bootstrap_from_db(storage)        # load harvest_start universe
    # ... new actions decoded in process_block_range ...
    resolver.observe_action(action)            # registers harvest_starts
    kid = resolver.resolve(action.harvest_id)  # for stop/collect rows
"""

from __future__ import annotations

import logging
from typing import Iterable

from eth_utils import keccak

from .decoder import DecodedAction
from .storage import Storage

log = logging.getLogger(__name__)


def _harvest_id_for_kami(kami_id_int: int) -> str:
    digest = keccak(b"harvest" + kami_id_int.to_bytes(32, "big"))
    return str(int.from_bytes(digest, "big"))


class HarvestResolver:
    """Stateful map of harvest_id → kami_id, populated from observed kamis."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    # ---------------------------------------------------------------------
    # Population.
    # ---------------------------------------------------------------------

    def register(self, harvest_id: str | None, kami_id: str | None) -> None:
        if not harvest_id or not kami_id:
            return
        # Idempotent — last writer wins, but the map should be bijective
        # in practice. A collision would indicate a kami_id reuse, which
        # we'd want to log loudly, but it can't actually happen given the
        # keccak derivation.
        self._map[harvest_id] = kami_id

    def register_kami(self, kami_id: str | None) -> None:
        """Compute and register harvest_id for a kami we just observed."""
        if not kami_id:
            return
        try:
            kid_int = int(kami_id)
        except (ValueError, TypeError):
            return
        self.register(_harvest_id_for_kami(kid_int), kami_id)

    def bootstrap_from_db(self, storage: Storage) -> int:
        """Pre-warm from every (harvest_id, kami_id) pair already in the DB.

        Pulls from harvest_start rows after migration 002 has populated the
        harvest_id column. Also pulls from any harvest_stop/collect row whose
        kami_id was already stitched (idempotent — same map, same value).
        """
        rows = storage.fetchall(
            """
            SELECT DISTINCT harvest_id, kami_id
            FROM kami_action
            WHERE action_type IN ('harvest_start', 'harvest_stop', 'harvest_collect')
              AND harvest_id IS NOT NULL
              AND kami_id IS NOT NULL
            """
        )
        for hid, kid in rows:
            self._map[str(hid)] = str(kid)

        # Also harvest the kami_id universe from every other action that
        # populates kami_id — this lets us resolve stops whose start was
        # outside the rolling window (the kami might still appear in feed,
        # lvlup, harvest_liquidate, etc.).
        kami_rows = storage.fetchall(
            """
            SELECT DISTINCT kami_id FROM kami_action
            WHERE kami_id IS NOT NULL
            """
        )
        for (kid,) in kami_rows:
            self.register_kami(str(kid))
        log.info(
            "harvest_resolver: bootstrapped — %d harvest_id mappings",
            len(self._map),
        )
        return len(self._map)

    def observe_actions(self, actions: Iterable[DecodedAction]) -> None:
        """Update the map from a freshly decoded batch.

        Any action that carries kami_id contributes a (computed_harvest_id,
        kami_id) entry. harvest_start populates harvest_id directly via the
        decoder's deterministic compute; for completeness we also add every
        other action's kami_id so the universe stays comprehensive.
        """
        for a in actions:
            if a.kami_id:
                self.register_kami(a.kami_id)

    # ---------------------------------------------------------------------
    # Lookup.
    # ---------------------------------------------------------------------

    def resolve(self, harvest_id: str | None) -> str | None:
        if not harvest_id:
            return None
        return self._map.get(str(harvest_id))

    def stitch(self, actions: Iterable[DecodedAction]) -> int:
        """Mutate stop/collect actions in place: set kami_id from the map.

        Returns the number of rows whose kami_id was set.
        """
        n = 0
        for a in actions:
            if a.kami_id is not None:
                continue
            if a.action_type not in ("harvest_stop", "harvest_collect"):
                continue
            kid = self.resolve(a.harvest_id)
            if kid is not None:
                a.kami_id = kid
                n += 1
        return n

    def __len__(self) -> int:
        return len(self._map)

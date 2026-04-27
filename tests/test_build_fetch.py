"""Tests for the Session 10 build-snapshot fan-out in kami_static.

Three behaviours under test:

1. ``_stat_total`` matches the canonical game formula
   ``floor((1000 + boost) * (base + shift) / 1000)`` and clamps at 0.
2. ``_kami_shape_to_static`` extracts ``level``, ``xp``, and the
   formula-resolved ``total_health/power/harmony/violence`` from the
   GetterSystem.getKami(...) tuple — including a Zephyr fixture pulled
   from the live Session 10 discovery dump (level=37, total_health=230,
   total_power=16, total_harmony=19, total_violence=17). The chain dump
   is the source of truth; if these values change in the live game,
   that's a real meta shift, not a code bug.
3. ``KamiStaticReader._fetch_build_extras`` enumerates skills correctly
   via the IDOwnsSkillComponent + IndexSkillComponent + SkillPointComponent
   triple and returns ``skills_json`` as a JSON-encoded list of
   ``{index, points}`` dicts. Per-component failures are tolerated
   (column ends up absent, not a propagating exception).

The bpeon end-to-end check (does the live populator actually reproduce
the Zephyr build) is recorded in ``memory/decoder-notes.md`` after the
backfill runs — not gated by unit tests because the RPC is the source
of truth.
"""

from __future__ import annotations

import json

from ingester.kami_static import (
    KamiStaticReader,
    _kami_shape_to_static,
    _stat_total,
)


def test_stat_total_matches_canonical_formula():
    # Effective = max(0, floor((1000 + boost) * (base + shift) / 1000))
    # Zephyr health: base=90, shift=140, boost=0 -> 230
    assert _stat_total(90, 140, 0) == 230
    # Zephyr power: base=16, shift=0, boost=0 -> 16
    assert _stat_total(16, 0, 0) == 16
    # Boost applies as multiplier: base=10, shift=0, boost=500 -> floor(1500*10/1000) = 15
    assert _stat_total(10, 0, 500) == 15
    # Negative shift can drive base+shift below 0 — clamp at 0, don't go negative.
    assert _stat_total(5, -10, 0) == 0
    # Slots: all zeros -> 0 (Zephyr's actual on-chain value)
    assert _stat_total(0, 0, 0) == 0


def test_kami_shape_round_trip_zephyr_fixture():
    """Run the live Zephyr getKami() shape through _kami_shape_to_static.

    Values pulled from the Session 10 discovery dump
    (memory/session-10-discovery.txt) — same chain query the populator runs.
    """
    zephyr_id = "28257207240752812050526875800976233322376494609598859084860556459780762796410"
    zephyr_account_int = 766652271399468889391879684419720168355448418214

    # (id, index, name, mediaURI, stats, traits, affinities,
    #  account, level, xp, room, state)
    shape = (
        int(zephyr_id),
        43,
        "Zephyr",
        "ipfs://...",
        (
            (90, 140, 0, 78),     # health: base, shift, boost, sync
            (16, 0, 0, 0),         # power
            (11, 8, 0, 0),         # harmony
            (17, 0, 0, 0),         # violence
        ),
        (1, 2, 3, 4, 5),           # face, hand, body, background, color (irrelevant for this test)
        ["NORMAL", "EERIE"],
        zephyr_account_int,
        37,                        # level
        136367,                    # xp
        16,                        # room
        "RESTING",
    )

    row = _kami_shape_to_static(zephyr_id, shape)
    assert row.kami_index == 43
    assert row.name == "Zephyr"
    assert row.account_id == str(zephyr_account_int)
    assert row.affinities == ["NORMAL", "EERIE"]
    assert row.level == 37
    assert row.xp == 136367
    assert row.base_health == 90
    assert row.base_power == 16
    assert row.base_harmony == 11
    assert row.base_violence == 17
    assert row.total_health == 230
    assert row.total_power == 16
    assert row.total_harmony == 19
    assert row.total_violence == 17
    # total_health > base_health because skill shift = 140 — sanity that we're
    # reading the effective total, not just the base.
    assert row.total_health > row.base_health


# ---------------------------------------------------------------------------
# _fetch_build_extras — stub the per-component contracts.
# ---------------------------------------------------------------------------


class _StubFn:
    def __init__(self, parent, fn_name):
        self.parent = parent
        self.fn_name = fn_name

    def __call__(self, *args):
        self._args = args
        return self

    def call(self):
        return self.parent._call(self.fn_name, self._args)


class _StubFunctions:
    def __init__(self, parent):
        self._parent = parent

    def __getattr__(self, name):
        return _StubFn(self._parent, name)


class _StubContract:
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[tuple[str, tuple]] = []
        self.functions = _StubFunctions(self)

    def _call(self, fn_name, args):
        self.calls.append((fn_name, args))
        return self._handler(fn_name, args)


class _StubClient:
    def call_contract_fn(self, contract, fn_name, *args, block_identifier=None):
        return getattr(contract.functions, fn_name)(*args).call()


def _make_reader_with_components(components: dict) -> KamiStaticReader:
    """Bypass KamiStaticReader.__init__ so we can install pre-built stubs."""
    reader = KamiStaticReader.__new__(KamiStaticReader)
    reader.client = _StubClient()
    reader.contract = None
    reader._account_cache = {}
    reader._build_components = components
    return reader


def test_fetch_build_extras_skills_enumeration():
    # Two skill instances, each with a (index, points) record on chain.
    skill_data = {
        111111: {"index": 212, "points": 5},
        222222: {"index": 311, "points": 3},
    }

    def skills_owns_handler(fn, args):
        assert fn == "getEntitiesWithValue"
        (kid,) = args
        assert kid == 999
        return list(skill_data.keys())

    def skill_index_handler(fn, args):
        assert fn == "safeGet"
        (sid,) = args
        return skill_data[sid]["index"]

    def skill_point_handler(fn, args):
        assert fn == "safeGet"
        (sid,) = args
        return skill_data[sid]["points"]

    def slots_handler(fn, args):
        assert fn == "safeGet"
        # Zephyr's real slots tuple: all zeros.
        return (0, 0, 0, 0)

    def equip_owns_handler(fn, args):
        return []  # no equipment

    def item_index_handler(fn, args):
        raise AssertionError("item_index should not be called when no equips")

    components = {
        "slots": _StubContract(slots_handler),
        "skills_owns": _StubContract(skills_owns_handler),
        "skill_index": _StubContract(skill_index_handler),
        "skill_point": _StubContract(skill_point_handler),
        "equip_owns": _StubContract(equip_owns_handler),
        "item_index": _StubContract(item_index_handler),
    }
    reader = _make_reader_with_components(components)

    extras = reader._fetch_build_extras(999)
    assert extras["total_slots"] == 0
    skills = json.loads(extras["skills_json"])
    # Order is preserved from the enumeration; assert as a set for stability.
    assert {(s["index"], s["points"]) for s in skills} == {(212, 5), (311, 3)}
    assert extras["equipment_json"] == "[]"


def test_fetch_build_extras_tolerates_per_component_failure():
    """If skills_owns reverts, skills_json must end up absent from the result
    instead of raising — the caller (KamiStaticReader.fetch) treats absent
    keys as NULL columns. Slots / equipment in the same call should still
    succeed independently."""

    def slots_handler(fn, args):
        return (0, 0, 0, 0)

    def skills_owns_handler(fn, args):
        raise RuntimeError("revert: kami transferred mid-fetch")

    def equip_owns_handler(fn, args):
        return [55555]

    def item_index_handler(fn, args):
        return 1234

    components = {
        "slots": _StubContract(slots_handler),
        "skills_owns": _StubContract(skills_owns_handler),
        "skill_index": _StubContract(lambda f, a: 0),
        "skill_point": _StubContract(lambda f, a: 0),
        "equip_owns": _StubContract(equip_owns_handler),
        "item_index": _StubContract(item_index_handler),
    }
    reader = _make_reader_with_components(components)

    extras = reader._fetch_build_extras(7)
    assert extras["total_slots"] == 0
    assert "skills_json" not in extras  # absent on per-component failure
    assert json.loads(extras["equipment_json"]) == [1234]


# ---------------------------------------------------------------------------
# Session 11 — modifier catalog walk on Zephyr.
# ---------------------------------------------------------------------------


def _load_real_catalog():
    from pathlib import Path

    from ingester.skill_catalog import load_skill_catalog

    return load_skill_catalog(Path(__file__).resolve().parent.parent / "kami_context" / "catalogs")


def test_modifiers_zephyr_catalog_walk():
    """Bpeon Zephyr (kami #43) round-trip via the catalog.

    Skills + equipment from Session 10 / 11 discovery dump:
    skills [{212,5},{222,5},{223,5},{232,1},{311,5},{312,5},{323,5},
            {331,1},{322,4},{313,1}], equipment []. Expected modifier
    columns recorded in memory/session-11-discovery.txt — these come from
    walking the upstream catalog and must round-trip exactly to the
    populator's stored values.
    """
    cat = _load_real_catalog()
    skills_json = json.dumps([
        {"index": 212, "points": 5},
        {"index": 222, "points": 5},
        {"index": 223, "points": 5},
        {"index": 232, "points": 1},
        {"index": 311, "points": 5},
        {"index": 312, "points": 5},
        {"index": 323, "points": 5},
        {"index": 331, "points": 1},
        {"index": 322, "points": 4},
        {"index": 313, "points": 1},
    ])
    mods = cat.compute_modifiers(skills_json, "[]")
    assert mods == {
        "strain_boost": -125,             # 223 Concentration: -25 ×1000 × 5
        "harvest_fertility_boost": 0,
        "harvest_intensity_boost": 20,    # 232 Warmup ×1×15 + 313 Patience ×1×5
        "harvest_bounty_boost": 0,
        "rest_recovery_boost": 0,
        "cooldown_shift": 0,
        "attack_threshold_shift": 0,
        "attack_threshold_ratio": 0,
        "attack_spoils_ratio": 0,
        "defense_threshold_shift": 200,   # 222 Meditative ×5×20 + 323 Armor ×5×20
        "defense_threshold_ratio": 0,
        "defense_salvage_ratio": 0,
    }


def test_modifiers_strain_boost_sign_handling():
    """SB / CS / threshold-shift columns must round-trip negative values.

    Catches the sign-handling bug class — if anyone introduces an ABS or
    UINT cast in the catalog parser, this test breaks. SB on Zephyr is
    -125; an enlightened-tree-heavy build would be more negative.
    """
    cat = _load_real_catalog()
    # Hypothetical max-strain-reduction build: skill 263 Immortality
    # (SB -0.125 ×1 max=1) + skill 223 Concentration (SB -0.025 ×5 max=5)
    # + skill 243 Endurance (SB -0.025 ×5 max=5) → -125 + -125 + -125 = -375
    skills_json = json.dumps([
        {"index": 263, "points": 1},
        {"index": 223, "points": 5},
        {"index": 243, "points": 5},
    ])
    mods = cat.compute_modifiers(skills_json, "[]")
    assert mods["strain_boost"] < 0
    assert mods["strain_boost"] == -375

    # CS sign: skill 163 Assassin -50 sec ×1 — must store as int(-50).
    cs_skills = json.dumps([{"index": 163, "points": 1}])
    cs_mods = cat.compute_modifiers(cs_skills, "[]")
    assert cs_mods["cooldown_shift"] == -50


def test_modifiers_equipment_walk():
    """An equipped item that grants a modifier must contribute its
    catalog value. Item 30007 (Mask of Mischief, E_ASR+6%) → 60 ×1000."""
    cat = _load_real_catalog()
    # Empty skills, one equipped Mask of Mischief.
    mods = cat.compute_modifiers("[]", json.dumps([30007]))
    assert mods["attack_spoils_ratio"] == 60

    # Equipment that grants a stat shift (E_HEALTH+30 → SHS) must NOT
    # appear in the 12 modifier columns — those four are already in
    # total_*. Only the 12 non-stat keys are surfaced here.
    health_only = cat.compute_modifiers("[]", json.dumps([30016]))  # Old Critter, E_HEALTH+30
    assert all(v == 0 for v in health_only.values())


def test_modifiers_zero_floor_when_no_skills_or_equipment():
    """A kami with no skills + no equipment must get all-zero modifiers,
    not NULL — the populator distinguishes 0 (no investment) from NULL
    (compute failed)."""
    cat = _load_real_catalog()
    mods = cat.compute_modifiers("[]", "[]")
    assert mods == {
        "strain_boost": 0,
        "harvest_fertility_boost": 0,
        "harvest_intensity_boost": 0,
        "harvest_bounty_boost": 0,
        "rest_recovery_boost": 0,
        "cooldown_shift": 0,
        "attack_threshold_shift": 0,
        "attack_threshold_ratio": 0,
        "attack_spoils_ratio": 0,
        "defense_threshold_shift": 0,
        "defense_threshold_ratio": 0,
        "defense_salvage_ratio": 0,
    }

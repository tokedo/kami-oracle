"""Static skill + equipment effect catalog → per-kami modifier sums.

Loads two CSVs vendored from upstream Kamigotchi:

    kami_context/catalogs/skills.csv  — per-skill (Index, Effect, Value, Units)
    kami_context/catalogs/items.csv   — per-equipment-item (Index, Effects)

Provides:

    load_skill_catalog(abi_dir_or_path) -> SkillCatalog
        Loaded once at populator startup; immutable for the entire
        population pass. Same caching shape as the per-pass account_id
        cache from Session 9.

    SkillCatalog.compute_modifiers(skills_json, equipment_json) -> dict[col, int]
        Given a kami's skills_json (Session 10 shape:
        ``[{"index": int, "points": int}, ...]``) and equipment_json
        (``[item_index, ...]``), returns the 12 modifier column values.

The 4 stat-shift effects (SHS / SPS / SVS / SYS) are intentionally NOT
exported — they're already folded into Session 10's
``total_health/power/violence/harmony`` via getKami(id).stats and
re-emitting them here would double-count.

The catalog → chain pipeline is faithful (validated on Zephyr — see
``memory/decoder-notes.md`` "Session 11 — skill-effect modifiers on
chain"), so the catalog walk produces the resolved totals the game
itself uses for the 12 non-stat modifiers.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Effect keys → kami_static column name. The 4 stat-shift effects
# (SHS/SPS/SVS/SYS) are intentionally absent — those are already in
# Session 10's total_*.
EFFECT_TO_COLUMN: dict[str, str] = {
    "SB":  "strain_boost",
    "HFB": "harvest_fertility_boost",
    "HIB": "harvest_intensity_boost",
    "HBB": "harvest_bounty_boost",
    "RMB": "rest_recovery_boost",
    "CS":  "cooldown_shift",
    "ATS": "attack_threshold_shift",
    "ATR": "attack_threshold_ratio",
    "ASR": "attack_spoils_ratio",
    "DTS": "defense_threshold_shift",
    "DTR": "defense_threshold_ratio",
    "DSR": "defense_salvage_ratio",
}

# All 12 modifier column names — used to ensure every column is set
# (default 0) even when no skill / equipment contributes.
ALL_MODIFIER_COLUMNS: tuple[str, ...] = tuple(EFFECT_TO_COLUMN.values())

# Effects whose stored value is signed seconds, not a ×1000 percent.
INTEGER_UNIT_EFFECTS: frozenset[str] = frozenset({"HIB", "CS"})

# Equipment-effect key prefixes in items.csv map to canonical effect
# keys. ``E_HEALTH``/``E_POWER``/``E_HARMONY``/``E_VIOLENCE`` resolve
# to the four stat-shift effects (SHS/SPS/SYS/SVS) — we do NOT emit
# those (they're in total_*). ``E_BOUNTY`` is a synonym for HBB.
EQUIP_KEY_ALIASES: dict[str, str] = {
    "E_HEALTH": "SHS",
    "E_POWER": "SPS",
    "E_HARMONY": "SYS",
    "E_VIOLENCE": "SVS",
    "E_BOUNTY": "HBB",
    "E_HFB": "HFB",
    "E_HIB": "HIB",
    "E_HBB": "HBB",
    "E_DTS": "DTS",
    "E_DSR": "DSR",
    "E_ATS": "ATS",
    "E_ASR": "ASR",
    # Some catalog entries omit the E_ prefix (RMB).
    "RMB": "RMB",
}

# Pattern: optional E_ prefix, key, signed numeric, optional %/P suffix.
# Examples: E_DTS+6%, RMB+15%, E_HEALTH+30, E_HIB+15P, E_HIB+20.
_EQUIP_EFFECT_RE = re.compile(
    r"^(?P<key>(?:E_)?[A-Z]+)\s*(?P<sign>[+-])?\s*(?P<num>\d+(?:\.\d+)?)(?P<suffix>[%P]?)$",
)


def _scale_value(effect: str, raw_value: float) -> int:
    """Convert a catalog ``Value`` cell into the storage integer.

    HIB / CS keep their raw integer (Musu/hr or seconds, signed).
    Everything else is a percent stored ×1000, rounded half-away-from-zero.
    """
    if effect in INTEGER_UNIT_EFFECTS:
        # HIB values are integer Musu/hr; CS values are integer seconds
        # (sometimes negative). Use round() to defend against any float
        # noise in the CSV.
        return int(round(raw_value))
    # Percent → ×1000.
    return int(round(raw_value * 1000))


@dataclass(frozen=True)
class SkillEntry:
    effect: str       # one of SHS/SPS/SVS/SYS/SB/HFB/HIB/HBB/RMB/CS/ATS/ATR/ASR/DTS/DTR/DSR
    value_per_pt: int  # storage-scaled (×1000 for percent, raw for HIB/CS/Stat)


@dataclass(frozen=True)
class EquipEntry:
    effect: str
    value: int        # storage-scaled, level-1 (no per-point multiplier)


@dataclass
class SkillCatalog:
    """In-memory catalogs keyed by index.

    ``skills`` maps skill_index → SkillEntry. ``equipment`` maps
    item_index → list[EquipEntry] (one item can declare multiple
    effects, though in the current catalog every equipment row carries
    exactly one).
    """
    skills: dict[int, SkillEntry] = field(default_factory=dict)
    equipment: dict[int, list[EquipEntry]] = field(default_factory=dict)

    def compute_modifiers(
        self,
        skills_json: str | None,
        equipment_json: str | None,
    ) -> dict[str, int]:
        """Sum per-effect modifier values for one kami.

        Returns a dict mapping every one of the 12 modifier column names
        to its summed integer value (defaulting to 0). Stat-shift effects
        (SHS/SPS/SVS/SYS) are dropped silently — they're already in
        total_health/power/violence/harmony.

        ``skills_json`` and ``equipment_json`` are the strings stored on
        ``kami_static`` by the Session 10 populator. Missing / unparseable
        strings yield all-zero output rather than raising — the caller
        treats a population pass with a bad input as a soft failure.
        """
        import json

        totals: dict[str, int] = {col: 0 for col in ALL_MODIFIER_COLUMNS}

        if skills_json:
            try:
                skills = json.loads(skills_json)
            except (ValueError, TypeError):
                log.debug("skill_catalog: bad skills_json; skipping skills")
                skills = []
            for s in skills:
                idx = int(s.get("index", -1))
                pts = int(s.get("points", 0))
                if pts == 0:
                    continue
                entry = self.skills.get(idx)
                if entry is None:
                    log.debug("skill_catalog: skill index %d not in catalog", idx)
                    continue
                col = EFFECT_TO_COLUMN.get(entry.effect)
                if col is None:
                    continue  # SHS/SPS/SVS/SYS — already in total_*
                totals[col] += entry.value_per_pt * pts

        if equipment_json:
            try:
                equips = json.loads(equipment_json)
            except (ValueError, TypeError):
                log.debug("skill_catalog: bad equipment_json; skipping equipment")
                equips = []
            for item_idx in equips:
                entries = self.equipment.get(int(item_idx), [])
                for e in entries:
                    col = EFFECT_TO_COLUMN.get(e.effect)
                    if col is None:
                        continue
                    totals[col] += e.value

        return totals


def load_skill_catalog(catalogs_dir: Path | str) -> SkillCatalog:
    """Load and parse skills.csv and items.csv from ``catalogs_dir``.

    ``catalogs_dir`` should point at ``kami_context/catalogs/`` (the
    vendored copy of upstream Kamigotchi catalogs). Returns a populated
    SkillCatalog. Missing files / parse errors raise — catalog load is
    populator-startup-critical.
    """
    catalogs_dir = Path(catalogs_dir)
    skills_path = catalogs_dir / "skills.csv"
    items_path = catalogs_dir / "items.csv"

    cat = SkillCatalog()

    with skills_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                idx = int(row["Index"])
            except (KeyError, ValueError):
                continue
            effect = (row.get("Effect") or "").strip()
            if not effect:
                continue
            try:
                raw_val = float(row.get("Value") or 0)
            except ValueError:
                continue
            scaled = _scale_value(effect, raw_val)
            cat.skills[idx] = SkillEntry(effect=effect, value_per_pt=scaled)

    with items_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("Type") or "").strip() != "Equipment":
                continue
            try:
                idx = int(row["Index"])
            except (KeyError, ValueError):
                continue
            effects_str = (row.get("Effects") or "").strip()
            if not effects_str:
                continue
            entries: list[EquipEntry] = []
            # Multiple effects could be comma-separated; current catalog
            # only ever carries one, but split defensively.
            for tok in (t.strip() for t in effects_str.split(",")):
                if not tok:
                    continue
                m = _EQUIP_EFFECT_RE.match(tok)
                if not m:
                    log.debug("skill_catalog: unparseable equipment effect %r on item %d", tok, idx)
                    continue
                key = m.group("key")
                effect = EQUIP_KEY_ALIASES.get(key)
                if effect is None:
                    log.debug("skill_catalog: unknown equipment effect key %r on item %d", key, idx)
                    continue
                sign = -1 if m.group("sign") == "-" else 1
                raw_num = float(m.group("num")) * sign
                suffix = m.group("suffix")
                # Suffix policy:
                #   '%' -> percent: catalog num is already the integer percent,
                #          stored ×1000 (e.g. "DTS+6%" -> 60). Catalog skill
                #          rows store percent as a decimal (0.06) — different
                #          convention. Equipment rows are integers.
                #   'P' -> Musu/hr (HIB suffix variant) — store raw.
                #   ''  -> raw integer (Stat shift or seconds, no scaling).
                if suffix == "%":
                    if effect in INTEGER_UNIT_EFFECTS:
                        # Defensive: HIB shouldn't carry %; treat as raw.
                        value = int(round(raw_num))
                    else:
                        value = int(round(raw_num * 10))  # %->×1000 means +6% -> 60
                else:
                    value = int(round(raw_num))
                entries.append(EquipEntry(effect=effect, value=value))
            if entries:
                cat.equipment[idx] = entries

    log.info(
        "skill_catalog: loaded %d skills, %d equipment items",
        len(cat.skills), len(cat.equipment),
    )
    return cat

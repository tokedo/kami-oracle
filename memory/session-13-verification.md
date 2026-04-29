# Session 13 — Verification Report

## Service health (live now, 2026-04-29 19:49 UTC)

- `systemctl is-active kami-oracle`: **active**
- `/health` schema version: **8** (target was 8 ✅)
- cursor lag: **10.8s**
- last block: **28217946**
- kami_static rows: **7110**
- items_catalog rows: **177**

## Part 1 — Discovery

- [x] `memory/decoder-notes.md` "Session 13 — items_catalog +
      kami_equipment view" present (commit ea96f5e).
- [x] items.csv schema confirmed (10 columns:
      Index,Name,Type,Rarity,For,Flags,Effects,Requirements,Status,Description).
- [x] Distinct `For` values captured (Type/For breakdown):
      `Equipment|Kami_Pet_Slot=36`, `NFT|Passport_slot=14`,
      `Material|=41`, `Food|Kami=39`, `Key Item|=8`, `Lootbox|Account=6`,
      `Potion|Kami=5`, `Potion|Enemy_Kami=5`, `Food|Account=5`,
      `Misc|=4`, `Tool|=3`, `Revive|Kami=2`, `Misc|Kami=2`,
      `Key Item|Account=2`, `Potion|Account=1`, `Misc|Account=1`,
      `ERC20|=1`, `Consumable|Kami=1`, `Consumable|Account=1`.
      Slot-bearing: 36 (Kami_Pet_Slot) + 14 (Passport_slot) = 50.
- [x] 30011 (Wise Leafling) + 30031 (Antique Automata) + sample
      non-pet rows captured in `memory/session-13-discovery.txt`.
- [x] Commits: `ea96f5e docs: session 13 discovery — items.csv shape
      + slot-resolution rule`.

## Part 2 — items_catalog

- [x] `migrations/007_add_items_catalog.py` added.
- [x] `schema/schema.sql` updated with `items_catalog` table block.
- [x] `SCHEMA_VERSION` 6 → 7 (in `ingester/storage.py`).
- [x] Loader `ingester/items_catalog.py` added; wired into
      `ingester/serve.py` startup (idempotent skip when populated).
- [x] Loader test passes — `tests/test_items_catalog.py`:
      `7 passed in 0.50s` (test_resolve_slot_type, test_parse_items_csv,
      test_load_items_catalog_into_db, test_load_is_idempotent,
      test_ensure_loaded_skips_when_populated,
      test_ensure_loaded_loads_when_empty, test_missing_csv_raises).
- [x] Initial load: **177 rows** (matches CSV row count
      `wc -l items.csv = 178` minus 1 header). Loaded automatically
      on the post-migration restart at 2026-04-29 19:42:31.
- [x] Slot distribution:

      | slot_type      |   n |
      |----------------|----:|
      | (NULL)         | 127 |
      | Kami_Pet_Slot  |  36 |
      | Passport_slot  |  14 |

- [x] 30011 / 30031 spot-check passes:
      - 30011 → name="Wise Leafling", slot_type="Kami_Pet_Slot",
        effect="E_BOUNTY+16%".
      - 30031 → name="Antique Automata", slot_type="Kami_Pet_Slot",
        effect="E_HIB+15P".
- [x] Commits: `6d4a8bc harness: schema migration v7 +
      items_catalog table + loader`.

## Part 3 — kami_equipment view

- [x] `migrations/008_add_kami_equipment_view.py` added.
- [x] `SCHEMA_VERSION` 7 → 8.
- [x] `schema/schema.sql` documents the view shape and the 36h
      threshold.
- [x] View test passes — `tests/test_kami_equipment_view.py`:
      `6 passed in 0.40s` (test_view_resolves_single_pet_with_freshness,
      test_view_flags_stale_kami, test_view_unnests_multi_item_kami,
      test_view_excludes_empty_and_null_equipment,
      test_view_total_row_count, test_view_unresolved_items_get_null_slot).
- [x] Total view rows: **405**; kamis_with_equip: **405**.
      Matches Part 0 baseline (`with_equip = 405` on `kami_static`).
- [x] `unresolved` (slot_type IS NULL) count: **0** ✅ — every
      equipped item resolves through `items_catalog`.
- [x] Slot mix in the wild:

      | slot_type      |   n |
      |----------------|----:|
      | Kami_Pet_Slot  | 405 |

      (Passport_slot has zero equipped instances in the current
      snapshot — passports are NFT-flavored and not commonly held
      by kamis the populator has hydrated. The view *would* surface
      them when present; this is data-shape, not a view defect.)
- [x] Staleness summary:

      | stale_rows | fresh_rows | oldest_seconds |
      |-----------:|-----------:|---------------:|
      |          0 |        405 |          82372 |

      Oldest snapshot is ~22.9h, well under the 36h stale threshold.
- [x] Spot-check on agent-reported stale-pet kamis (1186 / 1745 /
      2418 / 2465): **0 view rows returned**. Cross-checked against
      `kami_static`:
      - 1186 (Killchain): equipment_json `[]`, build_refreshed_ts
        2026-04-28T22:30 — populator has re-fetched since the
        agent's pass and the chain reports no equipped pet now.
      - 2418 (QUU): equipment_json `[]`, build_refreshed_ts
        2026-04-28T23:29 — same, empty equipment on chain.
      - 1745 / 2465: not present in `kami_static` at all. These are
        the canonical Pain 1b cases (kami appears in `kami_action`
        but populator hasn't created the static row yet). That gap
        is queued for **Session 17** (populator-side new-kami
        auto-populate); out of scope for Session 13.
      The view's exclusion of these rows is correct behaviour:
      kami_equipment shows only currently-equipped items per the
      latest snapshot, and "equipment was once present at a stale
      build_refreshed_ts" is exactly what the agent must learn to
      verify against live chain via Kamibots before destructive ops
      — not something the oracle should retain.
- [x] Real-data sanity (top 5 most-equipped items, joined live via
      the view):

      | item_name            | item_effect    |  n |
      |----------------------|----------------|---:|
      | Mask of Contempt     | E_DTS+6%       | 50 |
      | Old Critter          | E_HEALTH+30    | 43 |
      | Antique Ledger       | E_HARMONY+3    | 40 |
      | Antique Automata     | E_HIB+15P      | 27 |
      | Old Gumdrop          | RMB+15%        | 27 |

- [x] Commits: `5921333 harness: schema migration v8 +
      kami_equipment view`.

## Part 4 — Documentation

- [x] README updated — new "Slot-resolved equipment: items_catalog +
      kami_equipment (Session 13)" section with sample SQL.
- [x] `memory/decoder-notes.md` Session 13 section promoted (source
      path, loader trigger, slot_type → NULL convention, 36h
      threshold rationale).
- [x] `CLAUDE.md` schema summary lists `items_catalog` and
      `kami_equipment` alongside `kami_static`.
- [x] Commits: `c35c8c6 docs: README + CLAUDE.md — items_catalog +
      kami_equipment view (session 13)`,
      `ea96f5e docs: session 13 discovery` (covered the
      decoder-notes promotion).

## Part 5 — Hand-off

- [x] `memory/next-steps.md` carries the `oracle.md` diff (5a) —
      refresh-cadence callout, new `kami_equipment` section with
      example query and `is_stale` guidance, replacement for the
      "raw item indices" caveat in `equipment_json`.
- [x] `memory/next-steps.md` carries the Session 14+ candidate
      framing (5b): Session 14 catalogs+views, Session 15
      account_static, Session 16 kami_last_known_state, Session 17
      populator-side new-kami auto-populate (with the explicit
      "no HTTP write endpoint, no live-derivation view" rejections
      attached), Session 18 oracle.md cookbook (docs only); plus
      the deferred / out list (musu/hour, predator-threat,
      cross-account inventory, strategy-membership, operator
      nonce indicator).
- [x] The four Session 13 rejections (no kami_pet_inferred view,
      no HTTP write/refresh endpoint, no freshness columns on
      kami_static, no pet-specific surfaces) are duplicated into
      the queue so future sessions inherit them.
- [x] Commits: `2987177 docs: next-steps — Session 13 hand-off +
      Session 14+ queue framing`.

## Coverage check (regression guard)

`SELECT COUNT(...) FROM kami_static` per column, total = 7110:

| column                  | non-NULL | coverage |
|-------------------------|---------:|---------:|
| account_name (S9)       |     7110 |   100.0% |
| level (S10)             |     7110 |   100.0% |
| skills_json (S10)       |     7110 |   100.0% |
| equipment_json (S10)    |     7110 |   100.0% |
| strain_boost (S11)      |     7110 |   100.0% |
| body_affinity (S12)     |     7110 |   100.0% |
| hand_affinity (S12)     |     7110 |   100.0% |

No regression on Sessions 9 / 10 / 11 / 12.

Full test suite: `119 passed, 12 skipped, 1 warning in 21.89s`
(no pre-existing tests broken).

## Known issues

None.

## Status

✅ **Session 13 complete.** `items_catalog` (177 rows, 50 slot-
bearing) and `kami_equipment` view (405 rows, 0 unresolved) are
populated; agents can query slot-resolved equipment with
freshness flags via a single SQL call. No regression on Sessions
9 / 10 / 11 / 12. Hand-off to founder for the `oracle.md` doc
edits and the Session 14+ queue is in `memory/next-steps.md`.
The four scope rejections (no live-derivation view, no HTTP
write endpoint, no freshness columns on `kami_static`, no
pet-specific surfaces) held end-to-end.

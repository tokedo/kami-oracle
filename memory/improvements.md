# Improvements log

One-line entries per session describing what the harness learned about
itself. Commit hashes are filled in after the commit.

## Session 1 — 2026-04-17

- Built the Stage 1 skeleton: `ingester/{config,chain_client,system_registry,decoder,storage,ingest,poller,backfill,prune}.py`, `schema/schema.sql`, `scripts/validate_decode.py`, `tests/test_decoder.py`.
- Discovered vendored JSON ABIs are partial vs. live chain; added a small `SYSTEM_ABI_OVERLAY` in `decoder.py` carrying documented-but-missing signatures (cited against `kami_context/system-ids.md`) — got decode coverage from ~43% → ~98% on a 500-block sample.
- Decoder design: per-system **explicit field map** (`SYSTEM_FIELD_MAP`) rather than generic arg-name inference. Ambiguous names (`id`, `targetID`, `holderID`) mean different things per system; explicit mapping avoids mis-decodes.
- Decoded batched calls (`executeBatched(uint256[] ...)`) fan out to one `kami_action` row per array element, with `sub_index` as the row key suffix. A single tx commonly produces 2–8 rows.
- Left one ABI question open for human review: `system.craft` on-chain uses a 2-arg executeTyped that contradicts both the vendored JSON and the docs (see `memory/questions-for-human.md`).

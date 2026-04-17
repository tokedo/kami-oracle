# Questions for human — Session 1

## 1. `system.craft` has an undocumented 2-arg signature on-chain — confirm?

Vendored ABI (`kami_context/abi/CraftSystem.json`) and docs
(`kami_context/system-ids.md`) both say:
```
executeTyped(uint256 assignerID, uint32 index, uint256 amt)
```

But every `system.craft` tx on-chain right now uses selector `0x5c817c70`,
which hashes to `executeTyped(uint32, uint256)`. Example calldata decodes
to `(34, 2)`, `(29, 1)` etc. — small numbers plausibly matching
`(recipe_index, amount)`.

**Can you confirm** that the current on-chain sig is `executeTyped(uint32
recipe_index, uint256 amount)` and that `assignerID` was removed? If yes,
I'll:
- Add `executeTyped(uint32,uint256)` to `SYSTEM_ABI_OVERLAY` with args
  named `index` (uint32) and `amt` (uint256) — which drop into the existing
  `system.craft` field map as `("metadata", "recipe_index")` and `amount`.
- Update `memory/decoder-notes.md`.

Sample unknown craft txs for review:
- `0x62a99c841e20b14e3f033bebc4fbd894fd9bcf4302f367057133b38fb88804bf`
- `0x67fb42b712484fba885cd2d266194bf97554af2dc05a19f45be1e0144444d40c`

Alternative: re-vendor `kami_context` from upstream so the JSON ABI is
current — and I'll drop the overlay.

## 2. bpeon kami IDs for spot-validation

CLAUDE.md says the founder's bpeon accounts on kami-zero harvest node 47
under Kamibots `auto_v2`. In a 2000-block scan of Yominet I saw only 2
harvest_start txs on node 47 from one operator
(`0x247f161Ff635C106D3c0850A1B3D64D0C6f60F3B`, 2 unique kamis) — not a
bpeon-like pattern. Top nodes by volume were 86 (451), 35 (161), 16
(128), 9 (57).

Possibilities: (a) bpeons are dormant right now; (b) the meta moved and
they harvest elsewhere; (c) kami-zero uses a different chain/RPC than
the public Yominet endpoint configured in `.env`; (d) CLAUDE.md info is
stale.

**Can you provide**:
- Either the specific kami IDs of the bpeon accounts (preferred — decimal
  strings are fine), or
- The operator wallet addresses they transact from, or
- Confirmation that kami-zero is on the same Yominet chain we're scanning.

I'll then re-run spot-validation and record in `memory/decoder-notes.md`.

## 3. ABI overlay policy — OK to extend further?

For Session 1 I added three overlay entries (`harvest.start` 4-arg
variants, `harvest.stop` / `harvest.collect` batched variants) because
the sigs were explicitly documented in `kami_context/system-ids.md` — I
treated the docs as authoritative.

Is that policy acceptable for future sessions? Specifically: if a
selector is missing from the JSON ABI but has a documented signature in
`system-ids.md`, can I add it to the overlay without asking? If the
signature is **only** derivable from inspecting calldata with no doc
backing, I'll continue to flag for you.

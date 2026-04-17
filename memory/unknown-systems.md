## Resolved

### 2026-04-17 — system.craft `0x5c817c70` → `executeTyped(uint32,uint256)`

Confirmed against deployed `CraftSystem` bytecode at
`0xd5dDd9102900cbF6277e16D3eECa9686F2531951`: contains `0x5c817c70` and
**not** the documented 3-arg `0xa693008c`. Vendored `CraftSystem.json` is
stale. Overlay entry added; field map maps `index → metadata.recipe_index`
and `amt → amount`.

Samples (now decoding cleanly):
- `0x62a99c841e20b14e3f033bebc4fbd894fd9bcf4302f367057133b38fb88804bf`
- `0x67fb42b712484fba885cd2d266194bf97554af2dc05a19f45be1e0144444d40c`
- `0xc21a0120f6bf5ad24de5e177dc6eb9f45a931240cc29c1595963eebd7055cba2`
- `0x2b6d948643a1e1caf0434b214a66f590fb1bafdd3c6e91b60c7d2130793a8c24`
- `0x0838fca9ff035469453a7a422c7b358a66f88237e00751e43c795659bfa62345`
- `0x4fb432a389fc338cb3b7e1c7701d3d02999e66fcab75b6179d5ef8d5363c8aa7`

## Open

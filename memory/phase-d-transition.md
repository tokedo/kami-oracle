# Phase D transition — public query plane

**Date**: 2026-04-24 (session 5)
**Authorized by**: founder, via session 5 prompt in
blocklife-ai/context/kami-oracle-bootstrap/session-5-prompt.md

## Context
Stage 1 completed with 81.6% harvest coverage (session 3.5
acceptance gate). Founder wants to run ad-hoc SQL from Colab and
wire `kami-zero` (on VM `kami-agent`) to the oracle for strategy
queries. The pre-baked REST endpoints plus SSH tunnel are too
clunky for both use cases.

## Decision
Transition to Phase D ahead of the original plan. Expose
`136-112-224-147.sslip.io` publicly on HTTPS:443 via Caddy reverse
proxy, with:
- Bearer-token auth on all non-/health routes
- Per-token rate limit (60 req/min default)
- /sql endpoint: SELECT-only, 10s timeout, 10k row cap
- /backup endpoint: loopback-only, EXPORT DATABASE under serve's
  own DB lock, dest_dir restricted to <repo>/db
- Nightly backup to gs://kami-oracle-backups/ (cron 04:15 UTC,
  14-day retention)
- Systemd-managed with auto-restart

## Threat model
Data is public on-chain — confidentiality not at risk. Real risks:
- DoS via expensive queries → mitigated by /sql governance (SELECT
  only, 10s timeout, 10k row cap)
- DoS via request rate → mitigated by per-token rate limit
- Lateral movement via compromise → service is read-only; no
  signer, no private keys on this VM
- Token leak → rotate via regenerating KAMI_ORACLE_API_TOKEN +
  systemctl restart kami-oracle, then redistribute to kami-zero
  and Colab secrets

## Not yet
- MCP server (still post-decision, after kami-agent validates /sql)
- Multi-user auth (single shared bearer token is fine for now)
- WAF / geo-blocking (not warranted at this scale)

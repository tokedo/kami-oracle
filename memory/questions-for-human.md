# Questions for human

## Open

### Session 5 — VM service-account scope blocks GCS uploads

The bucket `gs://kami-oracle-backups` exists in project `kami-agent-prod`
and the nightly backup endpoint, script, and cron entry are all wired
up. Manual run on 2026-04-24 ran the EXPORT cleanly (67MB tarball),
but the `gcloud storage cp` step fails with:

```
HTTP 403 — Provided scope(s) are not authorized
```

The VM `kami-oracle` runs as service account
`820986853453-compute@developer.gserviceaccount.com` with scope
`https://www.googleapis.com/auth/devstorage.read_only`. To upload
backups the VM needs `devstorage.read_write` (or `cloud-platform`).

Fix is one of:

```bash
# Option A — widen scopes (requires VM stop)
gcloud compute instances stop kami-oracle --zone=<zone> --project=kami-agent-prod
gcloud compute instances set-service-account kami-oracle \
    --zone=<zone> --project=kami-agent-prod \
    --service-account=820986853453-compute@developer.gserviceaccount.com \
    --scopes=https://www.googleapis.com/auth/devstorage.read_write,\
https://www.googleapis.com/auth/logging.write,\
https://www.googleapis.com/auth/monitoring.write
gcloud compute instances start kami-oracle --zone=<zone> --project=kami-agent-prod

# Option B — give the SA a key file mounted at /etc/oracle/sa.json,
# and `gcloud auth activate-service-account` from the script. Less
# preferred (long-lived key on disk).
```

Confirm afterwards by running:
```bash
/home/anatolyzaytsev/kami-oracle/scripts/backup-db.sh
gcloud storage ls gs://kami-oracle-backups/ --project=kami-agent-prod
```

Until then the cron job at `15 4 * * *` UTC will run the export, build
a tarball, fail at upload, and log to `logs/backup.log`. The local
EXPORT path still works — useful for crash recovery — but no
off-machine durability yet.

## Resolved

Session 1's three questions were addressed in Session 2's resume brief
(craft sig, overlay policy, bpeon operator wallet). One Session 2
finding is non-blocking and lives in `memory/next-steps.md` under
"Session 2 finding — RPC retention limit".

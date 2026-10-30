# First-release data operations runbook

These commands operate on one PostgreSQL database and its initial `platform` schema. They do not call external services. There is no physical account-deletion command: transcript deletion redacts personal transcript/chat-test content while account identity, wallet/ledger, sessions and audit evidence remain. Never delete wallet entries or audit records to satisfy a privacy request.

## Owners and role separation

- **Platform on-call:** execute approved exports and scheduled retention batches; preserve command results with the change record.
- **Privacy/security approver:** verify export identity/scope and approve any transcript-bearing export.
- **Database owner:** provision restricted roles, protect backup storage, prepare fresh restore databases, and validate restore drills.
- **Incident commander:** coordinate production recovery and record the decision and target.

No deployment roster is defined in this repository. Map these roles to named groups and alert routes before production use. Obtain database URLs from the deployment secret manager; do not put credentials in shell history, process arguments, tickets or this runbook, and do not enable shell tracing.

| Environment | Required connection |
|---|---|
| `PLATFORM_RETENTION_DATABASE_URL` | Different role, same exact host/port/database as `PLATFORM_DATABASE_URL`. Grant only scoped SELECT/UPDATE on transcript text, chat-test text, transcript state, and INSERT to `audit_logs`. No `DELETE`, schema ownership, or ledger/audit mutation. |
| `PLATFORM_BACKUP_DATABASE_URL` | Different, read-only role on the same exact host/port/database as `PLATFORM_DATABASE_URL`. |
| `PLATFORM_RESTORE_DATABASE_URL` | Different role on a separate, empty target database; schema/table creation and data writes only on that target. |

The CLI validates PostgreSQL+asyncpg URLs, distinct roles, and target relationships. Review inherited role memberships too; direct grants are not the whole privilege set.

## User data export

Confirm the requester and UUID through the approved support workflow. Use a non-sensitive ticket/change reference as the required 10–500 character `--reason`. Choose an absolute path in an existing protected export directory. The output is mode `0600`; existing files are never overwritten.

```sh
python -m platform_api.cli export-user \
  --user-id '<user-uuid>' \
  --output '/secure-exports/<ticket>-<user-uuid>.json' \
  --reason 'approved export ticket <ticket>'
```

Transcript/chat text is redacted by default. `--include-transcripts` requires privacy/security approval; the artifact remains highly sensitive, although known secret fields and recognizable secret strings are still redacted. Do not send it through email/chat or store it in ordinary support attachments. Check the returned user UUID, counts, size and elapsed time; verify `ops.user.exported` in `platform.audit_logs`; deliver only through the approved channel and expire the copy under the deployment's export-copy policy.

The export includes the user's profile, account/session, agent/testing, public deployment metadata, wallet/billing, history and related audit data. Public deployment IDs, origin, disclosure version, enabled state, and created/disabled timestamps are included; guest bearer capabilities and capability digests are excluded. Credential/MFA values are redacted. Wallet entries and audit evidence are preserved and included. Limits come from `PLATFORM_OPS_EXPORT_MAX_BYTES` and `PLATFORM_OPS_EXPORT_MAX_RECORDS`; optional CLI limits can only lower them.

## Transcript deletion and retention

Run the per-user command repeatedly until all three reported counts reach zero. Each bounded pass is audited, including a zero-count pass:

```sh
python -m platform_api.cli delete-user-transcripts \
  --user-id '<user-uuid>' \
  --reason 'approved transcript deletion ticket <ticket>'
```

For policy retention, use an approved timezone-aware UTC cutoff and repeat until the reported counts are zero:

```sh
python -m platform_api.cli apply-retention \
  --before '2026-01-01T00:00:00Z' \
  --reason 'scheduled transcript retention policy 2026-01'
```

Both operations replace transcript segments, chat-test messages and quarantined event bodies with `[REDACTED]`; transcript state revisions advance when segments change. They do not delete ledger, session or audit rows. The configured default batch is `PLATFORM_OPS_RETENTION_BATCH_SIZE`; `--batch-size` can lower, never exceed, the configured cap (hard service limit: 1,000). Check the per-table counts and matching audit action (`ops.user.transcripts_redacted` or `ops.retention.applied`) in the change record. A cutoff is a redaction policy, not a hard-delete job.

## Backup

Verify schema health, free space and protected encrypted-at-rest backup storage. The ZIP contains exact PostgreSQL binary-COPY table rows, including transcripts and secrets; the archive is **not encrypted by this command**. Never put it in a home directory or ordinary ticket attachment.

```sh
python -m platform_api.cli backup \
  --output '/secure-backups/platform-<change-id>.zip' \
  --reason 'scheduled database backup <change-id>'
```

The backup is a repeatable-read snapshot. Check returned size, table counts, elapsed seconds and the source `ops.backup.created` audit entry. File mode is `0600`; choose a fresh absolute output path. `PLATFORM_OPS_BACKUP_MAX_BYTES` is the cap; `--max-bytes` may lower but not raise it. Keep the application build matching the archive schema/version/hash.

## Restore drill or recovery

Restore is only into a separate empty target database. The command initializes the current schema, verifies all platform tables are empty, checks archive schema/version/hash/contents and restores in a transaction; it never merges or drops a database. Restore audit `ops.backup.restored` commits with restored rows.

1. Obtain incident/change approval. Have the database owner prepare a **new disposable target database**, and provide its URL only through `PLATFORM_RESTORE_DATABASE_URL`. Confirm it is not the app database. Never route live traffic to a drill target.
2. Use the matching application version and a protected archive path.
3. Run:

   ```sh
   python -m platform_api.cli restore \
     --backup '/secure-backups/platform-<change-id>.zip' \
     --reason 'approved restore drill <change-id>'
   ```
4. Check schema and table counts; verify wallet/ledger, prior audit rows and the new restore audit event. Keep the result with the drill record.
5. The database owner retires the isolated target through the approved disposable-database lifecycle. Never reuse a nonempty restore target.

`PLATFORM_OPS_RESTORE_MAX_BYTES` bounds input size; the command rejects unexpected ZIP members, version/schema mismatch, over-limit archives, row-count mismatch and nonempty targets. A failed restore target is unsuitable for traffic; retire it through the approved disposable-target process and begin again with a fresh database.

## Failures

Commands return nonzero and report only the exception class, not URLs or exception text. Do not retry with broader privileges or raise configured limits to bypass a failure. Preserve the change reference and ask the database owner to check role grants and schema state; involve privacy/security when scope or content is in doubt. Check the protected output directory before retrying. A failed export/backup removes its newly created artifact when possible.

## Staging release qualification

Run release qualification against the exact candidate commit in an isolated
staging environment. Keep public-call and real-paid flags disabled throughout
the drill. Do not use production database credentials, customer accounts, or
private recordings.

Before the run, have the release owner review an absolute-path JSON profile with
schema version `1` and profile `deployment`. It must pin exact HTTPS origins;
LLM, STT, English TTS, and Hindi TTS IDs, revisions, and SHA-256 hashes; runner
and API endpoints; the required TURN relay endpoint and a secret-manager
credential reference (never a credential value); Chromium version; PostgreSQL
server version and image digest; measured concurrency; timeout, latency, and
quality limits; and baseline artifact IDs. Missing models, TURN, database,
browser, audio fixtures, or reports block qualification.

Provision the pre-reviewed English and Hindi speech fixtures under
`VERIFY_AUDIO_ROOT`, with a checked `audio_manifest.json`. Use the staging
model/adapter tuple and matching deployment-specific baselines. The verifier
must exercise PCM input through STT and a completed pinned-flow turn; send-text,
synthetic, or silent media is not speech evidence. Keep raw recordings and
provider/TURN credentials out of reports.

The deployment lane must also report a real Chromium TLS/WebRTC session using
the configured TURN relay, with non-silent media in both directions, a completed
turn, barge-in cancellation, and audio recovery after reconnect. Keep
`audio-evidence.json` and `transport-evidence.json` with the owned run artifacts;
hand-authored or synthetic reports do not count.

From the candidate checkout, run:

```sh
scripts/voice-ai.sh verify release \
  --profile deployment \
  --config '/secure-qualification/release-profile.json'
```

Keep the owned `data/verification/<run-id>/` artifacts with the change record.
Review both `manifest.json` and `result.json`: every required gate must be
`passed`, each gate must have a status and reason, reports must be present and
nonempty, and the manifest commit must be the same release commit being
staged. Confirm the database report includes a fresh-database backup/restore,
wallet-ledger reconciliation, and preserved audit evidence. A failed or
`not_run` gate blocks the release; do not relabel a required capability as
optional to clear it.

Only after the reviewed manifest records all required gates passed for the
same release commit may the release owner separately approve public-call
enablement. Public and real-paid flags remain off until that decision; payment
gateway enablement remains independently disabled until its own verification
is complete.
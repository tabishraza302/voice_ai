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

Run the release gate from the exact candidate commit in an isolated staging
environment. Keep public calls, real-paid calls, and gateway access disabled
throughout the run. Do not use production database credentials, customer
accounts, or private recordings.

The current verifier fails closed unless it can run the real adapter
qualification scenarios and obtain current-run PCM-to-STT-to-completed-flow
evidence from recorded human speech. The repository's smoke and batch
benchmark tools are diagnostics, not proof of a completed assistant turn. A
hand-authored report, a prior run's report, a silent mic track, or text sent
instead of speech cannot satisfy this prerequisite.

### Prepare reviewed inputs

1. Commit the candidate and make sure the checkout is clean:

   ```sh
   git status --porcelain
   git rev-parse HEAD
   ```

   Both `local_offline` and `deployment` profiles must set
   `qualification.source_revision` to that full commit hash.

2. Review two absolute-path JSON profiles. Both use schema version `1`, pin
   LLM/STT/English TTS/Hindi TTS IDs, revisions, and SHA-256 hashes, and carry
   the same qualification tuple: loopback model and transport endpoints,
   adapter version, exact LLM/STT/TTS agent settings, locales `["en", "hi"]`,
   and `source_revision`. Each sets a positive concurrency and measured
   `turn_timeout_ms`, `latency_p95_ms`, and `quality_threshold`.

   The deployment profile additionally pins exact HTTPS origins, HTTPS runner,
   API, and flags endpoints; a required TURN relay and secret-manager
   credential reference (never the credential itself); Chromium version;
   PostgreSQL server version and image digest; and baseline artifact IDs.
   Neither profile may contain credentials or endpoint query strings.

3. Provision the reviewed English and Hindi human-speech fixtures outside the
   repository under `VERIFY_AUDIO_ROOT`. Its `audio_manifest.json` must identify
   each PCM fixture, transcript, source, license, and SHA-256. The same fixture
   files must be available offline throughout the run. Do not download
   fixtures during qualification or substitute generated audio.

4. Arrange a current-run audio qualification producer that records the actual
   PCM input, STT result, completed pinned-flow turn, and assistant audio for
   both locales. Arrange actual adapter scenario evidence bound to the
   configured profile and source revision. If either producer is unavailable
   or returns `NOT_RUN`, `UNSUPPORTED`, or `FAILED`, the gate must fail; do not
   hand-create or edit its JSON to make the report pass.

5. Configure the staging flags endpoint to return HTTP 200 JSON with a `flags`
   object containing all of the following values, each explicitly `false`:

   ```json
   {
     "flags": {
       "PLATFORM_ENABLE_PUBLIC_CALLS": false,
       "PLATFORM_ENABLE_REAL_PAID_CALLS": false,
       "PLATFORM_ENABLE_GATEWAY": false
     }
   }
   ```

   The configured runner and API endpoints must also return HTTP 200 JSON with
   `status` equal to `ok` or `ready`. Redirects, missing endpoints, missing
   flags, and unknown/non-boolean flag values block the run.

### Run and inspect

From the clean candidate checkout, export the reviewed fixture root and run
both profiles plus the required Chromium journeys:

```sh
export VERIFY_AUDIO_ROOT='/secure-qualification/audio'
scripts/voice-ai.sh verify release \
  --profile deployment \
  --config '/secure-qualification/deployment-profile.json' \
  --local-config '/secure-qualification/local-offline-profile.json'
```

The release gate runs the development split-port auth journey and the TLS
production-like ingress journey in Chromium, then actual model/adapter
qualification for each profile. The verifier writes results only inside its
new `data/verification/<run-id>/` directory. Each verifier HMAC envelope binds
the evidence payload to that run ID, profile hash, and exact commit with a
temporary key that is never written to disk. The verifier HMAC is envelope
integrity only; it does not authenticate a payload's origin. Do not prepopulate,
copy, rename, or edit evidence files.

Review `manifest.json` and `result.json`. Every required gate must be
`passed`, and every gate must have a status and reason. Confirm the manifest
commit is the candidate commit, `playwright_dev.json` and
`playwright_tls.json` are raw reports from the two browser journeys, and
`deployment-preflight.json` records live endpoint and disabled-flag checks.
Profile-specific `adapter-qualification-<profile>.json` reports must contain
all seven actual scenario results and an authenticated live execution ledger:
a runner-minted execution UUID, per-scenario runtime traces, server event and
transaction/ledger IDs, a receipt hash chain, and a valid runner HMAC bound to
the current run, profile, commit, and capability report. Runner names,
run IDs, profile hashes, report hashes, and scenario labels alone are not
execution proof. Profile-specific `audio-evidence-<profile>.json` files must
come from the current-run PCM-to-STT-to-completed-flow producer and contain
measurements recomputed from actual completed turns; the verifier HMAC alone
cannot make editable metrics live evidence.
Audio measurements are derived from the current-run observed turns, not values
asserted by a report: measured concurrency must equal the profile setting,
each completed turn must be within `turn_timeout_ms`, measured p95 latency
must be within `latency_p95_ms`, and English and Hindi transcript quality
scores must meet `quality_threshold`. Do not retain raw recordings or
provider/TURN credentials in the run directory.

The verifier blocks on a missing fixture, live audio producer, endpoint,
disabled flag, measurement, or qualified adapter scenario and reports the
exact reason. The current repository has no PCM-to-STT-to-completed-flow
producer and no adapter-owned execution UUID, runtime trace receipts,
server-owned event/transaction ledger chain, or runner HMAC. The platform also
does not expose the required flags endpoint. Those producer, runner/backend
receipt-chain, and staging endpoint prerequisites must be implemented and
supplied before this gate can pass. A failed or `not_run` gate is not a
release approval.

Only after the reviewed manifest records all required gates passed for the
same commit may the release owner separately approve public-call enablement.
Public and real-paid flags remain off until that decision; gateway enablement
remains independently disabled until its own verification is complete.
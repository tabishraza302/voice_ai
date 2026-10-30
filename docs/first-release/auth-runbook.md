# First-Release Platform Authentication Runbook

This runbook documents exact operational procedures, CLI commands, cryptographic key generation, admin bootstrap, MFA recovery, mail delivery, and incident revocation for the Voice AI SaaS platform control plane (`platform_api`).

---

## 1. Cryptographic Secrets Setup

All secrets must be distinct 32-byte cryptographic random values. Never commit secrets, reuse keys across purposes, or use hardcoded values in staging/production.

### 1.1 Generating Required Keys

Run the following commands in an isolated terminal to generate production keys:

```bash
# PLATFORM_AUTH_CSRF_KEY (32-byte base64url secret for HMAC CSRF tokens)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# PLATFORM_AUTH_RATE_KEY (32-byte base64url secret for rate-limiting HMACs)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# PLATFORM_AUTH_MFA_KEY (32-byte Fernet key for encrypting TOTP secrets and mail payloads)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode('ascii'))"
```

Store these keys in your secure deployment secret manager (e.g. AWS Secrets Manager, HashiCorp Vault).

---

## 2. Shared Database and Platform Schema Initialization

The platform shares the single PostgreSQL runtime database selected by the explicitly exported `DATABASE_URL` with the local agent and Phoenix. Export that variable in the shell/process environment before running platform commands; the platform must not silently load `.env`, supply a default, open a second database or fall back to SQLite. The agent owns `public`, the platform owns `platform` (override with `PLATFORM_POSTGRES_SCHEMA`), and Phoenix owns `phoenix`. The host launcher derives Phoenix's PostgreSQL URL from `DATABASE_URL` and selects the `phoenix` schema; do not set a separate Phoenix database URL. `init-db` (also accepted as `init-schema`) creates only the platform schema; it does not touch `public` or `phoenix` and performs no implicit migration.

### 2.1 Initialize Schema

Run the schema initialization command against the shared database:

```bash
uv run python -m platform_api.cli init-db
```

This verifies that the `platform` schema is absent or empty, acquires an exclusive advisory lock, and atomically applies the platform metadata, `schema_info` record (version `platform-v1`), and explicitly configured first tariff. For a fresh schema, set `PLATFORM_INITIAL_SESSION_RATE_MINOR_PER_MINUTE`, `PLATFORM_INITIAL_BILLING_SLICE_MS`, and `PLATFORM_INITIAL_BILLING_PERMIT_TTL_MS` together; `init-db` never invents a rate or billing policy. A nonempty unknown `platform` schema or a mismatched recorded schema fails with `SCHEMA_MISMATCH`; the command never repairs or converts local or Phoenix tables.

### 2.2 Verify Schema Readiness

Verify that the `platform` schema metadata matches the expected platform specification:

```bash
uv run python -m platform_api.cli check-schema
```

### 2.3 Schema Boundaries

- `public`: local agent tables; not created or migrated by `platform_api`.
- `platform`: platform control-plane tables and `schema_info`; created only by explicit `init-db` (also accepted as `init-schema`).
- `phoenix`: Phoenix telemetry tables; owned by Phoenix and never read or written by `platform_api`. The host launcher derives the connection URL from `DATABASE_URL` and sets `PHOENIX_SQL_DATABASE_SCHEMA=phoenix`; never configure a second database URL.
- Backups and restores cover the whole database, so all three schemas move together; restore tests must not split them apart.

---

## 3. Administrator Bootstrap

Before the platform ingress can report readiness (`/health/ready` returns 200), at least one active administrator with enrolled MFA must exist.

### 3.1 First Administrator Creation

Run the bootstrap command. The operator's password is read interactively with `getpass`:

```bash
uv run python -m platform_api.cli bootstrap-admin \
  --email "admin@example.com" \
  --display-name "System Administrator" \
  --reason "Initial SaaS deployment operator bootstrap"
```

- Acquires PostgreSQL advisory lock `PLATFORM_ADMIN_ADVISORY_LOCK_ID`.
- Verifies that zero administrators exist and the email is unused.
- Prompts for password twice with interactive masking.
- Generates bounded scrypt hash and active `User` record.
- Emits: `{"user_id": "<uuid>", "result": "bootstrapped"}`.

### 3.2 MFA Enrollment

1. Operator navigates to the login page (`http://127.0.0.1:3001/login` or public ingress).
2. Submits email and password. The system responds with session state `mfa_enrollment_required`.
3. Operator enters password on enrollment screen to generate TOTP secret and provisioning URI (`otpauth://`).
4. Operator imports the secret into an offline authenticator (e.g. 1Password, Google Authenticator).
5. Operator confirms enrollment with a fresh 6-digit TOTP code.
6. The session rotates to `authenticated`, and readiness checks begin passing.

### 3.3 Adding Additional Administrators

Subsequent administrators must be created via the operator CLI, not public signup:

```bash
uv run python -m platform_api.cli create-admin \
  --email "security-lead@example.com" \
  --display-name "Security Lead" \
  --reason "Authorizing secondary operator for on-call rotation"
```

The new administrator then signs in and completes TOTP enrollment.

---

## 4. Admin MFA Recovery Procedure

If an administrator loses access to their authenticator device:

```bash
uv run python -m platform_api.cli recover-admin-mfa \
  --user-id "<administrator-uuid>" \
  --reason "Verified operator ticket #SEC-1049 authenticator loss"
```

- Clears the registered TOTP factor under advisory lock.
- Increments the user's `auth_version`, invalidating all existing sessions across all tabs and devices.
- Requires the administrator to log in with their password and complete a fresh TOTP enrollment.
- Emits: `{"user_id": "<uuid>", "result": "mfa_recovered"}`.

---

## 5. Transactional Mail Dispatch

Transactional emails (signup verification, password reset) are stored in the durable `auth_mail` queue.

### 5.1 Local Development (File Mode)

In development (`PLATFORM_AUTH_MAIL_MODE=file`), emails are written to mode-0600 JSON files under `data/platform-auth-mail/` (directory mode 0700).

To trigger a delivery batch:

```bash
uv run python -m platform_api.cli deliver-auth-mail --once --limit 25
```

### 5.2 Production Scheduler (SMTP Mode)

In production (`PLATFORM_AUTH_MAIL_MODE=smtp`), schedule `deliver-auth-mail` periodically (e.g., via cron or Kubernetes CronJob every 30 seconds):

```bash
python -m platform_api.cli deliver-auth-mail --once --limit 50
```

---

## 6. Maintenance & Pruning

Periodically prune expired rate-limit window counters, expired tokens, and revoked anonymous sessions:

```bash
uv run python -m platform_api.cli prune-auth \
  --anonymous-ttl 600 \
  --rate-limit-ttl 3600 \
  --tokens-ttl 86400
```

---

## 7. Incident Revocation & Suspension

### 7.1 Emergency User Suspension

To immediately revoke all access for a compromised account:

1. Privileged admin calls `POST /api/v1/admin/users/{user_id}/suspend` with step-up verification.
2. The user's status is set to `suspended`, `auth_version` is incremented, and all active sessions are revoked immediately.
3. Subsequent requests by that user return `401 UNAUTHENTICATED` or `403 ACCOUNT_SUSPENDED`.

### 7.2 Global Session Revocation

To revoke all sessions for an individual user from the database in an emergency:

```sql
UPDATE platform.users SET auth_version = auth_version + 1, updated_at = NOW() WHERE id = '<user_uuid>';
UPDATE platform.auth_sessions SET revoked_at = NOW() WHERE user_id = '<user_uuid>' AND revoked_at IS NULL;
```

---

## 8. Password Hashing Benchmark & Capacity Sizing

### 8.1 Sizing Principles & Strict Remeasurement Requirement

Password hashing in `platform_api` uses bounded scrypt (`N=131072`, `r=8`, `p=1`, `maxmem=256MB`, 16-byte salt, 32-byte digest) off the event loop via `platform_api.auth.passwords.PasswordHasher`.

The 128 MiB scrypt work factor represents a theoretical minimum working buffer per active hash operation (`128 * r * N` bytes), not the full deployed process peak. Process memory also includes the Python runtime, interpreter heap, thread stacks, OpenSSL allocations, database driver connections, and ASGI web framework overhead. To prevent CPU starvation and memory exhaustion from concurrent authentication attempts, thread pool concurrency is strictly constrained by `PLATFORM_AUTH_HASH_CONCURRENCY` (default: 2, bounded by semaphore with immediate 429 rejection on saturated capacity).

**Production Remeasurement Requirement:**
Hardware architecture, CPU clock speeds, hypervisor scheduling, container cgroup memory limits, and OpenSSL library implementations differ substantially between local development workstations and production target environments.

**Local baseline figures must NEVER be conflated with or assumed valid for staging or production deployments.**
Before deploying or modifying target machine sizing, cluster pod limits, or concurrency limits in staging/production, operators **MUST** rederive actual latency distributions and peak memory consumption directly on the intended deployment target under intended target concurrency.

### 8.2 Safe Local Benchmark Command

The repository provides a self-contained benchmark tool (`scripts/bench_auth_hash.py`) that exercises the production `PasswordHasher` under the exact configured concurrency without any external network calls or persistent secret storage. All passwords used in the benchmark are synthetic, disposable in-memory test vectors.

Run the benchmark locally using `uv`:

```bash
# Run with production default concurrency (concurrency=2, 10 iterations)
uv run python scripts/bench_auth_hash.py --concurrency 2 --iterations 10

# Output structured JSON to data/ (ignored by git)
uv run python scripts/bench_auth_hash.py --concurrency 2 --iterations 10 --json --output data/auth_hash_benchmark.json
```

#### Parameters:
- `--concurrency <N>`: Thread concurrency pool limit (must match target `PLATFORM_AUTH_HASH_CONCURRENCY`, default: `2`).
- `--iterations <N>`: Total password hashing operations to execute (default: `10`).
- `--warmup <N>`: Warmup hashing operations executed before taking measurements (default: `1`).
- `--json`: Output raw structured JSON.
- `--output <path>`: Write structured JSON report to file (e.g. `data/auth_hash_benchmark.json`).

### 8.3 Output Interpretation & Capacity Calculation

The benchmark tool outputs:
1. **Metadata**: Host OS, CPU architecture, core count, Python version, scrypt parameters (`N=131072, r=8, p=1`).
2. **Workload**: Configured concurrency, total hash iterations, total elapsed wall time, and throughput (hashes/second).
3. **Latency**: p50 (median) and p95 latencies in milliseconds.
4. **Memory**: Peak resident set size (RSS in MB and GB) of the process.

#### Deriving Deployment Sizing:
- **Process Memory Budget**: `Base Runtime Memory + Measured Peak under Representative HTTP & Concurrency + Deployment Safety Margin`. The 128 MiB per-hash factor is a theoretical algorithmic floor, not a substitute for measuring deployed peak process RSS under intended worker concurrency. Operators must evaluate actual resident memory under representative traffic before setting container cgroup limits or pod resource requests.
- **Throughput & Capacity Limit**: With concurrency $C$ and measured p50 latency $L_{ms}$, maximum sustained authentication throughput per API worker instance is approximately:
  $$\text{Max Throughput} = \frac{C \times 1000}{L_{ms}} \text{ hashes/second}$$
  Any burst exceeding $C$ concurrent requests is immediately rejected with `429 RateLimitedError` (`Retry-After: 1`), protecting the host event loop from denial of service.

### 8.4 Production Deployment Verification Checklist

When validating a new production environment:
1. Provision the target container / VM with its planned CPU and memory limits.
2. Run `scripts/bench_auth_hash.py --concurrency <TARGET_CONCURRENCY> --iterations 20 --json --output /tmp/auth_measure.json`.
3. Verify that observed peak memory under representative HTTP load and concurrency remains safely below the container's cgroup memory limit (`memory.max` / Kubernetes memory limit) with an independently determined deployment safety margin.
4. Verify that measured p95 latency satisfies the independently selected deployment SLA on the target infrastructure.
5. Update operational dashboards and alert thresholds with the newly measured target-specific values.

### 8.5 Observed Local Measurements

The following figures reflect an actual measured local baseline run using `scripts/bench_auth_hash.py`. These metrics represent a **local standalone-process baseline only** and must **NOT** be treated as a production HTTP server budget or production sizing commitment.

- **Environment**: Mac M5 16 GB, macOS Darwin 27.0.0 (`arm64`), Python 3.12.13, 10 CPUs
- **Command**: `uv run --frozen python scripts/bench_auth_hash.py --concurrency 2 --iterations 10 --warmup 2 --json`
- **Algorithm Parameters**: scrypt `N=131072`, `r=8`, `p=1`, `maxmem=268435456` (256 MiB), 16-byte salt, 32-byte digest
- **Conditions & Workload**: Local standalone process; concurrency = 2; sample size = 10 hashes (preceded by 2 warmup iterations); total wall time = 0.786 s; throughput = 12.72 hashes/s
- **Latency**:
  - **p50 (Median)**: `153.84 ms`
  - **p95**: `163.91 ms`
- **Peak Process Memory**: `299.36 MiB` resident set size (`ru_maxrss`)

*Note on Production Interpretation:* Production HTTP deployments run within multi-worker application containers alongside ASGI web servers, database connection pools, middleware overhead, and host OS cgroups. Production capacity sizing must rederive latency distributions and memory ceilings directly under live HTTP deployment conditions.

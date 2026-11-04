# First-release identity and authentication implementation plan

> **For agentic workers:** Use `superpowers:executing-plans` to implement this contract task by task. This document defines work; it does not authorize starting services, changing dependencies, or executing the implementation during the present design task.

**Goal:** Give individual account owners and administrators a concrete, independently testable authentication boundary for the first SaaS release.

**Architecture:** Keep `web/` and the local `bot/` workflow. Create `platform_api/` as a separate FastAPI control plane with its own SQLAlchemy `Base` whose tables live in a dedicated `platform` schema of the PostgreSQL database selected by the shared `DATABASE_URL`, plus server-side opaque sessions. The local agent keeps `public` and Phoenix telemetry keeps `phoenix`; there is no separate platform database. The first release has two roles, `user` and `admin`, and one owner per account; no organizations, workspaces, impersonation, or role designer.

**Tech stack:** Existing Python 3.12+, FastAPI/Pydantic, SQLAlchemy 2 async sessions and asyncpg; Python `hashlib.scrypt`, `secrets`, and `hmac`; PyOTP for admin TOTP; cryptography Fernet for TOTP secrets; existing Next.js frontend.

**Spec:** This contract supplies the identity details for the first-release master specification. The task's correction that nothing has shipped supersedes references in earlier reviews to migration/backfill/cutover. Existing local records remain local records; there is no import or owner assignment exercise.

## Global constraints

- `DATABASE_URL` must be explicitly exported in the process environment as a `postgresql+asyncpg://` URL for the single PostgreSQL runtime database shared by the local agent (`public`), the platform control plane (`platform`), and Phoenix (`phoenix`). Platform settings and CLI commands must require that exported value; they must not silently load `.env`, supply a default/second URL or fall back to SQLite. The platform schema is selected by `PLATFORM_POSTGRES_SCHEMA` and defaults to `platform`. The host Phoenix launcher derives its connection URL from `DATABASE_URL` and sets the Phoenix-owned schema to `phoenix`; no independent Phoenix URL is configured. There is no `PLATFORM_DATABASE_URL`, no second runtime database, no SQLite fallback, no `bot.db` imports, no table reuse, no Alembic baseline or schema stamping, and no implicit or legacy data migration.
- `platform_api` default development bind is `127.0.0.1:8000`, configurable through `PLATFORM_HOST` and `PLATFORM_PORT`; the implementation must check host listeners and container-published ports before starting it. No new port is opened by this document.
- `local_offline` runs the existing local model workflow; it does not start `platform_api`, require a customer account, deliver mail, or contact an identity vendor. Its management surface must stay loopback-only. SaaS owner/admin resources always require authenticated server context.
- A small explicit allowlist of authentication bootstrap endpoints and liveness/readiness endpoints is necessarily usable without an authenticated owner. Public voice participants use the separate guest capability contract, never the owner session or a guest-selected `user_id`.
- Browser authentication is a cookie plus server-side session. Runner join credentials and internal service authentication are separate interfaces and must not accept this cookie as service authorization.
- Exact dependency pins; no model, prompt, metric, audio path, or measurement change is part of this identity slice.
- All auth response bodies and cookies use `Cache-Control: no-store`; auth pages use `Referrer-Policy: no-referrer`. Never log passwords, cookies, CSRF values, reset/verification tokens, TOTP secrets/codes, or complete auth request bodies.

## Source inventory and decisions grounded in it

Inspected on 2026-10-28; paths and symbols below describe the current repository, not a deployed system.

- `bot/api/router.py:create_api_router(settings)` includes agent, prompt, model, and voice routers without an authentication dependency. Its `/catalog` handler has none either.
- `bot/api/routes_agents.py:list_agents`, `create_agent`, `get_agent`, `update_agent`, `delete_agent`, and `activate_agent` depend on `bot.db.session.get_db_session`; there is no current principal or owner filter. `list_agents` seeds defaults on read.
- `bot/api/routes_prompts.py:get_base_prompt`, `update_base_prompt`, and `preview_prompt` also depend only on that DB session. Existing base-prompt writes are not protected by an admin role.
- `bot/bot.py:mount_api` mounts those routers at `/api`; its CORS allowlist is the localhost/127.0.0.1 web origins, with `allow_credentials=False`. CORS is not authentication. `bot/config.py:Settings.host` defaults to `localhost`, but configuration can change it; current routes do not themselves prove loopback enforcement.
- `bot/control.py:mount_control` exposes local settings controls including `POST /settings/stt`. These remain local-only; they are not a SaaS management shortcut.
- `bot/db/base.py:Base` and `bot/db/session.py:init_engine` own the local agent schema in `public`. The shared runtime is PostgreSQL-only; SQLite is not supported. The new platform must not import or call `bot.db`, because its metadata and engine belong to the separate `platform` schema.
- `bot/db/models/session.py:VoiceSession` / `SessionTurn` represent voice calls and conversation turns. They are not login sessions, user identities, or authorization records.
- `web/src/lib/api-client.ts:ApiClient.request` sends ordinary JSON fetches to `NEXT_PUBLIC_API_URL` (default `http://localhost:7860/api`). It has no cookie-credentials option, CSRF handling, auth methods, or typed auth errors. Keep it for the local path and add a distinct platform client.
- `web/src/app/` currently contains home, agents, base-prompt, and playground pages, with no account/auth/admin pages. `web/next.config.mjs` has no auth proxy. `web/package.json` uses Next.js 15.5.28, React 19.3.1 and Bun tests.
- Search of `bot/**/*.py` for login, password hashes, CSRF, session middleware, current-user dependencies, and cookies found no application auth implementation. Hits for realtime STT authentication and conversational `role` are unrelated.

Therefore implement identity freshly in `platform_api`; do not claim an existing auth component can be extended. No files need deleting to accomplish this boundary.

## Dependency decisions and primary sources

This is a selection for implementation, not an installation performed by the design task.

- Preserve existing direct pins `sqlalchemy==2.0.38`, `asyncpg==0.30.0`, `pydantic-settings==2.12.0`, and test pins `pytest==9.0.1`, `pytest-asyncio==1.3.0`.
- Promote already locked dependencies used directly by the new package to explicit direct pins: `fastapi==0.141.1`, `uvicorn==0.54.0`, `pydantic==2.14.1`, `email-validator==2.3.0`, and `cryptography==50.0.2`. Use the lock's `httpx==0.28.1` as an explicit dev dependency for API tests. These version observations come from `uv.lock`; do not upgrade the audio stack as a side effect.
- Add exactly `pyotp==2.9.0`. Its version exists in the [maintainer's PyPI release](https://pypi.org/project/PyOTP/2.9.0/). It supplies TOTP and provisioning URIs; no QR image package is needed because users can copy the setup key into an offline authenticator. PyOTP's [official documentation](https://pyauth.github.io/pyotp/) requires replay rejection and throttling in the surrounding application; `verify()` alone is insufficient.
- The [email-validator 2.3.0 release](https://pypi.org/project/email-validator/2.3.0/) supports normalization and optional deliverability checks. Call it with `check_deliverability=False` and `allow_smtputf8=False`; validation must never trigger DNS traffic. The first release accepts ASCII local parts and normalized IDNA domains.
- The [cryptography 50.0.2 release](https://pypi.org/project/cryptography/50.0.2/) exists and is already locked. Use its high-level [Fernet recipe](https://cryptography.io/en/latest/fernet/) to encrypt TOTP secrets with a separately injected key; do not invent encryption.
- Python's [scrypt API](https://docs.python.org/3.12/library/hashlib.html#hashlib.scrypt) removes the need for a password-hashing package. Its [secrets API](https://docs.python.org/3.12/library/secrets.html) supplies random salts/session/reset tokens. Use `hmac.compare_digest` for fixed-length hash/MAC comparison. Sessions are not JWTs, and this slice adds neither PyJWT nor an external identity SDK.
- `scrypt` parameters below are security-policy defaults, consistent with the [OWASP password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html), not measured throughput claims. Deployment qualification measures auth latency/memory independently of voice model benchmarks. TOTP follows [RFC 6238](https://www.rfc-editor.org/rfc/rfc6238).

## Identity, persistence, and validation

All identifiers are server-generated UUIDs, all stored instants are PostgreSQL `timestamptz`, all API instants are RFC 3339 UTC. Use constrained strings for the first schema. `python -m platform_api.cli init-schema` creates an absent or empty `platform` schema and records `schema_info(version='platform-v3', metadata_hash)` in the same transaction. It upgrades exact `platform-v1` and `platform-v2` schemas after verifying their historical metadata hash and object inventory, adding the missing `provider_secrets` and `provider_model_entries` tables as applicable. Other unknown schemas or schema drift are refused. Ordinary application startup checks the recorded version/hash and required tables, failing closed if either is missing or wrong; it never runs migrations. The initializer must never repair or migrate `public` or `phoenix`. Schema-separation validation rejects `public`, `phoenix`, `information_schema`, `pg_catalog`, any `pg_*` schema and non-identifier names for `PLATFORM_POSTGRES_SCHEMA`; `DATABASE_URL` is rejected when it is a SQLite URL or uses a non-asyncpg driver.

### `users`

- `id UUID PRIMARY KEY`.
- `email varchar(254) NOT NULL`, `email_key varchar(254) NOT NULL UNIQUE`. Trim surrounding email whitespace; normalize with email-validator, then lowercase the whole normalized ASCII address for the account-identity policy. Retain normalized display email separately. Do not collapse dots or `+suffix` aliases.
- `display_name varchar(80) NOT NULL`: trim, 1–80 Unicode codepoints, reject control characters. The same length rule applies after trimming.
- `password_hash text NOT NULL`: versioned encoding described below; never returned in a schema.
- `role varchar(8) NOT NULL CHECK IN ('user','admin')`; signup always creates `user`.
- `status varchar(12) NOT NULL CHECK IN ('active','suspended')`; signup creates `active` only after token confirmation.
- `email_verified_at timestamptz NOT NULL`; no usable unverified `users` row is required. Pending signup is a purpose-limited token record.
- `preferred_language varchar(2) NOT NULL DEFAULT 'en' CHECK IN ('en','hi')`, `timezone varchar(64) NOT NULL DEFAULT 'Asia/Kolkata'` validated with `zoneinfo.ZoneInfo`, and `low_balance_notifications boolean NOT NULL DEFAULT true`. These preferences authorize no notification network dependency by themselves.
- `auth_version integer NOT NULL DEFAULT 1 CHECK > 0`; `created_at`, `updated_at` required; `last_login_at` nullable.
- First-release email changes, self-service role changes, and physical account deletion are not API operations. Email is displayed read-only. Retention/export explanation belongs in account UI; immutable financial data is never cascaded away by auth.

### `auth_sessions`

- `id UUID PRIMARY KEY`, `token_digest bytea NOT NULL UNIQUE` containing SHA-256 of the decoded 32-byte opaque cookie secret, `user_id UUID NULL REFERENCES users(id)`, `auth_version integer NULL`.
- `state varchar(24)` constrained to `anonymous | mfa_required | mfa_enrollment_required | authenticated`. Only `authenticated` yields a `Principal`.
- `created_at`, `last_seen_at`, `idle_expires_at`, `absolute_expires_at` required; `revoked_at`, `password_verified_at`, `mfa_verified_at`, `step_up_at` nullable. Require a user and matching auth version for every non-anonymous state.
- Store a sanitized `user_agent_label varchar(160)` for device display and `created_ip inet` for restricted security audit; these are not authentication factors. No hard IP binding. Session IDs, IPs and user IDs never become Prometheus labels.
- Cookie secrets are generated by `secrets.token_bytes(32)`, encoded unpadded base64url (43 characters), stored only as hashes, and never returned in JSON. A malformed cookie is treated as unauthenticated, without DB exceptions.
- Anonymous session lifetime 10 minutes; password-verified MFA/enrollment lifetime 5 minutes, no sliding extension; authenticated lifetime 12 hours absolute and 30 minutes idle. All are `PLATFORM_AUTH_*` config values with these policy defaults. Authenticated activity renews the idle deadline up to absolute expiry, at most once per minute; perform renewal conditionally while checking revocation to avoid reviving an expired session.
- Rotate to a fresh row, secret, and CSRF token on login success, MFA completion, and password change. Lock and revalidate the preceding session before rotation, then revoke it in the same transaction so concurrent reuse cannot produce two upgrades. Cap authenticated sessions at 5 per user by revoking the oldest in the login transaction under a user-row lock. Expired session rows can be pruned; validity never depends on the pruning job running.
- Each request rechecks the user status, role and `auth_version` from PostgreSQL. No cached role in the cookie. PostgreSQL unavailable means `503 DEPENDENCY_UNAVAILABLE`, never local fallback or successful anonymous access.

### `auth_tokens`

- `id UUID PRIMARY KEY`, `purpose varchar(16) CHECK IN ('register','password_reset')`, `token_digest bytea UNIQUE NOT NULL`, `email_key varchar(254) NOT NULL`, `user_id UUID NULL REFERENCES users(id)`, `display_name varchar(80) NULL`, `created_at`, `expires_at`, `consumed_at NULL`.
- Tokens contain 32 random bytes encoded as 43-character unpadded base64url strings; store SHA-256 of the decoded bytes. Registration tokens expire in 24 hours; reset tokens in 30 minutes. Issuing a fresh token invalidates previous unconsumed tokens for that email/purpose in the same locked transaction.
- Confirmation consumes the token atomically with user creation or password update; two simultaneous submissions have exactly one success. Expired, wrong-purpose, consumed and unknown tokens share `400 TOKEN_INVALID`.
- Signup initiation stores only the pending email/display name and token. Password is chosen on the verification page after possession of the token is proven. This avoids an attacker choosing a password for someone else's unverified account. Verification creates the user and a normal account-owned wallet/profile initialization event atomically, using the master contract's idempotent account-initialization hook. It does not log the user in; user signs in explicitly.

### `auth_mfa`

- `user_id UUID PRIMARY KEY REFERENCES users(id)`, `secret_ciphertext text NULL`, `pending_secret_ciphertext text NULL`, `pending_expires_at timestamptz NULL`, `enabled_at timestamptz NULL`, `last_accepted_step bigint NULL`.
- Encrypt a payload containing both `user_id` and the 160-bit random TOTP secret using Fernet and verify the embedded binding on decrypt. `PLATFORM_AUTH_MFA_KEY` is a Fernet key injected separately from database backups and CSRF/rate-limit keys; missing/invalid keys fail startup in SaaS. Plaintext appears only in the explicit enrollment response's setup key/`otpauth` URI and in the user's authenticator; never store it in plaintext rows, HTTP URLs, logs, analytics, mail, or audit events.
- Use `pyotp.TOTP(secret, digits=6, interval=30)` with HMAC-SHA1, accepting current time step ±1 only. Under a row lock, find the matching step and require it to be greater than `last_accepted_step`, then update that field atomically. Same code/step cannot satisfy two concurrent login/step-up requests. Enrollment confirmation also consumes its matching step.
- TOTP is mandatory for all SaaS admin access in every SaaS profile, including development; tests can inject a fixed clock/secret through dependencies, never a production disable-MFA flag. Ordinary users use passwords in v1; no optional user OTP subsystem.
- No email OTP, SMS OTP, security questions, recovery-code inventory, or external identity provider. Lost admin authenticator recovery is a protected operator CLI procedure that revokes all sessions and requires reenrollment, with an operator reason and audit entry. It does not remove MFA requirements or issue an unrestricted session.

### Passwords and bounded work

- Accept 15–128 Unicode codepoints, UTF-8 encoded length at most 512 bytes; accept spaces and Unicode without trimming, normalization, silent truncation, or composition rules. Apply the same behavior in frontend, signup confirmation, login and reset. Reject malformed input before hashing; never reflect the input in errors.
- Hash with `hashlib.scrypt(password_utf8, salt=random_16_bytes, n=131072, r=8, p=1, dklen=32, maxmem=268435456)`. Store `scrypt$v1$131072$8$1$<salt-base64url>$<digest-base64url>`; parse only supported bounded parameter sets to avoid crafted DB data causing arbitrary allocations. No password pepper is required in v1.
- Perform hashes/verifications in a bounded thread executor, not on the event loop. Default maximum 2 simultaneous hashes per control-plane process; immediately reject overflow with `429 RATE_LIMITED` and `Retry-After: 1`. Deployment config may increase this only after measuring aggregate process memory and concurrent API latency. Startup checks OpenSSL scrypt availability; no weaker fallback.
- Unknown-email login verifies against a fixed dummy hash with the same parameters. Wrong-password, unknown-account, and suspended-account login all return `401 INVALID_CREDENTIALS` with identical response shape. Rate limiting precedes expensive hashing and is not a permanent account lock.

## Browser, cookie, origin, and CSRF contract

Production ingress serves web UI and `/api/v1` from one HTTPS origin. It routes `/api/v1` to `platform_api` directly; no Next.js session BFF. The browser's production platform base URL is `/api/v1`. Development uses `http://127.0.0.1:3001` UI and `http://127.0.0.1:8000/api/v1` control plane; document one hostname consistently. A deployment can override both origins explicitly.

- Production cookie: `__Host-platform_session=<opaque>; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=<remaining absolute TTL>`; no `Domain`. On logout, expire the identical cookie name/path/attributes. The DB remains the authority for idle/revoked/absolute validity.
- The only insecure cookie is `platform_dev_session` when `PLATFORM_ENV=development`, both origins are HTTP loopback, and both binds are loopback. Staging/production require HTTPS, secure cookies, trusted proxy CIDRs and exact configured public origin; startup rejects an insecure combination. Proxy-forwarded scheme/IP are trusted only from the configured proxy set.
- `PLATFORM_WEB_ORIGIN` is one exact scheme/host/port, without path or wildcard. For split-origin development only, CORS allows precisely that origin, `allow_credentials=True`, methods `GET,POST,PATCH,DELETE,OPTIONS`, and headers `Content-Type,X-CSRF-Token,Idempotency-Key,X-Request-ID`. Include `Vary: Origin`; never reflect arbitrary origins. Production same-origin needs no cross-origin allowance. Unknown Host headers are rejected before routing.
- Every unsafe browser method, including register, login, verification, reset, MFA and logout, requires `Origin` exactly equal to `PLATFORM_WEB_ORIGIN`, JSON content type, and `X-CSRF-Token` bound to the current cookie session. Missing/`null`/other Origin returns `403 ORIGIN_FORBIDDEN`; wrong/missing CSRF returns `403 CSRF_INVALID`; missing/wrong content type returns `415 UNSUPPORTED_MEDIA_TYPE`. OPTIONS preflights contain no secrets or mutation and do not require cookies.
- `GET /api/v1/auth/session` is the sole auth-bootstrap GET. If no valid session exists, create an anonymous session; return its current state and CSRF token. If Origin is supplied, it must match; reject `Sec-Fetch-Site: cross-site`. Same-origin navigation may omit Origin. Rate-limit session bootstrap creation to prevent unlimited DB growth. A browser cannot read this response cross-origin through an untrusted origin.
- CSRF token is unpadded base64url `HMAC-SHA256(PLATFORM_AUTH_CSRF_KEY, b'csrf-v1\0' + session.id.bytes + session.token_digest)`. Use a distinct random 32-byte deployment key, constant-time comparison, and regenerate naturally on session rotation. This lets reload recover a CSRF token without storing session secrets or plaintext CSRF values. Keep the token only in frontend memory; do not use localStorage/sessionStorage for credentials.
- `GET /auth/session` may expose the CSRF token only in its no-store response; never put it in links, cookies readable by scripts, logs, or query strings. Frontend uses `credentials: 'include'` for every platform call and sets `X-CSRF-Token` only for mutations. On rotation it replaces its in-memory token from the response. It may refresh session state after `CSRF_INVALID`, but never automatically replay a financial or other non-idempotent mutation.
- Set application CSP and same-origin frame protection in web ingress; secrets and one-time token pages must not load analytics or third-party resources. Auth errors do not redirect the browser to an arbitrary `next` URL. The UI accepts return paths only beginning with a single `/` and within the customer/admin app, rejecting `//`, schemes and backslashes.

## Registration, mail, reset, bootstrap, and step-up

Registration, resend and reset initiation always return `202 {"status":"accepted"}` for syntactically valid requests, whether the account exists or can receive a token. No raw token or account existence result is returned to the browser. A bounded, durable auth-mail record is written in the token transaction; a delivery failure does not roll back user state, expose the email, or make an already invalidated token usable.

`platform_api/auth/delivery.py` owns an explicit delivery interface. Development uses `file` delivery to a gitignored directory such as `data/platform-auth-mail/`: one mode-0600 message file per token, directory mode 0700, operator-local access only, never publicly served or printed in normal logs. Its plaintext token is a development fixture secret. Production requires authenticated SMTP with STARTTLS or TLS and certificate verification; host, credentials, sender, timeout and retry count are deployment config, not a mandatory identity vendor. SMTP is a declared SaaS network dependency. There is no network delivery adapter in `local_offline` because platform auth is not run there.

Mail links use the configured `PLATFORM_WEB_ORIGIN`, never a request Host or user-supplied URL. Put token in URL fragment (`/verify-email#token=...` or `/reset-password#token=...`); web reads it into memory and immediately removes the fragment with `history.replaceState` before rendering other app content. GET links never consume tokens: mail scanners must not verify or reset accounts. Both forms bootstrap CSRF and explicitly POST the token and chosen password. Retry delivery of the same token, not a new token on every retry. `auth_mail` has `id UUID`, `token_id UUID UNIQUE REFERENCES auth_tokens(id)`, `payload_ciphertext text NULL`, `state varchar(12) CHECK IN ('queued','sending','sent','expired')`, `attempts integer`, `available_at`, `lease_expires_at NULL`, `sent_at NULL`, `expires_at`, and `last_error_code varchar(40) NULL`. Claim with `FOR UPDATE SKIP LOCKED` and bounded lease; only the claiming dispatcher sends. Retain encrypted payloads only until delivered or their token expires; use the configured Fernet key, bind payload to token ID, and erase payload immediately on successful handoff. The DB token record itself retains only its hash. A dispatcher crash after SMTP handoff can duplicate the same link; token use remains single-use.

Password reset does not authenticate the user, alter role/status, clear MFA, or reactivate suspension. It sets a new hash, increments `auth_version`, revokes all sessions and all reset tokens, and appends the security audit in one transaction. Existing logged-in tabs become unauthorized on their next request. Password change requires current password and, for admin, fresh TOTP step-up; revoke every other session, rotate the current session with the new version, and return new session/CSRF state.

`python -m platform_api.cli bootstrap-admin --email <address> --display-name <name> --reason <reason>` reads the password using `getpass` twice; no password/token CLI argument, environment variable, or generated default password. It uses an exclusive PostgreSQL advisory lock and refuses unless there are zero admins and the email is unused. The trusted operator confirms the email's ownership out of band; the command records that assertion, creates an active verified admin and an append-only operator audit. It never seeds or logs credentials. Bootstrap and schema-init are commands, never startup side effects or HTTP endpoints.

The operator signs in through the restricted enrollment flow, copies the TOTP key into an offline authenticator, and confirms a fresh code before exposing production ingress. Readiness requires at least one active admin with enabled MFA; bootstrap deployment can reach liveness/auth enrollment while readiness remains false. Adding further admins uses an operator-only `create-admin` command with the same validation and audit, not a public role field. Recovery command `recover-admin-mfa --user-id <uuid> --reason <reason>` clears the old factor under lock, revokes sessions and pending secrets, increments auth version and requires fresh enrollment; it never disables the policy. Commands emit only user ID and result. Prevent suspension/removal of the last active enrolled admin with `409 LAST_ADMIN` under a shared advisory lock.

Admin successful password login issues only `mfa_required` or `mfa_enrollment_required` session state. It grants no resource access. TOTP success rotates it to `authenticated` and records `mfa_verified_at`/`step_up_at=now`. A privileged money/rate/suspension/limits/global-AI-config/terminate-call mutation requires `step_up_at` within 5 minutes; otherwise return `403 STEP_UP_REQUIRED`. `POST /auth/step-up` requires current password and unused TOTP, records a fresh time on the same session, and returns session state. Initial verified MFA counts as step-up. Role authorization, ownership/policy, CSRF and idempotency remain required after step-up.

All successful privileged changes and security events have append-only audit records containing actor kind (`user`/`operator`), actor ID, session ID where applicable, action, target type/ID, reason, request ID, UTC time, source IP, result and redacted change summary. Admin changes write audit and domain state in the same DB transaction; audit failure aborts the mutation. Use separate application/maintenance DB grants to prevent UPDATE/DELETE of audit rows. Never copy secrets or transcript bodies into audit.

## Exact API models and endpoints

All platform paths below are under `/api/v1`. Requests reject unknown fields (`extra='forbid'`). All bodies are JSON with an auth-route limit of 8 KiB, enforced before parsing. UUIDs, page cursors, limit fields and schemas are validated; errors never echo rejected password/token content. Successful JSON has the named shape directly, not an inconsistent `data` envelope. Empty operations return HTTP 204 with no JSON body.

Named wire models:

```text
UserView = { id: UUID, email: string, display_name: string,
  role: 'user'|'admin', status: 'active'|'suspended',
  email_verified_at: UTC, preferred_language: 'en'|'hi', timezone: string,
  low_balance_notifications: boolean, mfa_enabled: boolean,
  created_at: UTC, updated_at: UTC }
SessionResponse = { state: 'anonymous'|'mfa_required'|
  'mfa_enrollment_required'|'authenticated', user: UserView|null,
  session_id: UUID, expires_at: UTC, csrf_token: string,
  step_up_expires_at: UTC|null }
Accepted = { status: 'accepted' }
SessionItem = { id: UUID, current: boolean, user_agent_label: string,
  created_at: UTC, last_seen_at: UTC, expires_at: UTC }
SessionList = { items: SessionItem[] }
MfaSetup = { secret: string, provisioning_uri: string, expires_at: UTC }
UserPage = { items: UserView[], next_cursor: string|null }
ApiError = { error: { code: string, message: string,
  request_id: UUID, retryable: boolean, fields: [{ field: string, code: string }] } }
```

`SessionResponse.user` is null for anonymous state; otherwise it is the password-verified user. It is informational until `state='authenticated'`. Never include hashes, secrets, raw IPs, auth version, login cookie or provider credentials in these models. `MfaSetup` is the explicit one-time enrollment response exception for the setup secret and is no-store.

Endpoints, authorization and exact behavior:

1. `GET /auth/session` → `200 SessionResponse`; anonymous bootstrap or current session state. Invalid/expired/revoked cookie is replaced with an anonymous session; ordinary protected endpoints instead return `401 UNAUTHENTICATED`.
2. `POST /auth/register` accepts `{email, display_name}` → `202 Accepted`. Anonymous/current session plus Origin/CSRF; existing user does nothing and has the same result. Pending signup resend goes through this same endpoint, generating a fresh single-use registration token subject to the resend limit.
3. `POST /auth/verify-email` accepts `{token, password}` → `201 UserView` on first successful creation; no login cookie upgrade. Wrong/expired/consumed token → `400 TOKEN_INVALID`; unique email race with a separately completed registration also → `400 TOKEN_INVALID`, no second account.
4. `POST /auth/login` accepts `{email, password}` → `200 SessionResponse` with authenticated user or restricted admin MFA state and rotated cookie; wrong/unknown/suspended → `401 INVALID_CREDENTIALS`. If already authenticated, login may replace the current session after full credential verification; never preserve the old authenticated cookie on failed identity switching.
5. `POST /auth/logout` accepts `{}` → `204`, revokes the current anonymous/restricted/full session and expires its cookie. A second logout with a still-valid anonymous session succeeds; a missing/invalid session fails CSRF rather than silently authenticating anything.
6. `POST /auth/password/reset-request` accepts `{email}` → `202 Accepted`, including for suspended/unknown accounts. Existing active accounts receive a reset token; suspended accounts receive none. No login upgrade.
7. `POST /auth/password/reset-confirm` accepts `{token, password}` → `204`; atomic reset and revocation above; invalid token → `400 TOKEN_INVALID`.
8. `POST /auth/password/change` accepts `{current_password, new_password}` → `200 SessionResponse`; full active session, and fresh step-up if admin. Wrong current password → `401 INVALID_CREDENTIALS`; invalid new password → `422 VALIDATION`.
9. `GET /auth/sessions` → `200 SessionList`, own authenticated sessions only, newest first, maximum 5.
10. `DELETE /auth/sessions/{session_id}` → `204`; own session only; unknown/other-user ID → `404 NOT_FOUND`. Revoking current also expires cookie. Admin status does not permit deleting another user's session through this owner route.
11. `POST /auth/logout-all` accepts `{}` → `204`; full session; increments auth version, revokes all own sessions and clears cookie. No password required to make one's own sessions less privileged.
12. `POST /auth/mfa/enroll` accepts `{current_password}` → `201 MfaSetup`; only `mfa_enrollment_required` admin session, no already-enabled factor; wrong password → `401 INVALID_CREDENTIALS`, wrong state → `409 MFA_STATE_CONFLICT`. Generate/replace one pending encrypted secret with a 5-minute expiry. Provisioning URI uses fixed issuer `Voice AI` and server-owned account email.
13. `POST /auth/mfa/confirm` accepts `{code}` → `200 SessionResponse`; restricted enrollment session and unexpired pending secret, six ASCII digits only. Enable factor, record consumed step, rotate session to full admin in one transaction; invalid/expired pending/code → `400 MFA_INVALID`.
14. `POST /auth/mfa/verify` accepts `{code}` → `200 SessionResponse`; only `mfa_required` session and enabled factor; consume code step and rotate to full admin. Invalid/replayed code → `400 MFA_INVALID`; wrong state → `409 MFA_STATE_CONFLICT`.
15. `POST /auth/step-up` accepts `{password, code}` → `200 SessionResponse`; full admin session, enabled MFA, both credentials required, no global login scope. Wrong password/code/replay → `401 INVALID_CREDENTIALS`; stale step-up is not refreshed by GETs.
16. `GET /me` → `200 UserView`; full active session. `PATCH /me` accepts any nonempty subset of `{display_name, preferred_language, timezone, low_balance_notifications}` → `200 UserView`. Email/role/status/owner fields → `422 VALIDATION`.
17. `GET /admin/users` accepts `limit` default 25 (1–100), opaque bounded cursor (max 256 characters), optional `q` max 80 characters for normalized email/display-name search, optional `status=active|suspended` → `200 UserPage`, ordered stably by `(created_at,id)`. Cursor decoding is schema-checked; malformed → `422 VALIDATION`. Full admin + MFA required.
18. `GET /admin/users/{user_id}` → `200 UserView`; full admin + MFA; missing → `404 NOT_FOUND`. It is safe account metadata, not password, session secret, provider key or transcript content.
19. `POST /admin/users/{user_id}/suspend` accepts `{reason}` (trimmed 10–500 characters) → `200 UserView`; admin + MFA + fresh step-up; set suspended, bump auth version, revoke sessions and future admissions, and audit atomically. Existing-call ending is a separate action. Already suspended is an audited no-op returning 200; last-admin protection still applies.
20. `POST /admin/users/{user_id}/restore` accepts `{reason}` → `200 UserView`; same admin/step-up/audit checks. Sets active without restoring sessions/credentials or creating credit; already active is an audited no-op. No role update endpoint in v1.

Additional admin wallet, rate, limit, global prompt and runtime operations consume `require_admin` / `require_step_up` below and the domain-specific request, idempotency and audit contracts. No auth endpoint makes financial adjustments. Customer agent mutations cannot write a global platform prompt or bypass ownership through `user_id` in their body.

Error mapping shared by every new platform route:

- `400 TOKEN_INVALID`, `400 MFA_INVALID`: safe generic invalid/expired confirmation message.
- `401 UNAUTHENTICATED`: missing, expired, revoked or version-invalid full session; expire unusable cookie. `401 INVALID_CREDENTIALS` is reserved for credential verification; it must not identify which factor failed.
- `403 FORBIDDEN`: authenticated normal user on admin routes; `403 MFA_REQUIRED` for resource access by restricted MFA state; `403 ACCOUNT_SUSPENDED` if a suspension is observed during an otherwise valid session check; `403 STEP_UP_REQUIRED`, `403 CSRF_INVALID`, `403 ORIGIN_FORBIDDEN` as defined above. After suspension the revoked session normally returns 401; neither 401 nor 403 is eligible for use by resources.
- `404 NOT_FOUND`: absent or inaccessible owned resource. Do not disclose another owner's existence.
- `409 LAST_ADMIN`, `409 MFA_STATE_CONFLICT`: explicit state conflict without mutation.
- `413 REQUEST_TOO_LARGE`, `415 UNSUPPORTED_MEDIA_TYPE`, `422 VALIDATION`: known field paths/codes, no raw Pydantic input values.
- `429 RATE_LIMITED`: integer `Retry-After` seconds; no account existence information.
- `503 DEPENDENCY_UNAVAILABLE`: DB/mail queue/storage dependency outage with safe message and request ID. Auth initiation can return 202 only once the delivery record is durably written, not if its transaction failed. SMTP delivery outage after durable queueing remains 202.

Unknown application exceptions become safe `500 INTERNAL_ERROR` with the same envelope. `retryable=true` only for temporary 429/503 outcomes; all other errors use false. `fields=[]` unless it is a field validation error. Each request gets a validated/generated request UUID; never trust arbitrary unbounded correlation headers or include secrets in messages.

## Abuse policy and concurrency

Use PostgreSQL-backed fixed-window counters with atomic upsert/check in `auth_rate_limits`, not per-process dictionaries. Store `scope`, `key_digest`, `window_start`, `count`, `expires_at` with a unique scope/key/window constraint. Keys are HMACs under `PLATFORM_AUTH_RATE_KEY` of normalized email or canonical client IP; never store the candidate email for nonexistent accounts in metrics. Separate dimensions apply independently:

- All auth route requests: 120/minute per client IP; new anonymous session creation: 30/minute/IP.
- Login: 5 attempts/15 minutes/email and 30/15 minutes/IP, counting every attempt regardless of outcome/existence.
- Register/reset-request: 3/hour/email and 20/hour/IP; mail resend additionally at most one/minute/email/purpose.
- MFA enrollment/confirm/verify/step-up: 5 attempts/5 minutes/session and 30/15 minutes/IP. On the fifth failed MFA credential attempt revoke the restricted session; a fresh password login is required. Authenticated step-up failures do not extend or upgrade privilege.
- Confirmation token submissions: 10/15 minutes/IP in addition to the global auth limit. No unbounded queueing for scrypt or SMTP.

All limits above are configurable policy defaults, not measured hardware capacity. Limit keys and clock-window math are shared across replicas; DB failure denies the operation. Configure the trusted-proxy boundary so an attacker cannot evade IP limits with arbitrary forwarded headers. Enforce the queued-mail global bound under a transaction-level advisory lock; overflow returns generic 429 without a token. Prune expired tokens/rate counters and anonymous sessions via CLI job; pruning is housekeeping, not expiry enforcement.

Exact configuration names and defaults for this contract:

- Required SaaS values: the shared `DATABASE_URL`, `PLATFORM_WEB_ORIGIN`, `PLATFORM_AUTH_CSRF_KEY`, `PLATFORM_AUTH_RATE_KEY`, `PLATFORM_AUTH_MFA_KEY`. `PLATFORM_POSTGRES_SCHEMA=platform` selects the platform schema in that shared database. CSRF/rate keys are separate 32-byte base64url secrets. `PLATFORM_ENV=development`, `PLATFORM_HOST=127.0.0.1`, `PLATFORM_PORT=8000`; `PLATFORM_TRUSTED_PROXY_CIDRS=[]`, `PLATFORM_ALLOWED_HOSTS=['127.0.0.1']` in development, explicitly set for staging/production. Bind/public-origin policy follows the cookie section.
- `PLATFORM_AUTH_ANONYMOUS_TTL_SECONDS=600`, `PLATFORM_AUTH_MFA_TTL_SECONDS=300`, `PLATFORM_AUTH_SESSION_TTL_SECONDS=43200`, `PLATFORM_AUTH_IDLE_TTL_SECONDS=1800`, `PLATFORM_AUTH_IDLE_TOUCH_SECONDS=60`, `PLATFORM_AUTH_MAX_SESSIONS=5`, `PLATFORM_AUTH_STEP_UP_SECONDS=300`, `PLATFORM_AUTH_REGISTER_TTL_SECONDS=86400`, `PLATFORM_AUTH_RESET_TTL_SECONDS=1800`, `PLATFORM_AUTH_HASH_CONCURRENCY=2`.
- `PLATFORM_AUTH_RATE_GLOBAL_PER_MINUTE=120`, `PLATFORM_AUTH_RATE_BOOTSTRAP_PER_MINUTE=30`, `PLATFORM_AUTH_RATE_LOGIN_EMAIL_PER_15_MINUTES=5`, `PLATFORM_AUTH_RATE_LOGIN_IP_PER_15_MINUTES=30`, `PLATFORM_AUTH_RATE_MAIL_EMAIL_PER_HOUR=3`, `PLATFORM_AUTH_RATE_MAIL_IP_PER_HOUR=20`, `PLATFORM_AUTH_MAIL_RESEND_SECONDS=60`, `PLATFORM_AUTH_RATE_MFA_SESSION_PER_5_MINUTES=5`, `PLATFORM_AUTH_RATE_MFA_IP_PER_15_MINUTES=30`, `PLATFORM_AUTH_RATE_CONFIRM_IP_PER_15_MINUTES=10`. Register and reset have separate purpose-scoped email/hour counters; global/IP counters remain aggregate.
- `PLATFORM_AUTH_MAIL_MODE=file` in development, `smtp` required in staging/production; `PLATFORM_AUTH_MAIL_DIR=data/platform-auth-mail`, `PLATFORM_AUTH_MAIL_QUEUE_MAX=10000`, `PLATFORM_AUTH_MAIL_BATCH_SIZE=25`, `PLATFORM_AUTH_MAIL_LEASE_SECONDS=60`, `PLATFORM_AUTH_MAIL_ATTEMPTS=5`, `PLATFORM_AUTH_SMTP_TIMEOUT_SECONDS=10`. Retry delays after failed attempts are 60, 120, 240, 480 seconds, capped by token expiry; final failure erases payload and sets state `expired`. SMTP requires `PLATFORM_AUTH_SMTP_HOST`, `PLATFORM_AUTH_SMTP_PORT`, `PLATFORM_AUTH_SMTP_USERNAME`, `PLATFORM_AUTH_SMTP_PASSWORD`, `PLATFORM_AUTH_MAIL_FROM`, and `PLATFORM_AUTH_SMTP_TLS_MODE=starttls|tls` (no plaintext mode). Required SMTP values have no insecure/default credentials.
- Reject zero/negative duration/limit/concurrency/port values and unknown profiles/modes; ensure idle TTL and touch interval do not exceed absolute session TTL, step-up does not exceed session TTL, and mail lease exceeds SMTP timeout. Security-policy constants such as token sizes, TOTP digits/window, body size and minimum scrypt work factor cannot be weakened through environment settings.

## Stable interfaces produced and consumed

`platform_api/auth/types.py` produces a frozen `Principal`:

```text
Principal(user_id: UUID, role: Literal['user','admin'], session_id: UUID,
          email_verified_at: datetime, password_verified_at: datetime,
          mfa_verified_at: datetime|None, step_up_at: datetime|None)
```

`platform_api/auth/dependencies.py` produces the following FastAPI dependencies; `AsyncSession` is always from `platform_api.db.session`, never `bot.db.session`:

```text
async require_user(request: Request, db: AsyncSession) -> Principal
async require_admin(principal: Principal) -> Principal
async require_step_up(principal: Principal, clock: Clock) -> Principal
async require_csrf(request: Request, db: AsyncSession) -> AuthSessionContext
```

The implementation must express their actual FastAPI `Depends` wiring explicitly. `require_user` reloads the session and user, verifies active/verified status and auth version, and accepts only full sessions; for an admin it additionally checks enabled MFA and the session's recorded MFA verification. `require_admin` consumes that verified Principal and enforces admin role and non-null MFA verification; `require_step_up` consumes `require_admin`, not just any Principal. Resource handlers consume these dependencies directly or on a router with no unguarded subrouter. Tests enumerate routes to detect accidental public additions.

Owner queries consume `principal.user_id` with their resource identifier in the same query: `WHERE resource.id=:id AND resource.owner_user_id=:principal_user_id`. They never read an owner header, body field, query parameter, cookie email, or frontend role. Admin cross-account reads use distinct admin policy paths with restricted content/audit, not a special case hidden in ordinary owner queries.

`platform_api/auth/delivery.py` produces `async enqueue_auth_message(db, *, token_id: UUID, email: str, purpose: Literal['register','password_reset'], token: str, expires_at: datetime) -> None` and `async deliver_pending_auth_messages(settings, *, limit: int) -> DeliveryBatchResult`. Enqueue participates in the caller's transaction; the worker uses bounded retries, never logs plaintext, and erases successful/expired payloads. `DeliveryBatchResult(sent: int, deferred: int, expired: int)` contains counts only. A CLI `deliver-auth-mail --once --limit 25` can run from the deployment scheduler; delivery need not depend on RabbitMQ identity topology.

`platform_api/accounts/service.py` consumes a successful registration transaction and produces `async initialize_user_account(db, *, user_id: UUID, now: datetime) -> None`. Its wallet/profile creation is idempotent by user ID and stays in the same transaction. The auth module must not implement balance arithmetic or publish irreversible external side effects before commit. A newly verified account starts with no paid credit unless a separately declared test-credit policy provisions it.

`web/src/lib/platform-api-client.ts` produces `PlatformApiClient`, `PlatformApiError` (status/code/requestId/fieldErrors), `getSession(): Promise<SessionResponse>`, `login(...)`, `logout(): Promise<void>`, `request<T>(path, options)`, and session-state notifications. It consumes the wire models above, performs cookie/CSRF handling, supports 204, and never treats client state as authorization. Existing `ApiClient` remains the local client; a profile-specific backend selector chooses intentionally between them.

## Create / Modify / Delete map for implementation

This is the proposed work map. The only artifact written by this design task is this Markdown file.

Create:

- `platform_api/__init__.py`, `platform_api/app.py`: app factory `create_app(settings)`; mount auth and guarded owner/admin routers; safe error mapping, allowed-host/origin policy, readiness.
- `platform_api/config.py`: `PlatformSettings` and exact profile/cookie/key/database/port/delivery/rate/hash-work config validation.
- `platform_api/db/__init__.py`, `platform_api/db/base.py`, `platform_api/db/session.py`, `platform_api/db/schema.py`: independent `Base`, engine/session factory, `get_db`, expected-schema check, explicit fresh initialization; no automatic local imports/fallback.
- `platform_api/auth/__init__.py`, `types.py`, `models.py`, `schemas.py`: identity/session/token/MFA/rate/mail tables, principal and exact API schemas.
- `platform_api/auth/passwords.py`, `sessions.py`, `csrf.py`, `mfa.py`, `rate_limits.py`: bounded password work, hashed sessions/revocation, CSRF derivation, encrypted factors/replay protection, atomic limits.
- `platform_api/auth/service.py`, `dependencies.py`, `routes.py`, `delivery.py`: transaction orchestration, guards, explicit routes, durable mail adapter/dispatcher.
- `platform_api/accounts/__init__.py`, `models.py`, `service.py`, `routes.py`, `admin_routes.py`: fresh account ownership/profile initialization and admin user metadata/suspend/restore. User identity table stays owned by `auth/models.py`; do not define it twice.
- `platform_api/audit.py`, `platform_api/cli.py`: append-only transaction audit and platform-schema/bootstrap/create-admin/recover-admin-MFA/prune/deliver commands. Domain audit table must have one owner shared with the master schema.
- `web/src/types/auth.ts`, `web/src/lib/platform-api-client.ts`, `web/src/lib/auth-state.ts`: exact wire types, typed cookie/CSRF client and explicit loading/anonymous/restricted/authenticated state.
- `web/src/app/login/page.tsx`, `register/page.tsx`, `verify-email/page.tsx`, `forgot-password/page.tsx`, `reset-password/page.tsx`, `settings/page.tsx`, `admin/users/page.tsx`, `admin/settings/page.tsx`: signup/login/reset, profile/sessions, account admin and TOTP enrollment/step-up surfaces; auth routes must be reachable before full session.
- `web/src/components/AuthGate.tsx`, `AdminGate.tsx`, `StepUpDialog.tsx`: visible access state and flows, consuming server responses; never security substitutes for backend guards.
- `tests/platform_api/conftest.py`, `test_auth_schema.py`, `test_auth_passwords.py`, `test_auth_sessions.py`, `test_auth_routes.py`, `test_auth_csrf.py`, `test_auth_mfa.py`, `test_auth_delivery.py`, `test_account_authorization.py`, `test_auth_cli.py`, `test_auth_route_inventory.py`; `web/src/lib/platform-api-client.test.mjs`, `auth-state.test.mjs`.
- `docs/first-release/auth-runbook.md`: exact shared-database/platform-schema/bootstrap/enroll/mail/recovery commands, trusted ingress/origin setup, secrets generation/backup and incident revocation procedure.

Platform tests use disposable PostgreSQL databases provisioned by the verifier; they never point `DATABASE_URL` at the runtime database, never use SQLite, and never create or migrate the runtime `public`/`platform`/`phoenix` schemas.

Modify:

- `pyproject.toml` / `uv.lock`: only selected direct pins/new PyOTP and existing httpx dev pin; resolve and verify compatibility without broad dependency upgrades.
- `.env.example`: one shared `DATABASE_URL`, the `platform`/`phoenix` schema selections, separately namespaced platform settings and network dependencies; no committed secrets. Keep local settings operational.
- `.gitignore`: exclude `data/platform-auth-mail/` and all generated auth artifacts; existing `data/` ignore may already suffice, verify rather than duplicate.
- `web/src/app/layout.tsx`, `web/src/components/Sidebar.tsx`: profile-aware auth state and customer/admin navigation; no user-facing global prompt control in SaaS.
- `web/src/lib/api-client.ts`: at most expose its existing local selection; do not retrofit SaaS credentials into the loopback local client. Actual platform requests use the new client.
- `bot/config.py`, `bot/bot.py:mount_api`, `bot/control.py:mount_control`: explicit mode isolation and loopback enforcement for local management; SaaS runner startup must not mount current `/api/agents`, `/api/prompts`, `/api/catalog`, `/settings/stt` as unauthenticated compatibility endpoints. Runner authenticated signaling is handled by its own task.
- `scripts/voice-ai.sh`: retain local start/test commands; add explicit platform command dispatch only if the shared scaffold slice owns it, respecting host-port checks. No auth behavior injected into ordinary local startup.
- `README.md`: document modes and auth entrypoints/deployment qualifications; do not replace measured local performance figures with unmeasured SaaS figures.

Delete: none in this slice. Do not move `web/`, delete local API files, rename voice metrics, remove local records, or edit `bot/db` to make platform authentication work.

## Review focus and implementation test gates

The highest-risk cases are cookie sessions crossing a login-state boundary, concurrent single-use token submissions, revoked privilege appearing valid in another replica, origin/token misuse on anonymous auth endpoints, and an offline launch accidentally importing platform/email dependencies. Each is pinned below. Future implementers run the tests; this design task did not run them or claim they pass.

### Task 1: Fresh platform schema and isolated startup

Consumes the global constraints and dependency decisions. Produces `PlatformSettings`, independent `Base`, explicit schema initialization and a database dependency usable by auth.

- [ ] Write `test_auth_schema.py` cases proving platform tables exist only in the `platform` schema of a disposable PostgreSQL database, do not register on `bot.db.base.Base`, and reject SQLite URLs plus `public`/`phoenix` platform schemas. Monkeypatch imports to fail on `bot.db` access. Assert a missing `platform` schema fails readiness/startup rather than running create-all automatically.
- [ ] Add settings tests for secure production cookies, exact origins, absent keys, required DB URL, loopback-only development insecure mode, and host port collision refusal. No live service starts during unit tests.
- [ ] Run `uv run pytest tests/platform_api/test_auth_schema.py`; observe failures for missing platform components before implementation. Add only this task's files/settings/pins, resolve lock, then rerun to pass.

### Task 2: Passwords, sessions, CSRF and route guard

Consumes task 1 DB and produces `Principal`, `AuthSessionContext`, `require_user`, `require_admin`, `require_step_up`, `require_csrf`, cookie policy and error schemas.

- [ ] Write `test_auth_passwords.py`: unique salts; known-password verification; wrong password; Unicode/spaces preserved; min/max codepoint and byte bounds; corrupt/unbounded hash encoding; dummy verify for unknown account; executor saturation without event-loop blocking. No benchmark threshold is asserted without deployment measurement.
- [ ] Write `test_auth_sessions.py`: cookie stores only opaque value; DB has only digest; old cookie fails after rotation; idle and absolute expiry with injected clock; cross-replica logout/version invalidation; maximum-five eviction under concurrent login; DB outage fails closed. Assert ordinary voice-session rows grant no identity.
- [ ] Write `test_auth_csrf.py`: missing/wrong CSRF and Origin (including `null`, attacker suffix domain and wrong port) cannot mutate; login CSRF blocked; preflight works only for configured origin; JSON enforcement/body limit; no credentialed wildcard CORS; production cookie attributes/expiration; dev hosts cannot be mixed; fragment links never consume a token on GET.
- [ ] Run those three test files to observe missing-function failures; implement only declared contracts, then rerun. Enumerate platform routes in `test_auth_route_inventory.py` to require full-session dependencies everywhere except the explicit bootstrap/health/guest/internal caller classes.

### Task 3: Signup/reset, durable delivery and account profile

Consumes task 2 guards and task 1 transactions; produces exact registration/login/reset/me/session endpoints and the `initialize_user_account` / delivery interfaces.

- [ ] Write `test_auth_routes.py`: email normalization/alias policy; unknown extra role fields rejected; signup starts without creating a usable user; completion chooses password after token possession; generic duplicate and reset-request response; token wrong-purpose/expiry/reuse; concurrent completion has exactly one winner and one account-owned initialization; password reset revokes all sessions without clearing admin MFA/status; password change rotates current and revokes others; logout/session-delete 204 handling.
- [ ] Write `test_auth_delivery.py`: no DNS during email validation; file delivery permissions and no public route; generated URL ignores Host header and uses fragment; transaction failure creates neither token nor mail; duplicate dispatch does not issue a fresh token; encryption/tamper/expired payload behavior; bounded SMTP timeout/retry and post-delivery erasure. Replace SMTP/network with fakes; do not send mail during tests.
- [ ] Write `test_account_authorization.py`: two users cannot list/read/change/revoke each other's resources; owner IDs and role/status/email writes cannot be injected through profile/API bodies; resource misses and foreign ownership both 404; admin cannot bypass owner queries through ordinary routes; local client behavior remains separate.
- [ ] Run the three test files to observe failures; implement the endpoints/services and rerun. Use real PostgreSQL transaction/concurrency tests for uniqueness and token consumption, not SQLite substitutes.

### Task 4: Admin bootstrap, MFA and privileged authorization

Consumes prior principal/DB/error contracts; produces operator commands, restricted MFA flow, step-up dependency and admin account endpoints.

- [ ] Write `test_auth_mfa.py`: deterministic RFC-compatible TOTP vectors; ±1-step limits; concurrent replay allows one winner; pending secret expiry; encrypted user binding; old/replaced secret rejected; restricted login cannot access any resource; current password required for enrollment; fresh MFA upgrades cookie; stale step-up denied; TOTP secret absent from every non-enrollment response/log/audit.
- [ ] Write `test_auth_cli.py`: simultaneous first bootstrap yields one admin; existing email/admin refusal; no default/env/CLI password; protected recovery revokes sessions and requires reenrollment; no public role promotion; readiness blocks until an enrolled active admin exists. Validate last-admin protection under concurrent suspension.
- [ ] Extend `test_account_authorization.py` for normal-user 403 on all admin routes, stale-step-up suspension denial, self/last-admin safety, suspension immediately blocking admissions/login and invalidating sessions across clients, restore granting no sessions or credits, and audit failure rolling back the admin change.
- [ ] Run those files to observe failures; implement this task, rerun, and include the actual `require_admin`/`require_step_up` route-dependency inventory as a regression test.

### Task 5: Frontend, local-mode isolation and completion verification

Consumes all wire models; produces auth UI, typed platform client, correct profile navigation, local-mode isolation and runbook.

- [ ] Write `platform-api-client.test.mjs` cases for credentials on every request, mutation-only CSRF header, token update after rotation, 204 without JSON parse, exact typed error code/field/request ID, session loading states, no storage of secrets, and no automatic retry of a submitted non-idempotent operation after CSRF refresh.
- [ ] Write `auth-state.test.mjs` for anonymous login/reset flow, password-verified MFA gate, normal-user admin redirect, expired session while unsaved work exists, fragment removal before other requests, allowed return-path validation, and step-up dialog preserving a pending operation without submitting it twice.
- [ ] Add local-mode regressions proving `scripts/voice-ai.sh start` uses only local workflow, platform module/email/network calls are not imported, non-loopback management configuration is rejected, and SaaS runner route inventory does not expose legacy CRUD/settings. Test mode contracts via fake apps/settings, not actual model/service starts.
- [ ] Implement frontend/profile wiring and docs, run `cd web && bun test && bun run typecheck` and `uv run pytest tests/platform_api`. Use the repository's actual script names as present, and then run `scripts/voice-ai.sh test` as the full repository gate.
- [ ] Qualify browser behavior with TLS production-like ingress: verify actual Set-Cookie, same-origin routing, logout, mail-fragment reset, admin MFA and stale-step-up flow in a browser. Confirm bad Origin cannot mutate. Dev cross-port behavior alone does not qualify production cookies.
- [ ] Record measured deployment hash memory/latency and auth limits under the intended API concurrency in the auth runbook. No local voice benchmark is needed unless implementation changes the audio/model/prompt path. Keep generated artifacts under ignored `data/` and never commit audio fixtures or mail secrets.

This plan ends at a reviewable first-release identity implementation. Deployment, public ingress activation and any real mail/customer-data operations remain their own explicitly requested operational actions.

# Voice Agent Builder

A self-service voice agent platform. Owners create agents, questions, validation rules and routing; callers use a public `/a/[slug]` page; administrators manage global prompt policy, users and suspensions. Voice calls use Pipecat SmallWebRTC with provider keys held only by the backend. PostgreSQL stores configuration, call snapshots, messages and answers. Phoenix receives redacted OpenTelemetry timing and error spans.

Stack: FastAPI + async SQLAlchemy/Alembic + PostgreSQL, Next.js 15 + Tailwind, Pipecat SmallWebRTC (Deepgram STT, OpenAI LLM, Cartesia/ElevenLabs TTS), Phoenix.

## Quick start (Docker Compose)

Prerequisites: Docker Desktop/Compose, a browser with microphone access, and real provider credentials for voice calls.

1. Create the environment file and fill in every required value:

   ```sh
   cp .env.example .env
   ```

   At minimum set `SESSION_SECRET`, `POSTGRES_PASSWORD`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, and one TTS key. See [Environment](#environment) below.

2. Build and start the services:

   ```sh
   docker compose up --build -d
   ```

3. Apply migrations and create the administrator. Both commands are idempotent:

   ```sh
   docker compose exec backend alembic upgrade head
   docker compose exec backend python -m app.bootstrap
   ```

4. Open <http://localhost:3000>. Sign in as the administrator, or register a normal owner account. Phoenix is at <http://127.0.0.1:6006> (loopback only).

The backend starts before the migration step by design; API requests need the schema, so run steps 3 and 4 in order. The postgres init script creates the separate `voice_test` database used only by pytest.

### Run the verification suites

```sh
docker compose exec backend pytest
docker compose exec frontend npm run build
```

`backend/tests/conftest.py` forces `DATABASE_URL` to `TEST_DATABASE_URL`, and the test suite drops and recreates tables there. It must never point at the application database. Compose supplies `TEST_DATABASE_URL` automatically; without Compose, point it at a local disposable database (for example one running on port 55432).

## Environment

`.env.example` is the tracked reference; `.env` is ignored and must never be committed. The backend reads:

| Variable | Purpose |
| --- | --- |
| `ENVIRONMENT` | `development` for local HTTP; `production` makes session cookies `Secure` (requires HTTPS). |
| `APP_ORIGIN` | Exact frontend origin checked on state-changing requests, e.g. `http://localhost:3000` or `https://voice.example.com`. |
| `SESSION_SECRET` | At least 32 characters; signs session cookies. Rotating it signs everyone out. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Used only by `python -m app.bootstrap`; password must be at least 12 characters. Existing non-admin emails are never promoted. |
| `DATABASE_URL` | Async PostgreSQL URL, `postgresql+asyncpg://...`. Compose supplies the service host and password. |
| `TEST_DATABASE_URL` | Disposable database used by pytest. Compose supplies it; without Compose, set it to a local disposable database. |
| `DEEPGRAM_API_KEY` | Deepgram `nova-3` speech recognition. |
| `OPENAI_API_KEY` | OpenAI language model and structured extraction. Note the exact name: `OPENAI_KEY` is not read. |
| `CARTESIA_API_KEY`, `ELEVENLABS_API_KEY` | TTS credentials; at least one provider is required. |
| `VERIFIED_OPENAI_MODELS` | Comma-separated OpenAI models owners may publish. |
| `VERIFIED_TTS_VOICE_CATALOG` | One-line JSON allowlist of provider → model → voice ID → supported languages. |
| `PHOENIX_OTLP_ENDPOINT` | OTLP HTTP trace endpoint. Leave empty to disable export and make the owner trace view return 503; Compose defaults it to the Phoenix service only when unset. |
| `PHOENIX_URL` | Phoenix base URL for scoped trace reads. Compose defaults it to the Phoenix service only when unset. |
| `PHOENIX_PROJECT_NAME` | Phoenix project header, default `voice-agent`. |
| `SMALLWEBRTC_STUN_URLS` | Comma-separated STUN URLs; defaults to Google STUN. |
| `SMALLWEBRTC_TURN_URLS` | Comma-separated TURN/TURNS URLs. Required for many public-network calls. |
| `SMALLWEBRTC_TURN_SHARED_SECRET` | TURN REST shared secret, at least 32 bytes, only with TURN URLs. |
| `SMALLWEBRTC_TURN_CREDENTIAL_TTL_SECONDS` | TURN credential lifetime, 60–3600 seconds. |
| `BACKEND_URL` | Next.js proxy target. Compose sets `http://backend:8000`; local dev defaults to `http://127.0.0.1:8000`. |

Provider/model/voice publishing is fail-closed. A draft saves incomplete, but publish returns field errors unless the exact combination is approved by server configuration. While an agent is published, step changes must keep it valid; the question editor adds new questions disabled until you configure and enable them.

```dotenv
VERIFIED_OPENAI_MODELS=gpt-4.1-mini,gpt-4o-mini
VERIFIED_TTS_VOICE_CATALOG={"cartesia":{"sonic-3.6":{"your-voice-id":["en","hi"]}},"elevenlabs":{"eleven_flash_v2_5":{"your-voice-id":["en"]}}}
```

Supported combinations in this build: Deepgram `nova-3`, OpenAI models from the allowlist, Cartesia `sonic-3.6`, ElevenLabs `eleven_flash_v2_5`, languages `en` and `hi`. A call uses one language. Publishing more than one language is rejected until live TTS language switching is verified end to end.

## Deployment notes

### HTTPS and cookies

Terminate TLS at a reverse proxy (Caddy, nginx, a cloud load balancer) and proxy to the frontend on port 3000. Keep the browser on one origin so `/api/*` is same-origin and cookie/CSRF handling works. Then set:

```dotenv
ENVIRONMENT=production
APP_ORIGIN=https://voice.example.com
```

With `ENVIRONMENT=production` over plain HTTP the browser will not store the `Secure` session cookie, so local HTTP must use `ENVIRONMENT=development`.

### Phoenix access and privacy

Compose binds Phoenix to `127.0.0.1` only. Do not publish port 6006 publicly; the application does not put an admin login in front of Phoenix itself. Use an SSH tunnel or an authenticated proxy if you need remote access. Owners use the app's authenticated trace page at `/sessions/{session_id}/trace`, which queries only that owned session and returns span names, timing, status and error type — never attributes.

Tracing redacts prompt text, transcript, answers, tool payloads, provider settings, credentials and API keys before export. Leave `PHOENIX_OTLP_ENDPOINT` empty to disable tracing completely. If Phoenix is unavailable, the trace page shows an unavailable state with a retry action.

### STUN, TURN and networks

STUN alone works on the same machine/LAN. Calls across strict NATs, corporate networks, or Docker Desktop on macOS commonly need a TURN server. Configure coturn (or a managed TURN service) with REST credentials:

```dotenv
SMALLWEBRTC_TURN_URLS=turn:turn.example.com:3478?transport=udp,turns:turn.example.com:5349
SMALLWEBRTC_TURN_SHARED_SECRET=a-random-secret-of-at-least-32-bytes
```

Do not claim public calling works across networks without a working TURN path. The browser also requires a secure context: `https://` or `http://localhost` for microphone permission.

### Single-process limits

The backend runs as one process by design. Signaling tokens, rate limits and live-call caps are in memory, so do not run multiple backend workers/replicas without replacing them with shared state. Agent suspension blocks new calls without terminating existing calls. Active-call deletion is rejected; call duration and concurrency policy come from the frozen admin policy snapshot.

## Development without Docker

Backend (Python 3.12–3.13):

```sh
cd backend
uv sync --all-groups
set -a; source ../.env; set +a
alembic upgrade head
python -m app.bootstrap
uvicorn app.main:app --reload
pytest
```

Frontend (Node 22):

```sh
cd frontend
npm ci
BACKEND_URL=http://127.0.0.1:8000 npm run dev
```

The frontend rewrites `/api/*` to `BACKEND_URL`; there is no CORS configuration because the browser should stay on one origin.

## Dev login buttons

For local development only, set `ALLOW_DEV_LOGIN=true` and keep `ENVIRONMENT=development`. The login page then shows “Sign in as User” and “Sign in as Admin” buttons that issue sessions for the seeded `DEV_USER_EMAIL`/`DEV_USER_PASSWORD` and `ADMIN_EMAIL`/`ADMIN_PASSWORD` accounts. The buttons and endpoints are disabled (404) when either condition is false.

## Roles and data boundaries

- Registration always creates a `user`; there is no role field in registration and no default administrator.
- Owners see and mutate only their own agents, steps, sessions, messages, answers and traces. Another owner's identifiers return 404, not 403.
- Administrators see owner/agent metadata, counts and suspension controls; admin directories do not return private prompts, answers or transcripts.
- Public callers receive agent name/description/languages, a short-lived single-use signaling grant, and call state. They never receive internal instructions, question rules, confirmed answers, diagnostics, provider configuration, API keys or Phoenix access.
- Session snapshots freeze the agent configuration, prompts, language and policy at call creation. Editing an agent or global prompt does not change an in-flight call.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Login appears to succeed but no session persists | `ENVIRONMENT=production` over HTTP; use HTTPS or set `development`. |
| State-changing request returns 403 | `APP_ORIGIN` must exactly match the browser origin (scheme, host, port). |
| Publish rejects providers | Exact key names, model allowlist, voice catalog language entry, and one language only. |
| Call returns 503 | Missing `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, or the selected TTS key. |
| Call cannot connect across networks | Configure TURN; check firewall/UDP and the ICE server endpoint. |
| Trace page returns 503 | Phoenix is not configured/reachable, or `PHOENIX_OTLP_ENDPOINT`/`PHOENIX_URL` are wrong. |
| `pytest` cannot connect | The disposable `voice_test` database does not exist or `TEST_DATABASE_URL` is wrong. |
| Postgres already has a volume but not `voice_test` | The init script runs only on first initialization; create it manually: `docker compose exec postgres psql -U voice -d voice -c 'CREATE DATABASE voice_test;'` |

## What is not verified by automated tests

The test suite uses deterministic substitutes and does not prove live audio behavior. A real browser call with actual provider credentials is still required to verify microphone capture, Deepgram transcription, LLM extraction, TTS playback/interruption, answer persistence, completion, and cross-network connectivity. Multilingual language switching is not enabled in this build.

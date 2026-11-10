# Voice Agent Builder

A self-service voice agent platform. Owners create agents with question steps and answer-based routing; callers use a public `/a/[slug]` page; administrators manage global prompt policy, users and suspensions. Voice calls use Pipecat SmallWebRTC with provider keys held only by the backend. PostgreSQL stores configuration, call snapshots, messages and answers. Phoenix receives redacted OpenTelemetry timing and error spans.

Stack: FastAPI + async SQLAlchemy/Alembic + PostgreSQL, Next.js 15 + Tailwind, Pipecat SmallWebRTC (Azure Speech and Deepgram STT, OpenAI LLM, Cartesia/ElevenLabs TTS), Phoenix.

## Quick start (Docker Compose)

Prerequisites: Docker Desktop/Compose, a browser with microphone access, and real provider credentials for voice calls.

1. Create the environment file and fill in every required value:

   ```sh
   cp .env.example .env
   ```

   At minimum set `SESSION_SECRET`, `POSTGRES_PASSWORD`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `DEV_USER_EMAIL`, `DEV_USER_PASSWORD`, `OPENAI_API_KEY`, STT credentials (`AZURE_SPEECH_API_KEY` with `AZURE_SPEECH_REGION` or `AZURE_SPEECH_ENDPOINT`, or `DEEPGRAM_API_KEY`), and one TTS key. Both account passwords must have at least 12 characters. See [Environment](#environment) below.

2. Build and start the services:

   ```sh
   docker compose up --build -d
   ```

3. Apply migrations and seed the administrator and development user. Both commands are safe to rerun:

   ```sh
   docker compose exec backend alembic upgrade head
   docker compose exec backend python -m app.seed
   ```

4. Open <http://localhost:3000>. Sign in using either configured account, or register another normal owner account. Phoenix is at <http://127.0.0.1:6006> (loopback only).

The backend starts before the migration step by design; API requests need the schema, so run steps 3 and 4 in order. The postgres init script creates the separate `voice_test` database used only by pytest.

### Run the verification suites

```sh
docker compose exec backend pytest
docker compose exec frontend npm run test:captions
docker compose exec frontend npm run lint -- --max-warnings 0
docker compose exec frontend npm run build
```

`backend/tests/conftest.py` forces `DATABASE_URL` to `TEST_DATABASE_URL`, and the test suite drops and recreates tables there. It must never point at the application database. Compose supplies `TEST_DATABASE_URL` automatically; without Compose, point it at a local disposable database (for example one running on port 55432).

Tests use the same pooled engine as production. AnyIO gives each async test a fresh event loop, so `conftest.py` disposes the pool after each async test in that test's loop; sync `TestClient` WebSocket tests use one lifespan loop per test. Do not replace this with a pytest-only `NullPool` bypass.

Scoped dependency overrides patch the lint parser's `minimatch` and Next's
`postcss` dependencies while keeping Next 15 and ESLint 9. The locked graph is
verified with the Node 22 Docker runtime.

### Structured agent prompts

AI-generated agent instructions use five sections, in order: `Role & Spoken Persona`,
`Primary Objective`, `Turn-by-Turn Conversation Flow`, `Call Termination & Wrap-Up Criteria`,
and `Spoken Voice Rules & Fallbacks`. Each numbered conversation step includes what to
say or ask and what to collect. Generated instructions with missing or reordered
headings, or a step without spoken-action or collection guidance, use structured
fallback instructions instead. Agent names and fallback purposes are kept on one line.

Prompt generation and enhancement ask for natural capability clarification instead
of fixed scripts, instruct agents to substitute collected values rather than read
bracketed placeholders aloud, and tell agents to invoke `end_call` when the
conversation is complete.

The single-prompt editor supports Write, Split, and Preview modes without changing
the Markdown source or draft autosave behavior. Test chat stays editable while an
assistant reply streams; sending another message does not move incoming tokens out
of the assistant's bubble. Warnings preserve partial replies and clear streaming state.

Use **Web call** in the agent editor to open a call modal, select an available
language, start or end a call, and inspect its scoped trace. **Chat** opens the
separate text panel; **History** lists saved sessions and refreshes after a web call ends.

Live voice captions update each speaker's unfinished turn independently, including
when caller and agent events overlap. Ending a call or losing its transport keeps
the received text and clears unfinished streaming markers.

Chat and voice runtimes expose an `end_call` tool, and generated prompts include
instructions to use it when the conversation is complete. In chat, the agent can
provide its farewell in response text, the tool's `message` argument, or both.
When both are present, the runtime keeps the streamed response and appends the
tool farewell unless that exact farewell already ends the response. It saves
the complete closing turn before marking the session `completed` and sending
`session_ended`, then closes the WebSocket without requiring another user turn.
Tool-provided voice farewells wait for synthesis and backend audio-transport
completion, with a ten-second safety limit. Already delivered farewells are not
spoken again, while additional closing information is preserved. Final assistant
captions and saved messages follow audio completion order. A late final
transcription still becomes a caller turn after the VAD fallback window expires.
Isolated backchannels that overlap bot playback are captioned and saved without
starting another model turn, even when their transcript finalizes after audio
drains. New utterances outside playback and meaningful commands still get normal
responses.

## Environment

`.env.example` is the tracked reference; `.env` is ignored and must never be committed. The backend reads:

| Variable | Purpose |
| --- | --- |
| `ENVIRONMENT` | Use exactly `development` for local HTTP; `production` makes session cookies `Secure` (requires HTTPS). |
| `APP_ORIGIN` | Exact frontend origin checked on state-changing requests and dev login, e.g. `http://localhost:3000` or `https://voice.example.com`. |
| `SESSION_SECRET` | At least 32 characters; signs session cookies. Rotating it signs everyone out. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Used by `python -m app.bootstrap`; password must be at least 12 characters. Existing non-admin emails are never promoted. These credentials also identify the admin development login when enabled. |
| `ALLOW_DEV_LOGIN` | Set to `true` only for local development to show passwordless login buttons. The endpoint also requires `ENVIRONMENT=development` and an exact matching `Origin`. Default: disabled. |
| `DEV_USER_EMAIL`, `DEV_USER_PASSWORD` | Optional development-user button credentials. Must match an existing `user` account; dev login never creates or changes accounts. |
| `DATABASE_URL` | Async PostgreSQL URL, `postgresql+asyncpg://...`. Compose supplies the service host and password. |
| `TEST_DATABASE_URL` | Disposable database used by pytest. Compose supplies it; without Compose, set it to a local disposable database. |
| `DEEPGRAM_API_KEY` | Deepgram `nova-3` speech recognition. |
| `AZURE_SPEECH_API_KEY` | Azure Speech service API key for speech recognition (`azure-speech`). |
| `AZURE_SPEECH_REGION` | Azure Speech region (e.g. `eastus`, `centralindia`). Standard key+region setup; required unless `AZURE_SPEECH_ENDPOINT` is set. |
| `AZURE_SPEECH_ENDPOINT` | Optional full Azure Speech SDK endpoint URL (overrides `AZURE_SPEECH_REGION`). |
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

PostgreSQL connections are pooled per backend process: 10 persistent connections plus 10 overflow connections, a 10-second checkout timeout, pre-ping, and a 5-minute recycle. The FastAPI lifespan disposes the pool on shutdown. Compose runs one uvicorn worker; if you add workers or replicas, multiply the pool cap and keep the total below PostgreSQL `max_connections` (100 in the Compose image).

Provider/model/voice publishing is fail-closed. A draft saves incomplete, but publish returns field errors unless the exact combination is approved by server configuration. The visual-flow editor lets owners set each step's title and objective, collect free-form answers, and route to another step or End call. Saved steps are active.
Migration 0008 drops legacy step configuration columns; historical session snapshots remain unchanged.

```dotenv
VERIFIED_OPENAI_MODELS=gpt-4.1-mini,gpt-4o-mini
VERIFIED_TTS_VOICE_CATALOG={"cartesia":{"sonic-3.6":{"your-voice-id":["en","hi"]}},"elevenlabs":{"eleven_flash_v2_5":{"your-voice-id":["en"]}}}
```

Supported combinations in this build: STT providers Azure Speech (`azure-speech`) and Deepgram (`nova-3`), OpenAI models from the allowlist, Cartesia `sonic-3.6`, ElevenLabs `eleven_flash_v2_5`, languages `en` and `hi`. New agents default to Azure Speech (`azure-speech`) with default English recognition locale `en-IN`; Deepgram remains selectable with no runtime fallback between providers. Publishing and placing calls requires server credentials for the selected STT provider: `AZURE_SPEECH_API_KEY` plus `AZURE_SPEECH_REGION` (or optional full Speech SDK service endpoint `AZURE_SPEECH_ENDPOINT` overriding region) for Azure, or `DEEPGRAM_API_KEY` for Deepgram. Supported English recognition locales for Azure Speech are `en-IN`, `en-US`, and `en-GB`. When call language is `hi`, Azure recognition uses `hi-IN` regardless of English locale setting. A call uses one language. Publishing more than one language is rejected until live TTS language switching is verified end to end.

## Deployment notes

### HTTPS and cookies

Terminate TLS at a reverse proxy (Caddy, nginx, a cloud load balancer) and proxy to the Compose ingress on port 3000. Keep the browser on one origin so `/api/*` is same-origin and cookie/CSRF handling works. Then set:

```dotenv
ENVIRONMENT=production
APP_ORIGIN=https://voice.example.com
```

With `ENVIRONMENT=production` over plain HTTP the browser will not store the `Secure` session cookie, so local HTTP must use `ENVIRONMENT=development`.

Compose's ingress replaces caller-supplied `X-Forwarded-For` headers, and the backend trusts forwarded addresses only from the frontend's isolated backend-network address. If a TLS proxy or load balancer sits in front of the ingress, the ingress sees that proxy's address and clients share its rate-limit bucket. Configure nginx's real-IP module with only the actual trusted proxy addresses before relying on per-client limits; never trust forwarded headers from arbitrary clients. If the default backend proxy subnet conflicts with another network, set both `BACKEND_PROXY_SUBNET` and a matching `FRONTEND_PROXY_IP` in `.env`.

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
python -m app.seed
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

## Login

Passwordless User and Admin buttons are available only when `ALLOW_DEV_LOGIN=true` and `ENVIRONMENT=development`. They use `DEV_USER_EMAIL`/`DEV_USER_PASSWORD` and `ADMIN_EMAIL`/`ADMIN_PASSWORD`, which must match existing accounts with the corresponding roles; they never create accounts or promote an existing user. Browser requests must include an exact `Origin` matching `APP_ORIGIN`.

To create both accounts without registering them manually, run:

```sh
docker compose exec backend python -m app.seed
```

For a backend outside Docker, load `.env` and run `uv run python -m app.seed` from `backend/`.
Seeding requires `ENVIRONMENT=development` and uses `ADMIN_EMAIL`/`ADMIN_PASSWORD`
and `DEV_USER_EMAIL`/`DEV_USER_PASSWORD`. Emails must be different; passwords must
have at least 12 characters. The command creates missing accounts and checks
existing credentials without changing passwords, IDs, or roles. Invalid credentials,
role conflicts, or password mismatches stop the command without saving either account.
If a password differs, set the environment value to the existing account's password
and recreate the backend container with `docker compose up -d backend` before rerunning.
The command does not enable passwordless login or create agents or call history.
For production, use `python -m app.bootstrap` instead to create only the administrator.

This option grants a session without asking for a password. Keep it disabled unless developing locally. It is not safe on shared or untrusted networks—even when the configured Origin check is enabled, non-browser clients can send that header. The default Compose ingress listens on all interfaces, so a machine reachable by other people must not enable this option. Production always disables the endpoints.

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
| Call returns 503 | Missing selected STT credentials (`AZURE_SPEECH_API_KEY` and `AZURE_SPEECH_REGION`/`AZURE_SPEECH_ENDPOINT` for Azure, or `DEEPGRAM_API_KEY` for Deepgram), `OPENAI_API_KEY`, or the selected TTS key. |
| Call cannot connect across networks | Configure TURN; check firewall/UDP and the ICE server endpoint. |
| Trace page returns 503 | Phoenix is not configured/reachable, or `PHOENIX_OTLP_ENDPOINT`/`PHOENIX_URL` are wrong. |
| `pytest` cannot connect | The disposable `voice_test` database does not exist or `TEST_DATABASE_URL` is wrong. |
| Postgres already has a volume but not `voice_test` | The init script runs only on first initialization; create it manually: `docker compose exec postgres psql -U voice -d voice -c 'CREATE DATABASE voice_test;'` |

## What is not verified by automated tests

The test suite uses deterministic substitutes and does not prove live audio behavior. A real browser call with actual provider credentials is still required to verify microphone capture, Deepgram transcription, LLM extraction, TTS playback/interruption, answer persistence, completion, and cross-network connectivity. Multilingual language switching is not enabled in this build.

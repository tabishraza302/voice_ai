# Voice AI SaaS — First Release Specification

**Version:** 3.0 · **Date:** 28 October 2026 · **Status:** first-release design and implementation contracts; proposed features are not claimed implemented  
**Audience:** engineering teams, individual developers, technical reviewers, QA teams, platform operators, and AI-assisted development tools  
**Artifact policy:** This master and its linked `docs/first-release/` contracts define the current design. Earlier review reports describe earlier snapshots. The user confirmed a fresh first build with nothing shipped: legacy-data migration and the pooled account coordinator are outside this release.

> **Implementation guidance:** Read this document before making changes, then inspect the *actual repository*. Existing functionality may differ from the supplied architecture drawing. Preserve working components instead of reshaping them solely to match a suggested folder tree. Work in ordered, reviewable phases; run each phase's tests, record results, and verify acceptance criteria before proceeding. Do not present unrun tests as passing. When a library or API differs from examples, verify its actual interface, document the adjustment, and preserve the stated invariants. These instructions apply equally to manual implementation, engineering teams, and coding assistants.

## Document map

- **Part I — Product and feature contract:** scope, source-system inventory, customer UX, admin UX, agent creation, versioning and deployment.
- **Part II — Conversation Flow Engine:** step authoring, graph representation, prompts, efficient single-inference voice execution, exception cases and offline Flow QA.
- **Part III — First-release platform:** selected component boundaries, fresh schema, identity, session admission, basic wallet accounting, durable events and APIs.
- **Part IV — Build order and qualification:** exact file-action boundaries, phase dependency graph, feature flags, acceptance gates and linked executable test/fixture plan.

---

# PART I — PRODUCT CONTRACT, EXISTING SYSTEM & FRONTENDS

## I.1 Purpose and boundaries

Build a **self-hosted, multiuser browser-based Voice AI SaaS**. Each customer is an individual account owner (no organizations/workspaces). Customers create and deploy their own agents, converse through WebRTC, track conversations, and pay from prepaid wallet credit. Admins operate users, billing, model availability, defaults, workers and alerts. Support local AI models and configured external provider APIs through existing Pipecat service adapters; secrets are server-side environment variables.

**Do build for MVP:** Next.js web at development port `3001`; separated FastAPI control plane `8000`; bounded Pipecat runtime `7860` and SmallWebRTC; PostgreSQL; RabbitMQ + dedicated consumer; durable outbox; authenticated browser sessions and runner join tokens; user-owned agents with immutable versions; draft/publish; two agent-creation paths; tabbed configuration; step/graph conversation design; Chat Test, Voice Test, Flow QA; customer dashboard/conversations/wallet; administrator panel; one prepaid wallet per user with small per-call reservations; monitoring and tests.

**Deliberate non-goals for MVP:** organizations/teams/shared agents, inbound/outbound PSTN/SIP telephony, live call recording, CRM webhooks, arbitrary user-defined code execution, visual node-canvas drag/drop authoring, Kafka, Kubernetes, mandatory Redis/PgBouncer, provider-catalog microservice, secret vault, automatic monetary payouts. Hosted browser voice link is the first external deployment channel; embedding is later; rate limiting and abuse controls are required before public link launch.

**Deployment qualification:** The platform must be deployable on different compute environments without assuming a particular machine, processor architecture, memory capacity or inference host. Support local, self-hosted, and configured external AI backends as available; verify connectivity to any selected provider or payment gateway. Public SaaS access requires tested public ingress, TLS and WebRTC/STUN/TURN networking. Size each environment from measured latency, concurrent-session throughput, memory/accelerator demand and fault tolerance, not a preset hardware specification.

**First paid release:** All Phase 0–12 exit gates, including Phase 10 Flow QA, are required for the MVP described here. Phase order is a development sequence, not permission to omit a required release capability. Static validation and deterministic flow regression tests are required as soon as flow execution exists; AI-generated scenarios and suggestions arrive in Phase 10. Later features are only those explicitly listed as Post-MVP.

**Offline compatibility:** Preserve the existing `scripts/voice-ai.sh start` and local `/client` voice workflow behind a `local_offline` operating mode. It uses predownloaded local models, disables external resources with `PHOENIX_ALLOW_EXTERNAL_RESOURCES=false`, and makes no runtime calls to external model APIs, payment services, public STUN/TURN, remote fonts, or other internet resources. Public hosted links and real customer billing are disabled in this mode. Keep existing local management routes bound to loopback until their authenticated replacement is qualified. SaaS modes explicitly declare their network dependencies and cannot be described as air-gapped. These mode contracts are proposed preservation requirements; a new mode selector is not claimed to exist today.

Preserve the backend registries, language-router protocols and backend-free pipeline boundary. Existing `METRIC_NAMES` and bounded label sets remain contracts; put session/user/call identifiers in permitted trace or event records, never Prometheus labels. Local latency/memory numbers describe their measured environment only. New services require configured ports checked against host processes and container memory limits sized in the target environment. Phoenix remains host-run in the existing local profile.


**Fresh-install boundary:** Nothing has shipped. The new control plane creates independent models in a dedicated `platform` schema of the PostgreSQL database the local agent already uses. The agent keeps `public`, the control plane owns `platform`, and Phoenix telemetry owns `phoenix`; there is no second runtime database and no `PLATFORM_DATABASE_URL`. It does not backfill owners, convert presets/history, alter old `public` records or run Alembic cutovers. Local data is neither production data nor a release blocker. Initial schema creation, idempotent setup and backup/restore are still tested. No task may reset, drop or repurpose the existing `public` schema or its records to make the new platform work.

**Concrete supporting contracts:** The [auth contract](docs/first-release/auth-contract.md), [flow contract](docs/first-release/flow-contract.md), executable [flow schema](docs/first-release/flow.schema.json), [authoring schema](docs/first-release/flow-authoring.schema.json), [accounting contract](docs/first-release/accounting-contract.md), [verification plan](docs/first-release/verification-plan.md), and [implementation plan](docs/first-release/implementation-plan.md) supply exact types, routes, files and gates. Part I/II keep product intent and examples; the linked versioned schemas determine executable wire structure. No reviewer is entitled to treat a proposed new file as an existing feature. These documents specify implementation; they do not assert that code or qualification results already exist.

## I.2 Existing code and infrastructure indicated by the uploaded architecture

The user-supplied architecture diagram (not an executed repository audit) indicates:

| Existing / shown | Details / preservation rule |
|---|---|
| Next.js Studio `:3001` | Sidebar currently **Agents, Base Prompt, Playground**; agents CRUD, clone, activation, voice-test modal; frontend `ApiClient`, `VoiceSession`, RTVI transcript protocol. Reuse existing components, remove customer Playground route/navigation only after integrated testing works. |
| Combined `bot/bot.py` `:7860` | FastAPI agent/prompt/catalog routes and Pipecat WebRTC/WS eval runtime in one process. Separate control-plane authorization and CRUD into a new `:8000` service incrementally; keep runtime operational. |
| Agent data | Fields include persona prompt, LLM model/temp/reasoning/max tokens, STT provider, English/Hindi TTS voices/speed, active/preset flags. Reuse these profile fields for new owner-scoped drafts and versions; existing local records stay in the local workflow. |
| Prompt features | Base prompt, revisions, preview, `bot/agents/compiler.py` (`base_prompt` + `persona_prompt`, XML-escaped). Keep compiler behavior; add structured conversation-flow compilation and safe admin permissions. |
| Voice engine | `bot/context.py` context repair, `bot/pipeline.py` sentence splitting/language routing/first-sentence deadline, Pipecat STT→LLM→TTS, assistant context after audible speech, interruption handling. Preserve, instrument and regression-test. |
| Optional local backends | Existing adapters may include Ollama-hosted LLMs, MLX-based STT and Kokoro TTS, along with alternatives and English/Hindi options. These are examples from the supplied system, not required models, frameworks or deployment hardware. Keep provider registration in code and validate actual supported capabilities. |
| Data | One PostgreSQL runtime database on host `:5439` (container `:5432`), selected by `DATABASE_URL`; PostgreSQL is the only runtime database, with no SQLite fallback. The agent's tables (agents, base_prompts, prompt_revisions, voice_sessions, session_turns) stay in `public`, the platform control plane uses `platform`, and Phoenix telemetry uses `phoenix`. The same PostgreSQL database is the sole production financial DB. |
| Observability and evals | OTel, Phoenix, Prometheus/Grafana; TTFB/TTFA/TTFAT, usage, WER/CER, turn latency, quantization and memory scripts. Reuse; extend with SaaS and billing signals. |
| Deployment | Existing architecture shows a mixed host-process/container arrangement and fixed-address internal networking. Treat these as source-system observations only, not architecture requirements. Use configurable service endpoints and deployment profiles without hardcoded host or container addresses. |

**Known from the diagram ≠ verified implemented.** Phase 0 must check code, routes, fresh-schema setup, available credentials, running WebRTC session and actual unit tests. Make a capability matrix: `present/runs`, `present/untested`, `partial`, `absent`; record evidence and file paths. Do not estimate percent completed from a diagram alone.

## I.3 Non-negotiable behavior and invariants

1. Customers can access **only** their own agent, configs, calls, transcripts, testing sessions, wallet and usage. All endpoint queries scope by verified user identity.
2. An agent has a mutable **draft** and immutable **published versions**. Live sessions pin a specific version; historical config is never rewritten by editing the agent.
3. The platform prompt is admin-owned and versioned. A customer edits **their** agent-level instructions and business knowledge; cannot edit platform-wide safety/admin controls.
4. Text Chat Test and Voice Test run the **same flow logic/config version**. Voice Test adds STT/TTS/WebRTC; Chat Test doesn't require a voice-concurrency slot.
5. Customer wallet billing is independent of wholesale provider cost. All available-credit decisions happen synchronously through the transactional wallet/reservation authorization path, never from eventual RabbitMQ projections.
6. A valid budget authorization and user/runner capacity reservation are required before paid audio conversation begins. All financial operations are idempotent.
7. Concurrent calls share their owner's wallet through PostgreSQL row-locked reservations. Each session has one assigned runner and one budget; cumulative spending cannot exceed funded money. There are no per-user coordinator epochs in v1.
8. Only proven ACTIVE time is charged. On a lost runner, stop/expire its permission, settle durable checkpoints and release the remainder; unproven time is waived rather than guessed or left in an indefinite reconciliation queue.
9. Event publication/consumption is at-least-once with producer durability and consumer dedupe, not a claim of magical exactly-once transport.
10. The LLM never decides authoritative business graph transitions or executes side-effecting business operations without application validation.
11. Flow QA and generation jobs are **off the live-call hot path** and resource-limited to protect voice latency.
12. No credentials, hidden administrator routes, internal grants or vendor API secrets are made visible to normal users.

## I.4 Customer information architecture

```text
Customer Studio
├── Dashboard                 account overview, balance, active calls, agent summary, recent activity
├── Agents
│   ├── All Agents            search/filter/sort, clone, enable/disable, create
│   ├── Create Agent          Generate from Prompt | Manual
│   └── Agent Workspace
│       ├── Overview          name, description, status, version, deployment
│       ├── Instructions      welcome, persona/objective, business knowledge
│       ├── Conversation Flow Step Editor | Flow View
│       ├── LLM               provider/model, temperature, reasoning, token limits
│       ├── STT               model, language, turn detection, mode
│       ├── TTS               provider, EN/HI voices, speed, pronunciation
│       ├── Advanced          approved tools, interruption/timeouts, limits
│       ├── Test              Chat Test | Voice Test | Flow QA
│       ├── Deploy            Publish, hosted browser link; widget later
│       └── Versions          history, compare, restore-to-new-draft
├── Conversations
│   ├── Active Calls
│   ├── Call History          searchable/filterable, paginated
│   └── Call Details         transcript, usage, charge, pinned config, technical status
├── Billing                   available/reserved/posted, credits, tariff, charges, transactions
└── Settings                  profile, notifications, data/retention, preferences
```

**Remove the standalone customer Playground.** The old `/playground`, `/client` and `VoiceTestModal` may be reused internally/redirected to an agent's workspace test panel; do not break existing voice transport. Put system-wide Base Prompt and STT mode controls under `/admin`. Do not confuse customer `Conversations` with internal evaluation simulations.

### Dashboard contract

- Authenticated header: account/profile and spendable wallet balance.
- Summary cards: spendable credit, reserved credit (explained), active calls, owned agents, recent total usage/charges.
- Show **top five most recently used agents** as an initial deterministic policy (agent name, status, short description, Configure, Test); `View all` opens `/agents`. If no calls exist, rank by recently updated instead.
- Recent calls (status, agent, duration, customer charge) and attention items (low credit, failed calls, unpublished changes).
- Shared `Create Agent` click-accessible dropdown **Generate from Prompt** / **Create Manually**, usable with keyboard/touch; no hover-only behavior.
- New-user empty state is a progression: create → configure → Chat Test → Voice Test → publish → deploy.
- Display loading/empty/error states, stale-data timestamps for live activity and currency/unit labels.

### Agents list contract

Search owned agents by name/description, filter status, sort recent activity/name, paginate. Each entry: name, short purpose, published/draft status, voice model summary, last used/modified, Configure, Test; menu: Duplicate (new owned draft), Enable/Disable, Delete (with typed/confirmed destructive action as appropriate). No globally active agent semantics in multi-user SaaS. `Enable` controls acceptance of new sessions; doesn't implicitly edit ongoing calls.

### Agent creation contracts

**Generate from Prompt:** user enters plain-language business requirement, selects **generation LLM** from configured/supported models, chooses language optionally, clicks Generate; backend validates budget/rate/ownership, creates a *proposal/draft*, not immediately a live agent. Generation LLM is separate from the resulting agent's runtime LLM. Generate the full structured agent schema: name, purpose, persona, welcome, knowledge gaps, step graph, captures/conditions/defaults, LLM/STT/TTS suggestions. Validate schema/graph; mark unknown business facts as needing input; never invent product prices/eligibility. User reviews then saves/publishes.

**Manual:** create draft with a centrally defined **admin-versioned default agent template**, copied at creation. Default provider/model/voice/temp/VAD/timeout; basic welcome/identity/objective; initially minimal/empty flow. Future template edits affect **new drafts only**, not existing versions. Both creation methods converge to same agent schema/editor.

### Agent Workspace layout

- Desktop: editor panel and right-hand integrated **Test Agent** panel. Mobile: test panel becomes tabs/drawer.
- Agent header: name, enabled/disabled, Draft changes / Published vN status, saved timestamp, Validate, Publish, Test, Version History.
- Tabs: **Instructions**, **Conversation Flow**, **LLM**, **STT**, **TTS**, **Advanced**, **Test**, **Deploy**, **Versions**. `Flow QA` lives inside Test, and is also reachable from the Flow Editor.
- **Instructions**: (1) welcome message, exact greeting spoken first after bot READY, cancellable by barge-in, included in transcript; (2) overall objectives, persona, business instructions/knowledge and global guardrails. The detailed multi-turn business script lives in Conversation Flow rather than being buried in one text area.
- Basic controls visible first, technical controls under Advanced; inline validation, accessibility, autosave with explicit saved/draft state and unsaved-change navigation warning.
- **Draft vs Published:** update draft, validate, run tests against draft, publish immutable config with changelog; new production sessions use latest published version. Tests select Draft/Published explicitly. Existing sessions continue pinned old version. Restore copies old version to a new draft; no historical mutation.

### Draft validation levels

- **Save:** Persist structurally valid authoring JSON, including an empty flow, incomplete step fields, missing transition targets and other temporarily invalid graph relationships. Reject malformed JSON, unsupported schema versions, wrong field types, unauthorized fields, secrets and configured size-limit violations with `422 VALIDATION`; retain the client's unsaved content and leave the stored revision unchanged. A successful autosave returns `200` with `draft_revision`, `authoring_hash`, and `validation={status, errors, warnings}`. Graph errors do not turn a successful save into an execution-ready draft. Expected-revision conflicts return `409` without changes.
- **Validate/test:** Full graph, provider and capability validation is required before compiling an executable draft snapshot. A test request with blocking errors returns `422 FLOW_INVALID` with field/node paths and creates no test session. Valid tests pin a saved draft revision, source hash, compiled hash and compiler version for their entire lifetime.
- **Publish:** Apply the same executable validation plus publication/availability policy checks in the immutable-version transaction. Blocking errors prevent publication. Warnings require an explicit acknowledgment list tied to the expected draft revision. A save hash describes canonical authoring JSON; a compiled hash exists only for an executable snapshot. Client edits and invalid saves never replace a running test's pinned snapshot.

### Agent deployment lifecycle

Publication progresses from editable draft to a validated snapshot and immutable published version. The agent's independent `enabled` flag controls new admissions; an enabled agent still requires a published version and a live deployment. Editing a draft does not change either the published version or enablement. First channel: **hosted voice page** with an unpredictable/opaque public agent link or permitted signed invite policy and per-agent abuse limits. Anonymous/public participant is not an account owner; call charges the **agent owner's wallet**. Public sessions have stronger CAPTCHA/rate/concurrency/abuse protections and no owner transcript visibility to anonymous participants. Disabling/deleting revokes future joins, subject to explicit in-flight session policy. Never embed admin/owner JWTs in public links. Embeddable website widget and scoped external API integration are post-MVP extensions.

### Testing UX

- **Chat Test:** text conversation with actual agent runtime LLM, same prompt compiler/flow controller; no STT/TTS; choose draft/published, reset conversation, show agent reply, active step, captured fields, transition trace and error states. Text test is metered according to a **published test tariff** (possibly free/limited for MVP), never silently charged.
- **Voice Test:** integrated reuse of current WebRTC/RTVI and Pipecat; bot READY, speaking/listening/processing, mic permission, reconnect, interruptions, latency diagnostics, transcript. Per-second voice-test charges must be disclosed before start and be distinguishable from live deployment traffic.
- **Flow QA:** Generate Scenarios, review checklist, Run selected, Results/Coverage, AI Suggestions. Expensive offline jobs must have budget/concurrency limits; run separate from latency-sensitive work.

### Conversations and history

- `Active`: session status, agent, connection state, running billable duration, *estimated* charge, owning runner; owner can request graceful end.
- `History`: filters user-owned agent/date/status/test vs deployed, pagination and search (subject to privacy/index decisions).
- `Details`: transcript with ordered finalized segments/welcome/corrections, start/end/billable duration, pricing snapshot, charge and `pending|settled|waived_unproven`, pinned full agent config, version ID/hash, optional failure code and trace link. Include transcript export/delete under policy.
- Past transcript/config must not reflect current edits. Reconnect intervals remain one session unless an explicit new session is created.

### Billing and settings UX

- Wallet clearly distinguishes `posted balance` (settled funds), `reserved` (authorized in-flight/pending), `available` (posted minus reserved); reservations are not money already charged. Show customer tariff/version, billable seconds, per-session charge, top-ups/adjustments/refunds and immutable transaction records. Provider wholesale costs are **not** shown as customer charges.
- Payment gateway integration is optional until a gateway is selected and verified. Test-only credit issuance can be used in nonproduction environments; real top-ups require server-side payment verification, idempotent webhook handling, and reconciliation before enabling the payment UI.
- Settings: profile/security, preferred language/timezone, low-balance notifications, transcript export/deletion/retention explanations, logout/session management. No customer-facing provider-secret or API-key forms; credentials are managed only through protected admin tools.
- Responsive layouts, focus indicators, labels, keyboard activation, adequate contrast, no inaccessible hover-only menus, proper `aria-live` for connection status, and clear disabled/error states.

## I.5 Admin information architecture and roles

```text
/admin  (backend-verified admin role; not a separate frontend deployable)
├── Overview            totals, active calls, funds/usage, service health, alerts
├── Users               search, inspect account, suspend/restore, limit changes
├── Agents              global inventory, owner, version, status, restrict
├── Conversations       active, failures, historical sessions, budget closure
├── Billing             ledger, top-ups, reservations, pricing versions, final charges
├── AI Configuration    approved models, default template, base prompt, STT mode
├── System              runner capacity, model endpoints, DB, MQ, events, metrics
├── Audit Logs          privileged changes and reasons
└── Admin Settings      own profile, MFA/security, platform preferences
```

**Roles for MVP:** `user` and `admin`; admin account provisioned by protected bootstrap CLI, not self-chosen via signup. Backend authorization on every `/admin/*` request and admin mutation. Ordinary user API must not expose global data simply by changing `user_id` or hiding navigation.

| Admin area | Query/controls | Boundaries |
|---|---|---|
| Overview | user count, agents, active/failed calls, usage, customer top-ups, **settled revenue distinct from cash received**, unclosed reservations, health | Metrics time range and staleness label; no fabricated real-time numbers |
| User management | find/view user, owned agents and sessions (metadata), current wallet/holds, concurrency limits, suspend admissions, unsuspend | No impersonation by default; never expose credentials; suspend of active calls separate confirmed action |
| Credit adjustments | issue manual credit/debit/refund via ledger with admin ID, reason and idempotency | Never `UPDATE balance` blindly; require confirmation, audit, permissions and bounds |
| Global agent inventory | search agents, inspect current/pinned config where authorized, disable abusive agents, failure trends | Customer prompt edits read-only by default; justified support overrides if ever introduced |
| Conversations | active sessions/status/runner/estimated charges; session termination; failed calls; inspect budget closure | Transcript content restricted to explicit privileged support reason; metadata only by default |
| Billing | posted/reserved/available balance, per-call reservations, checkpoint age, ledger, customer rates and expiry cleanup | All wallet changes use the guarded billing service; no direct balance overwrite |
| Pricing | versioned customer tariff and defaults for *new* sessions | Do not reprice pinned active/historical sessions |
| AI configuration | model availability from code registry, default agent template, platform base prompt revisions/preview, runtime mode/defaults | Admin-managed provider credentials are encrypted at rest with a dedicated deployment key; show configured/missing only, never secret values |
| System | FastAPI/Pipecat health, local model status/memory, DB pool, broker depth/DLQ, spool, Phoenix/Grafana links, alerts | Restarts, schema initialization and backups remain operator CLI in MVP |
| Emergency controls | pause new sessions globally/per user/agent; allow in-flight completion; separately request ending | All actions auditable; release reservations only after the session is stopped or its last permit expired |
| Audit | actor, action, target, before/after redacted diff, reason, timestamp, IP/correlation, outcome | Immutable or append-only and access restricted; no tokens/PII in audit details |

**Admin security:** protect admin accounts with MFA before production and step-up authorization for money/rate/suspension and provider-credential changes. Logs of privileged transcript access should include purpose. Initial 2 roles can coexist with policy-specific authorization checks; no organizations or RBAC designer required.

## I.6 Cross-cutting UX state contracts

| Scenario | Customer presentation | Admin/ops signal |
|---|---|---|
| WebRTC connected but bot initializing | "Preparing agent…", no claim bot is ready | initialization latency |
| Browser microphone blocked | permission help + retry | `MIC_PERMISSION` client-only diagnostic |
| User insufficient spendable credit | pricing/balance explanation, add-credit guidance | `INSUFFICIENT_CREDIT` admission reject |
| Active wallet authorization nearly depleted | honest low-balance warning when funds remain for notice | reservation renewal/denial counts |
| Session ends due to credit | "Call ended — credit exhausted"; history charge pending/settled | reason `CREDIT_EXHAUSTED` |
| Runner full | "Agents are busy, retry shortly" | capacity rejection |
| Broker delayed | history "transcript processing"; call need not necessarily end if durable outbox healthy | queue depth, spool age |
| Bot reconnect | reconnect countdown + pause billing if policy | interval transition and grace |
| Session closes after a lost response | distinguish estimated/pending from final charge; retry the same operation | budget-close retry age; expire stopped permission before releasing money |
| Agent edited while call active | ongoing conversation unchanged; new published version used for new calls | config version pins |

---

# PART II — CONVERSATION FLOW, PROMPT COMPILER & OFFLINE QA

## II.1 One canonical graph, three user experiences

**Step Editor, Flow View and Flow QA operate on the same canonical `conversation_flow` JSON.** Step Editor edits typed nodes and transitions; Flow View derives nodes/edges/labels from those records; QA generates coverage targets and simulated scenarios from the graph. Never maintain a separate manually edited diagram that diverges from execution.

Recommended authoring scope:

- **Agent global instructions**: objective, persona, safety/legal/business rules, approved knowledge, tone, allowed tools, completion definition.
- **Welcome message**: exact greeting text emitted through TTS once bot is ready (and optionally shown as first assistant text for Chat Test). If greeting already asks a question, mark `question_step_id` so the next user utterance can satisfy that step; avoid asking twice.
- **Flow steps**: stable `step_id`, title, type `ask|collect|inform|action|summary|end`, natural-language `instruction`, `priority`, `required`, expected typed captures, prerequisites, transitions, fallback and retry policy, exit conditions.
- **Behavior**: `ordering=adaptive|strict`, `allow_relevant_detours`, `capture_early_information`, `allow_corrections`, `clarification_attempts`, `allow_opt_out`; user cannot opt out of essential privacy and safety constraints.
- **Knowledge**: admin/user supplied authoritative facts, FAQs; absence of product details must be treated as missing, not invented.

### Example Step Editor (customer-defined)

**Agent:** Credit Card Qualification  
**Welcome:** "Hi! I can help you explore credit card options. Are you salaried or self-employed?"  
**Goal:** determine employment category, relevant approximate income and benefit preference; answer questions; never guarantee eligibility.

| Step ID/title | Step instruction | Capture/validation | Condition / next | Fallback |
|---|---|---|---|---|
| `employment` — Employment | Ask salaried or self-employed | `employment_type`: `salaried\|self_employed` | salaried→`salary`; self-employed→`business_income` | clarify twice, then offer to skip/close per user policy |
| `salary` — Salary | Ask approximate monthly income if salaried | `monthly_income_inr`: positive integer, approximate allowed | if valid→`preferences` | clarify currency/amount; refusal branch |
| `business_income` — Business income | Ask approximate annual income if self-employed | `annual_income_inr`: positive integer | if valid→`preferences` | clarify; refusal branch |
| `preferences` — Card interests | Ask travel/cashback/shopping benefits | `card_preference`: enum | if valid→`summary` | optionally free-text follow-up |
| `summary` — Wrap up | Summarize only validated facts, explain next step | no new required field | when required fields complete→`end` | incomplete path if refusal |
| `end` — Finish | Close politely, do not claim approval | terminal | none | n/a |

**Step Editor rule UI:** each transition row offers [Field] [Operator] [Value] → [Next step], plus explicit ELSE/fallback. No executable expression strings, no arbitrary Python/JS. `required` means required for the normal successful path, not permission to ignore a user's refusal or desire to end.

**Flow View rendering:** nodes=`steps`, edges=`transitions`; edge labels are normalized conditions (e.g. `employment_type = salaried`), prerequisites are shown as node constraints, fallback as dashed/secondary edges. If multiple independent steps are eligible, show preferred order and adaptive-step indication. Graph rendering is UI-only; server validates and executes its underlying typed graph. Begin with read-only graph + click-node-to-edit; drag/drop editing is not needed for MVP.

### Canonical schema specimen

```json
{
  "schema_version": 1,
  "entry_step_id": "employment",
  "behavior": {
    "ordering": "adaptive",
    "allow_relevant_detours": true,
    "capture_early_information": true,
    "allow_corrections": true,
    "clarification_attempts": 2
  },
  "steps": [
    {
      "id": "employment", "title": "Employment Type", "type": "collect",
      "instruction": "Determine whether salaried or self-employed.",
      "required": true, "priority": 10, "prerequisites": [],
      "captures": [{"field": "employment_type", "kind": "enum", "values": ["salaried", "self_employed"]}],
      "transitions": [
        {"when": {"field": "employment_type", "operator": "eq", "value": "salaried"}, "to": "salary"},
        {"when": {"field": "employment_type", "operator": "eq", "value": "self_employed"}, "to": "business_income"}
      ],
      "fallback": {"action": "clarify", "max_attempts": 2, "on_exhausted": "offer_exit"}
    },
    {
      "id": "salary", "title": "Monthly Salary", "type": "collect",
      "instruction": "Ask for approximate monthly income.", "required": true, "priority": 20,
      "prerequisites": [{"field": "employment_type", "operator": "eq", "value": "salaried"}],
      "captures": [{"field": "monthly_income_inr", "kind": "positive_integer"}],
      "transitions": [{"when": {"field": "monthly_income_inr", "operator": "is_present"}, "to": "preferences"}],
      "fallback": {"action": "clarify", "max_attempts": 2, "on_exhausted": "offer_exit"}
    },
    {
      "id": "business_income", "title": "Annual Business Income", "type": "collect",
      "instruction": "Ask for approximate annual income.", "required": true, "priority": 20,
      "prerequisites": [{"field": "employment_type", "operator": "eq", "value": "self_employed"}],
      "captures": [{"field": "annual_income_inr", "kind": "positive_integer"}],
      "transitions": [{"when": {"field": "annual_income_inr", "operator": "is_present"}, "to": "preferences"}],
      "fallback": {"action": "clarify", "max_attempts": 2, "on_exhausted": "offer_exit"}
    },
    {
      "id": "preferences", "title": "Card Preferences", "type": "collect",
      "instruction": "Ask what benefits interest them: travel, cashback, or shopping.",
      "required": true, "priority": 30, "prerequisites": [],
      "captures": [{"field": "card_preference", "kind": "enum", "values": ["travel", "cashback", "shopping"]}],
      "transitions": [{"when": {"field": "card_preference", "operator": "is_present"}, "to": "summary"}],
      "fallback": {"action": "clarify", "max_attempts": 2, "on_exhausted": "offer_exit"}
    },
    {
      "id": "summary", "title": "Summary", "type": "summary",
      "instruction": "Summarize known values and explicitly avoid approval guarantees.",
      "required": true, "priority": 90,
      "prerequisites": [{"operator": "all_applicable_required_fields_present"}],
      "transitions": [{"when": {"operator": "completed"}, "to": "end"}]
    },
    {"id": "end", "title": "End", "type": "end", "instruction": "Close politely.",
      "required": false, "priority": 100, "prerequisites": [], "transitions": []}
  ]
}
```

This is **an illustrative wire representation**, not a ready-made drop-in Pydantic schema. Define discriminated condition unions and explicit null/unknown/refused field states, validate transition targets, acyclic/allowed-loop rules, unreachable nodes, empty branches, impossible prerequisites, and mandatory end/exit policy. Do not evaluate conditions with `eval` or generate executable code from customer-entered prompts.

## II.2 Adaptive vs strict ordering and detour policy

- **Preferred order** is not a hard dependency. The engine chooses an eligible incomplete step based on allowed transitions, prerequisites, priority and progress. Adaptive mode may reorder independent steps. Strict mode preserves planned ask order where possible but can still record early user-supplied data and respect safety/exit requests.
- **Explicit dependencies** are enforced. Asking which income amount to collect requires employment; preferences can be answered earlier. Multiple fields in one utterance can complete multiple steps after validation.
- **Detour**: when user asks a relevant FAQ mid-step, answer from permitted knowledge, then return to the unfinished objective. Do not hallucinate missing price/fee data. An out-of-scope question can be declined or redirected politely.
- **Correction**: new employment category invalidates or flags dependent income; preserve unrelated preferences; re-evaluate affected downstream steps. Record provenance/turn for changes and avoid silently overwriting confirmed values.
- **Refusal/opt-out/end**: respect refusal, use explicit partial/aborted outcome. "Required" doesn't force harassment or continuation against user choice.
- **Ambiguity**: contradictory values, unclear currency, low-confidence STT transcriptions → ask clarifying question; do not claim field completion.
- **Business consequences** (eligibility decision, ordering, payment, irreversible tool calls): always validate deterministically and/or require confirmation before action; model prose is never authoritative approval.

### Deterministic routing and completion

Outgoing transition rows are ordered alternatives. Evaluate them in stored array order and select the **first true condition**; overlapping conditions are permitted with first-match semantics and a validator warning. The editor displays and allows reordering that precedence. Unknown/refused values never satisfy comparisons other than their explicit state predicates. Evaluate ELSE only when every explicit condition is false, never when one is unknown. If no explicit guard is true and one is unknown, clarify; an earlier unknown does not suppress a later true guard. If the selected target's prerequisites are unmet, do not try a later edge or ELSE: route to the earliest eligible missing prerequisite, or return `FLOW_BLOCKED` if none exists. Cycles without a finite retry/exit rule fail executable validation. Node priority orders independent eligible incomplete objectives by ascending `(priority, step_id)`; it does not override edge precedence. Strict mode uses authored step-array order among those same eligible objectives.

Completion is owned by the controller and is distinct from having prepared a question. `ask` and `collect` require at least one typed capture; they complete when all their required applicable captures are validated, including early user answers. `inform` and `summary` complete after all required fragments for that step are confirmed as played by the media transport; interruption leaves the unplayed part incomplete. No user acknowledgment is implied: model an acknowledgment as an explicit capture step. Chat Test uses committed assistant-message delivery as the corresponding completion event. `end` plays its closing fragments, then requests idempotent teardown; explicit user stop, exhausted credit or safety shutdown may end immediately without finishing the closing message. `action` completes only on a validated, recorded result from an allowlisted operation with a stable execution ID; each tool must supply its input/result schema, confirmation policy, idempotency and failure/retry contract before that step may publish. Missing tool contracts block publication.

Track generated, queued and confirmed-played fragments separately by session, turn, generation and step IDs. A playback completion event must be bound to the current transport/generation and required fragment set. It cannot authorize a financial operation or business action by itself. Phase 4 must verify this event contract against the selected transport; if reliable completion cannot be observed, that transport/flow combination is not executable until a documented safe completion protocol exists. On interruption preserve completed captures and confirmed fragments, discard queued speech, and resume the incomplete objective without replaying completed material by default.

## II.3 Flow compilation and prompts actually sent to the LLM

**At publish time**, compute versioned artifacts with hashes: normalized/validated flow graph; global/policy system prompt; compact index of all steps/goals/prerequisites; step-detail prompt fragments; knowledge references; tool schema; evaluator rubric. Recompile on version change, **not per audio frame**. Preserve original source JSON in immutable config snapshot.

**At runtime each turn**, build context in memory: global system prompt + compact whole-flow overview + current validated conversation state + current/relevant step fragments + available business knowledge + conversation history + latest user text. For small flows, including all step details is acceptable; for larger flows, include compact graph plus relevant fragments to avoid unnecessary context tokens. No DB round-trip or additional LLM inference is required simply to construct this text.

Example **static compiled system section**:

```text
ROLE: You are a conversational credit-card qualification assistant.
OBJECTIVE: Collect employment type, applicable approximate income and card preferences.
BUSINESS RULES: Never guarantee approval. Never invent annual fees or product facts.
GENERAL BEHAVIOR: Answer relevant questions, accept out-of-order answers and corrections,
respect refusals and explicit termination, avoid repeating validated questions.
FLOW SUMMARY:
  employment → salary (if salaried) | business_income (if self-employed)
  applicable income → preferences → summary → end
IMPORTANT: The application controller owns authoritative field/step state.
Only verified information is considered collected; do not imply an approval decision.
```

Example **turn context**:

```text
CURRENT VALIDATED STATE:
  employment_type = salaried
  monthly_income_inr = 80000
  card_preference = unknown
COMPLETED: employment, salary
NEXT ELIGIBLE OBJECTIVE: collect card_preference
USER QUESTION: "What is the annual fee?"
KNOWN PRODUCT DATA: annual fee not supplied
REPLY BEHAVIOR: Acknowledge question, do not invent amount, then ask preferences naturally.
```

**Important prompt-injection boundary:** Customer agent scripts/business knowledge are configuration/data subject to the platform's policy and tool sandbox, not higher-trust system instructions. End-user speech/transcript and fetched FAQ text are untrusted inputs. Enforce constraints in code for permissions, payment and tool execution.

## II.4 Optimize real-time inference without discarding structured control

The design we chose is **one flow engine** with **one streaming LLM inference as the normal voice-turn target**. Do not add a mandatory second pre-response extraction LLM call for every utterance. However, don't promise that all current Ollama/local/provider models support streaming natural speech and a reliably parseable state delta in one request. Test with the pinned model/adapter and define an explicit capability/fallback matrix.

```text
Browser mic → Pipecat STT → Context Builder → ONE LLM request → speech chunks → TTS → speaker
                                   |                    |
                            validated state       optional proposed field updates
                                   ^                    |
                                   +-- Python validator / flow controller
                                        (in-memory, serialized per session)
```

**Practical supported implementations (choose by validated model capability):**

1. **Preferred, if adapter supports it reliably:** single model generation with separate machine-readable tool/metadata event and streamable response speech. Extract metadata through an explicit schema/stream parser; no meta tokens sent to TTS. Validate proposed fields and transitions in Python.
2. **Conservative single-pass fallback:** normal streamed text response to TTS, with no claim of authoritative structured capture from arbitrary prose. Use explicit tool calls when required, cheap deterministic extraction for easily parsable fields, and mark unverified fields `unknown/pending_confirmation`. Next turn may ask for clarification. Do not falsely mark a step complete solely because text sounds plausible.
3. **Selective strict path:** when a consequential action/ambiguous branch must be validated **before** speaking, gate speech until structured extraction/validation succeeds. Allow an extra model call only for these cases and benchmark its cost. This is a targeted safety/correctness exception, not the default two-LLM pipeline.

**Do not use naïve JSON blobs as streaming TTS text.** Standard structured JSON generation may delay useful audible text until a complete object is emitted; separate tool/channel support is not uniform. Prototype adapter tests measuring time-to-first-audio and state validity before committing to a protocol. Once TTS has spoken a statement it cannot be "unspoken"; never stream conditional guarantees or consequential next-step statements before validating the governing condition.

**Turn orchestration:** per-session serialized logical turn; `turn_id` and `generation_id`; user barge-in cancels in-flight LLM/TTS and queued audio; ignore late state deltas from cancelled generations; only include actually spoken assistant text in future history. Use bounded conversation context, not unlimited transcript injection. The graph and state are cached per session; no DB connection held during audio.

**Capture commit and cancellation boundary:** Accept each user turn under the session lock, record its text once, advance `last_accepted_turn_id`, cancel the previous generation and allocate a new generation/state version. A complete structured capture proposal may commit as soon as deterministic validation succeeds while that generation is current; record its source turn and advance the validated-state version atomically. Barge-in does not roll back facts already committed from user input. It invalidates only uncommitted proposals and later output from the cancelled generation. Facts not committed before cancellation remain unconfirmed; retain their source user text in bounded history for the next turn rather than inventing a value. The next turn's context uses the latest committed state. Commit any dependency invalidations together with the correction. Record confirmed spoken fragments independently, including those played before cancellation; never append generated-but-unplayed text. These rules apply identically to chat and voice.

**Out-of-order example:** user says "I'm salaried, earn ₹80,000 monthly, and want travel rewards" while at `employment`. A single model generation can naturally acknowledge all three; proposed fields must be validated (`employment_type=salaried`, `monthly_income_inr=80000`, `card_preference=travel`). Application marks eligible captures complete and transitions to summary. If parsing fails, keep unknown fields instead of pretending they are captured.

**Correction example:** later "Actually I'm self-employed" → validate employment correction; `monthly_income_inr` becomes inapplicable/unconfirmed, `annual_income_inr` required; leave `card_preference` intact. Select business income step. Explain correction and ask appropriate question in next grounded response.

**Detour example:** ask annual fee during income collection → answer only if fee exists in business knowledge, then resume missing income; else admit missing fee data. No mandatory jump to a new graph node for every FAQ.

### Expected latency and capacity policy

- Preserve existing Pipecat STT → LLM → language routing → TTS stream and barge-in mechanics.
- Measure (do not guess) STT finalize latency, LLM TTFT/tokens/s, first sentence aggregation, TTS TTFA and user-end-of-speech → first audible sample, p50/p95/p99 across concurrency.
- Precompile config at publish, cache active graph, avoid per-second DB writes or per-turn graph regeneration, bound token budget, avoid unnecessary language-model evaluators in voice path, and gate or queue Flow QA when live-call compute is under pressure.
- The selected deployment environment determines realistic capacity; record measured session concurrency, inference throughput and memory high-water marks instead of assuming capacity from a particular hardware profile.

## II.5 Flow QA — scenario generation, evaluation and improvements

**Offline/on-demand only.** User chooses Draft/Published vN, the number of scenarios, models and a budget. Jobs enter an independent bounded evaluation queue/worker and may pause when the runtime is resource constrained. A separate test runner may reuse the flow controller and LLM adapter without WebRTC and without STT/TTS (unless explicitly running voice benchmark suites).

**Pipeline:**

1. **Static validation:** unknown step/edge, missing else/fallback, cycles without exit/retry cap, unsatisfiable prerequisites, missing capture types, dangerous tool actions, orphan step, impossible completion. Block publish for errors; warnings can be acknowledged.
2. **Deterministic scenario derivation:** normal paths for each transition, refusals/clarification exits, out-of-order fields, multi-answer utterances, mid-call corrections/dependency invalidations, partial info, detours, early termination, contradictions, missing business facts, malicious instruction attempts; pairwise variation where exhaustive cross-product is too large.
3. **LLM scenario expansion:** generate diverse natural customer utterances from each graph case. Keep the underlying assertions fixed; do not let scenario generator determine its own grade. Validate generated inputs and reject scenarios that violate their assigned scenario specification.
4. **Simulation:** simulated customer ↔ real agent flow controller, using chosen pinned model and max-turn/cost/time budgets. Store the transcripts, structured state trace, visited edges, versions, and artifacts.
5. **Deterministic assertions:** expected visited/skipped edges, fields and provenance, prerequisite enforcement, no repeated captured questions, no invented completion, correct correction invalidation, proper refusal/termination. Prefer these to subjective scores.
6. **Optional LLM judge:** rubric for clarity, coherence, handling detours, unsupported claims, politeness and script adherence; cite specific utterances. Judgments are fallible and cannot override deterministic policy failures.
7. **Report:** scenario pass/fail, branch coverage, failure evidence, regression deltas, prompt/flow suggestions **targeted to a node or condition**. User can Apply-to-Draft, Edit or Dismiss; never auto-publish.
8. **Retest:** keep frozen regression scenarios and evaluator/rubric version, then run additional newly generated scenarios. No "improvement" claim from comparing unrelated random scenario sets.

QA result statuses `QUEUED|RUNNING|COMPLETED|FAILED|CANCELLED|EXPIRED`; separate `scenario_status=PASS|FAIL|INCONCLUSIVE|ERROR`. Persist budgets, runtime and token usage, generator/judge model versions, config hash, rubric version and execution artifacts. Treat runtime errors as test infrastructure failures, not script-performance failures. Use per-user and global quotas to prevent compute-resource starvation or runaway credit use.

**Unit tests for QA generation:** each reachable branch gets at least one deterministic test, invalid edges block launch, scenario deduplication, capped combinatorial explosion, fixed seed where possible, bounded max turns/cost; qualitative graders return evidence with defensible references.

## II.6 Editor/engine special cases to implement

| Edge case | Required behavior |
|---|---|
| Agent welcome asks employment and first user immediately answers | first user utterance satisfies `employment`, no duplicate prompt |
| User answers employment + income + preferences at once | validate all applicable fields, skip completed questions |
| User gives income before employment | retain candidate with provenance; do not infer monthly vs annual until clarified |
| Corrects salaried → self-employed | invalidate/inactivate dependent monthly-income answer; ask annual income |
| Says "not sure" | clarification and optional alternate path; never coerce fabricated value |
| Refuses personal details | early exit/partial outcome per script, respect user consent |
| Asks about unavailable annual fee | acknowledge missing source; never fabricate |
| Switches language EN↔HI | preserve flow state; speech-language routing independent of business progress |
| Barge-in interrupts assistant in middle of a question | only spoken text in history; state not marked complete simply because question was prepared |
| LLM returns malformed sideband metadata | reject update, keep prior state; appropriate clarification/diagnostic |
| Two turn responses arrive out of order | generation/turn ID guards prevent stale overwrites |
| Dependency cycle or no terminal step | static validation fails draft publish |
| Prompt injection instructs bypassing step | tool/state constraints still enforced; no privilege/credit actions |
| Customer changes subject permanently / says stop | end or offer resume; don't force script |
| Agent draft changed while test in progress | test pinned to the version/hash it started with; new test loads new draft |
| QA generator produces unrealistic scenario | mark invalid and regenerate within budget; don't count a false fail |

---
# PART III — FIRST-RELEASE PLATFORM CONTRACT

## III.1 Chosen architecture and operating modes

Retain the actual `web/`, `bot/`, `tests/`, `scripts/` and existing Compose locations. Add `platform_api/` for owner/admin control, `bot/flow/` for pure shared conversation logic, and `bot/runtime/` for authorized session glue. Do not relocate the project into `apps/` or `services/` simply to match an imagined tree. New feature modules are expected to be absent until their planned task creates them.

```text
Browser / existing Next.js web
  /api/v1 -> platform_api (authentication, owners, versions, wallets, admission)
                  -> fresh platform PostgreSQL database
  scoped media token -> one bounded Pipecat runtime process
                  -> configured STT / LLM / TTS registries
                  -> persistent event spool -> RabbitMQ -> history consumer -> PostgreSQL
  auth/reset mail -> separate bounded PostgreSQL delivery outbox -> configured mail adapter

Billing evidence and reservations: runner -> synchronous platform_api transaction
Flow execution: shared pure bot.flow controller, in memory per session
```

Production ingress serves web and `/api/v1` at one HTTPS origin; the control plane is not a Next.js BFF. Development UI/API defaults are 127.0.0.1:3001 and 127.0.0.1:8000; 7860 remains the runtime. All ports are configurable and checked against host listeners before launch. Production exposes only intended web/media routes; PostgreSQL, AMQP, control-service credentials and observability remain private. No new service was started by writing this spec.

- `local_offline`: preserve existing launcher, local `/client`, downloaded models and local management; enforce loopback binding; no platform account, public link, external provider, mail delivery, payment or external resource lookup. Keep Phoenix host-run and external resources disabled.
- `development` / `integration`: the shared runtime DB with a dedicated `platform` schema, deterministic providers first, synthetic credits; explicit origins and profile flags. Networked adapters are opt-in.
- `staging` / `production`: real PostgreSQL, authenticated admin MFA, secure ingress/media, private service credentials, selected backend qualification, retention and restore evidence. Public or real paid traffic is disabled until the release gate passes.

The deployment declares model endpoints, secrets and required network access. Nothing implies this machine's memory or latency number applies elsewhere. Every container has a memory limit sized for its target. Existing metric names and bounded label keys remain unchanged; user/session/call IDs belong in permitted events/traces, not metric labels.

## III.2 Dependencies and executable entrypoints

Use Python 3.12.13 as the first-release tested interpreter baseline, Node 24.15.0 and Bun 1.3.14 for the frontend toolchain. Keep current audio/SQL/test pins; promote directly used locked versions to direct pins: FastAPI 0.141.1, Uvicorn 0.54.0, Pydantic 2.14.1, email-validator 2.3.0 and cryptography 50.0.2. Add PyOTP 2.9.0 for admin TOTP, `PyJWT[crypto]==2.15.1` for media-join JWTs, aio-pika 9.5.8 for the event worker, HTTPX 0.28.1 and JSONSchema 4.26.0 to dev validation dependencies, and `@playwright/test` 1.64.1 to the web dev dependencies. Pin the same values in lockfiles during their implementation tasks; do not upgrade the model stack implicitly. These are selected versions, not a claim this planning session installed or resolved the new set. [aio-pika selected release](https://pypi.org/project/aio-pika/9.5.8/), [PyJWT selected release](https://pypi.org/project/PyJWT/2.15.1/); auth/test contracts record their primary references and current-lock evidence.

Selected future entrypoints (all launched by the project wrapper):

```text
API:        uv run uvicorn platform_api.app:create_app --factory --host <PLATFORM_HOST> --port <PLATFORM_PORT>
Schema:     uv run python -m platform_api.cli init-db
Admin:      uv run python -m platform_api.cli bootstrap-admin --email <address> --display-name <name> --reason <reason>
Mail:       uv run python -m platform_api.cli deliver-auth-mail --once --limit 25
Runtime:    uv run python -m bot.saas_runtime
Consumer:   uv run python -m platform_api.events.consumer
QA worker:  uv run python -m platform_api.qa.worker
Existing:   uv run python -m bot.bot  (local_offline only)
```

`platform_api.config.PlatformSettings` owns platform config. `platform_api.db.base.Base` and `platform_api.db.session` own its engine/session factory scoped to the `platform` schema; never import `bot.db` to initialize it. `create_app(settings=None)` imports routes without GPU/model startup. API liveness does not call providers; readiness checks expected schema, required credentials/admin enrollment and only dependencies required by enabled flags. Missing dependencies return 503 and cannot enable an unsafe feature.

## III.3 Shared database, schemas and setup

`DATABASE_URL` is the one explicitly exported PostgreSQL runtime URL shared by the local agent, platform control plane and Phoenix. Export it in the environment before launching the platform; platform startup and CLI commands must not silently load it from `.env`, invent a default, use another URL, or fall back to SQLite. The agent's tables stay in `public`; platform tables live in the dedicated `platform` schema selected by `PLATFORM_POSTGRES_SCHEMA` (default `platform`; it must not be `public`, `phoenix`, `information_schema`, `pg_catalog` or any `pg_*` schema); Phoenix telemetry lives in `phoenix`. No `PLATFORM_DATABASE_URL` or second runtime database is permitted. `DATABASE_URL` must be a `postgresql+asyncpg://` URL and SQLite URLs are rejected. The Phoenix host launcher derives Phoenix's PostgreSQL connection URL from this same `DATABASE_URL`, selecting only the Phoenix-owned `phoenix` schema. Supply the bootstrap role out of band; ordinary runtime credentials have no schema-change privileges in `platform` and none in `public` or `phoenix`.

`python -m platform_api.cli init-db` (also named `init-schema`) owns one setup or upgrade transaction/advisory lock, imports only platform metadata, and creates the initial `platform` schema when that schema is absent or empty. Fresh setup stores `schema_info(version='platform-v2', metadata_hash)` in `platform` alongside it. The only supported upgrade is an exact `platform-v1` metadata hash and object inventory: add `provider_secrets` and update `schema_info` to `platform-v2` in the same transaction; any other drift fails with `SCHEMA_MISMATCH`. Re-running an identical current schema is a no-op. Application startup checks that record and never calls create-all, global-active index repair, local seeding or migrations. Schema creation and the declared upgrade are explicit: neither import nor startup runs Alembic or data migrations, and the initializer never touches `public` or `phoenix`. During this unshipped build, test databases are disposable and separate from the runtime database; the verifier owns each database it creates and refuses a database without its run-ownership token. Never point tests at the runtime database.

Shared record rules: UUID IDs; UTC timestamps; explicit NOT NULL/FK/CHECK/UNIQUE constraints; JSONB for versioned immutable configurations; no Python floats for time/currency. Every owner-scoped relation has `owner_user_id`; derive it from authenticated context. Financial/audit rows are append-only with separate maintenance credentials for retention tasks. No global `is_active` uniqueness, global activation or legacy preset seeding enters the platform schema.

Initial tables and their owners:

- Auth/account: `users`, `auth_sessions`, `auth_tokens`, `auth_mfa`, `auth_rate_limits`, `auth_mail`, user preferences and `audit_logs` per the auth contract. User identity is defined once in `auth/models.py`, not duplicated in accounts.
- Agents: `agents(id, owner_user_id, name, description, enabled=false, draft_revision, current_published_version_id?, created_at, updated_at)`; `agent_drafts(agent_id UNIQUE, authoring_json, authoring_hash, revision, source, template_version_id, updated_at)`; `agent_versions(id, agent_id, version, immutable_config_json, authoring_hash, flow_hash, artifact_hash, policy_version_id, compiler_version, created_at)` with UNIQUE `(agent_id,version)`; immutable `agent_template_versions` and `platform_prompt_versions` with active pointers.
- Sessions: `sessions(id, owner_user_id, participant_id, agent_version_id, tariff_snapshot, kind, state, version, runner_id, connect_deadline, hard_end_deadline, created_at, ended_at?, failure_code?)`; `session_slots(session_id UNIQUE, owner_user_id, reserved_at, released_at?)`; `runner_registrations(session_id UNIQUE, runner_id, registration_id UNIQUE, expires_at, status)`; hashed `session_participants` guest capabilities and single-use join JTIs.
- Money: `wallet_accounts`, `wallet_entries`, `call_budgets`, `billing_operations`, `meter_checkpoints` exactly as the accounting contract. A verified new account creates one zero-balance wallet transactionally. No account-pool leases or epochs exist.
- History: `processed_events(event_id UNIQUE, session_id, sequence, payload_hash, type, received_at)` with UNIQUE `(session_id,sequence)`; `transcript_segments(session_id, sequence, event_id UNIQUE, speaker, final_text, start_ms, end_ms)`; per-session `transcript_revision`; `provider_usage(event_id UNIQUE, session_id, provider, model, kind, quantity, unit)` distinct from retail charges.
- Jobs/deployments: `agent_deployments(id, agent_id, public_id UNIQUE, enabled, policy_json)`; `qa_jobs(owner, immutable_snapshot_hash, state, budget, timestamps)`, stable `qa_scenarios`, `qa_runs`, `qa_suggestions`; `notifications` only if its UI task uses them. No queue or token record stores plaintext secrets indefinitely.

Agent versions retain original source, normalized flow, compiled manifest and pinned model/voice/policy IDs. Config response serializers redact secrets/admin-only prompt details. Foreign-key deletion never cascades away money/audit evidence. The first schema uses constrained strings for status values; no enum upgrade protocol is needed before release. Restore tests initialize a fresh DB, restore the recorded initial schema/data backup and assert ledger/config invariants.

## III.4 Identity and authorization

The [auth contract](docs/first-release/auth-contract.md) is authoritative for exact auth fields, endpoints, cookies, CSRF, hashing, resets, mail and MFA. Existing management routes are not authenticated today: their DB dependencies and CORS do not identify a customer. Build the missing layer in the separate API rather than calling those routes safe because they are local.

Selected design: normalized email/password accounts; bounded stdlib scrypt; opaque server-side session cookie; PostgreSQL revocation and idle/absolute expiry; exact-origin/CSRF validation; secure cookies in deployed modes; `user|admin` role; admin TOTP/step-up for consequential mutations. Email verification/reset use the explicitly configured durable mail adapter; local_offline does not start it. Bootstrap reads passwords through protected CLI input, never a default credential or secret command argument. No external identity provider, organizations or shared-owner roles are required.

Owner routes depend on `require_user`; admin routes on `require_admin` and mutations on `require_step_up`. Queries filter owner and resource ID together. Bodies cannot nominate another owner or change role. Other-owner and absent resources share 404. Guest and internal-service identities are separate and never accepted as owner cookies.

## III.5 Agent, chat and job APIs

Use `/api/v1` for public application endpoints and `/internal` for assigned runtime/service operations. Router schemas reject unknown keys. Success schemas are direct typed objects; errors use `{error:{code,message,request_id,retryable,fields:[{field,code}]}}`, with safe text and Retry-After for bounded throttling/temporary overload. Idempotency-Key is required on agent create/publish, deployed session create, wallet changes and manual end. Store canonical payload hashes and full outcomes; same-key different-body returns 409.

Owner API contracts:

- `GET /dashboard`: available/reserved/posted, active sessions, five recent owned agents, recent calls and `as_of`.
- `GET/POST /agents`: cursor/limit listing and `AgentCreate={name,description,source:manual,template_version_id?}` -> `201 AgentView={id,name,description,enabled,draft_revision,published_version_id?}`. Generated creation uses the proposal job below, then the same save path.
- `GET/PATCH /agents/{id}/draft`: PATCH `{expected_revision,config}` -> `DraftView={agent_id,draft_revision,authoring_hash,config,validation:{errors,warnings}}`. Structural errors 422; graph incompleteness can save; revision conflict 409.
- `POST /agents/{id}/validate`: saved revision/hash -> normalized diagnostics and executable eligibility. `POST /agents/{id}/publish`: `{expected_revision,changelog,acknowledged_warning_codes}` -> immutable `VersionView={version_id,version,flow_hash,artifact_hash}`; full graph/policy/capability validation and pointer update in one transaction; no automatic publish on save.
- `GET /agents/{id}/versions`, `POST /agents/{id}/restore-draft {version_id,expected_revision}`, `POST /agents/{id}/duplicate {source_revision_or_version,name}`, `POST /agents/{id}/enable|disable {}`. All owner-scoped. Enable controls new admission; it does not rewrite a live snapshot.
- `POST /agents/generate`: `{requirement,generation_model_id,language,token_budget}` -> `202 {job_id,status}`; `GET /generation-jobs/{id}` returns `{status,proposal?,unknown_business_facts,validation_warnings,usage}`, where usage and each token count may be null when the provider does not report them. Job states `QUEUED|RUNNING|CANCELLING|COMPLETED|FAILED|CANCELLED|EXPIRED`; running cancellation is `CANCELLING` until the worker aborts the provider request.
- `POST /agents/{id}/test/chat`: `{snapshot_hash,test_id,expected_turn_sequence,text}` -> `{test_id,turn_sequence,reply,flow_state,trace,outcome}`. `test_id` is a client-generated UUID required on the first request and reused for exact retries. Pin a compiled saved draft or published version, same reducer as voice, no audio slot; stale sequence 409, invalid snapshot 422. Reset creates a new test ID. Transcript commit is required before corresponding chat delivery completion.
- `POST /agents/{id}/test/voice-sessions`: snapshot selection -> funded synthetic/nonproduction or disclosed paid voice-test admission, session type `voice_test`; it consumes a voice slot and uses the same capability checks.
- `POST /agents/{id}/qa-jobs`: `{snapshot_hash,scenario_ids_or_count,token_budget,max_turns}` -> job reference. `GET /qa-jobs/{id}`, `/results`; `POST /qa-suggestions/{id}/apply-draft {expected_revision}` applies a validated draft-only patch. Frozen assertions/scenarios are reused for comparisons. No auto-publish.
- `GET /sessions` / `/{id}` / `/{id}/config-snapshot` / `/{id}/transcript`, `GET /usage`, `GET /wallet` / `/wallet/entries`: owner-only cursor pagination, bounded date range, original pinned snapshots and clear eventual-ingestion/estimated-versus-final states.

The initial implementation keeps all original product screens and two agent creation paths. New catalog selection uses backend registry capabilities; customers cannot configure arbitrary server URLs/credentials or dynamically install provider code. Platform model restrictions apply before publication and admission.

## III.6 Basic wallet accounting

The [accounting contract](docs/first-release/accounting-contract.md) replaces the pooled allocator completely. One wallet per user, integer paise, an append-only ledger, and small per-call reservations are enough for v1. PostgreSQL wallet-row locking serializes concurrent funded requests. A per-session cumulative permit prevents retries from adding duration twice. Checkpoints and charges commit synchronously; asynchronous history never authorizes spending.

Customer charge is ceiling-to-paise of pinned rate × cumulative billable milliseconds / 60,000. Rounding is cumulative per session, not per checkpoint. Setup/disconnection are free. No one-minute minimum. Separate simultaneous sessions retain their own rounding; three 10-second calls at 200 paise/minute require 102 paise. Available funds can shorten/deny a new reservation. Equal-time fairness is not promised; do not divide a wallet by configured maximum concurrency.

Normal close posts proven time and releases the rest. Lost-runner close waits for stop/permit expiry, settles only durable proof and waives unproven time as bounded platform exposure. The budget cannot be reopened or retroactively charged by late events. No account coordinator, epoch handoff, pool operations or indefinite reconciliation queue is part of this release. Gateway-funded top-ups are optional; audited admin credit/corrections are supported, while synthetic test credit stays nonproduction. Wholesale provider expense never silently changes retail price.

## III.7 Admission, media, version pinning and guests

Single runner process with a bounded task set. Per-user concurrency and runner capacity are distinct settings derived/qualified in the deployment; DB pool size is not voice capacity. API transaction locks an owner's admission row, validates enabled agent/version/account/tariff, creates a session and reserves one user slot. Outside that transaction, register the same session ID on its selected runner idempotently and obtain its first funded budget permit. Then commit REGISTERED and return join details. Registration or funding denial compensates slots safely; unknown outcomes are queried/replayed by the same ID, not assumed failed. Never hold a DB transaction over HTTP/media/model calls.

Session states: `CREATED -> RESERVED -> REGISTERED -> CONNECTING -> INITIALIZING -> READY -> ACTIVE`. Active can enter nonbillable `RECONNECTING`, then ACTIVE within grace or ENDING on expiry. ENDING drains/cancels and becomes COMPLETED; any nonterminal state may fail; setup deadlines can EXPIRE. Every state update validates `(session_id,expected_version,allowed_transition)`; terminal execution never reopens. Budget OPEN/CLOSED and customer final amount are independent of delayed history ingestion. Teardown releases execution slots even while final financial response is being retried.

The API issues a short-lived asymmetrically signed join JWT using the selected PyJWT library and Ed25519 keys (fixed `alg=EdDSA`) with issuer, `aud=voice-runtime`, `scope=webrtc:join`, participant subject, session, version, runner, JTI and issued/not-before/expiry. Verification fixes `algorithms=['EdDSA']`, issuer and audience and requires every listed claim; never select an algorithm from an untrusted token. Store/consume the JTI atomically. JWT verification at the actual signaling boundary precedes any offer/provider allocation; verify all bindings against the registered session. Runtime and internal APIs also require separately injected service credentials and assigned-session authorization. A valid join credential never extends a budget or hard call deadline.

SaaS execution loads only the immutable registered-session runtime snapshot. It bypasses legacy `resolve_session_settings(agent_id)`, global activation, current base-prompt lookup/default fallback and process-wide `apply_mode`. Load/hash/version failure ends before ACTIVE. The local path keeps those behaviors isolated. Adapt the current VoiceSession without invoking its global activation callback in SaaS. The pinned snapshot owns all provider/model/language settings; routing still goes through existing registries/protocols.

Public deployment admission `POST /api/v1/public/deployments/{public_id}/sessions` resolves the wallet owner server-side and checks enabled deployment, disclosure acknowledgment, exact-origin policy and per-IP/per-agent admission limits before normal capacity/funding. It creates a distinct random participant and hashed opaque capability cookie. That capability permits only its session's safe status, reconnect token and idempotent end (`/api/v1/guest/sessions/{id}/...`), never owner history/config/wallet/admin access. Cookie lifetime is bounded by the hard session deadline and revoked at terminal cleanup. Reconnect retains the original session/tariff/budget, rechecks eligibility and funding, issues a fresh single-use JTI and replaces the prior peer. Outside grace/after terminal return 409; a new call needs new admission. Disabling a deployment or suspending the owner denies new/reconnect admission; in-flight end is an explicit policy action.

Public admission is not enabled until its TLS, guest authorization, replay/rate/abuse, resource-budget and storage preconditions have passed. Do not defer those protections to a later UI-only security task.

## III.8 Events and durable history

The runner writes critical transcript/usage/lifecycle events to the durable append-only JSONL journal at `data/runtime/spool/<runner_id>/journal.jsonl` before reporting capture. It fsyncs journal appends and newly created directory entries; no embedded database or SQLite fallback is used. It then publishes persistent messages to RabbitMQ `voice.session.events` with mandatory routing and publisher confirmation. One history consumer validates and writes `processed_events` plus its effect in the same PostgreSQL transaction, ACKing only after commit. Use event IDs and `(session_id,sequence)` hashes to detect replay/conflicting payloads. `sequence` is session-wide across every durable event class; persist allocation with spool append. Transcript numbering need not be contiguous across non-transcript events. Out-of-order valid projections can persist; they cannot regress execution or money.

History orders transcript by sequence and returns `transcript_revision`, `segments`, `has_gaps`, `next_cursor`. Cursor includes the revision; insertion changes it and an obsolete cursor yields 409 `TRANSCRIPT_CHANGED` so a late segment is not permanently skipped. Gap timeout raises an operational signal, not fabricated text. Ephemeral STT hypotheses/audio are not durable history; no live recording is enabled.

The selected retry protocol is explicit republish-confirm-before-source-ACK for critical events. Persist original IDs/body to declared durable retry/quarantine queues, verify routing/confirmation, then ACK. Keep source on transfer failure; pause/back off rather than hot-loop. No unsafe default TTL/dead-letter hop holds the only copy. The retry worker retains the message until its configured delay is due; replay keeps the original event. Consumer errors never directly debit money. Inject destination outage after producer outbox removal to prove recoverability.

Broker down -> bounded spool, stop admission/drain before capture fails. DB down -> consumer keeps messages unacknowledged. Crash after commit -> duplicate no-op. Disk loss/single-node failure is an explicit availability limit requiring backups, not a zero-loss claim. Broker operational isolation versus direct API batch ingestion remains the chosen tradeoff; record event/storage/memory load before enabling it. Auth mail uses its own PostgreSQL outbox and does not depend on Phase 8 AMQP.

## III.9 Observers, operational constraints and flags

One exported metric registry/instrument set; per-session attribution from that session's pinned bindings when payloads omit model. Normalize processor labels to a fixed supported set. Memory sampling starts once per process and stops only at process shutdown; one session cannot cancel sampling for another. Existing names, bounded labels, routing modularity and backend-independent pipeline imports remain test contracts. New model/prompt/latency-path implementation reruns relevant measurement scripts and updates README with machine/model/corpus/deployment conditions.

`PLATFORM_ENABLE_GENERATION`, `PLATFORM_ENABLE_VOICE_TEST`, `PLATFORM_ENABLE_PUBLIC_CALLS`, `PLATFORM_ENABLE_REAL_PAID_CALLS`, `PLATFORM_ENABLE_QA`, `PLATFORM_ENABLE_GATEWAY` default false in a new installation. Backend route guards enforce them, not only UI hiding. Nonproduction harnesses can enable the specific feature under test with synthetic credit. Public/real-paid flags can become true only after Phase 12 evidence and required dependencies exist. Gateway remains false without an implemented verified integration; manual audited administration does not require a gateway.

Retain explicit limits on request bytes, auth hashing concurrency, model queues, sessions, transcript/export pages and QA jobs. Deployment config supplies actual latency SLOs, model timeouts, concurrency and storage budgets measured in that environment. Source guardrails travel; local benchmark values do not. Retention policy, restore evidence and selected media/backend qualification are launch inputs, not missing business-architecture choices to be guessed in code.

---

# PART IV — IMPLEMENTATION ORDER, FILE ACTIONS AND ACCEPTANCE

## IV.1 File-action decisions

The exact auth/flow/test lists live beside their authoritative contracts; the [implementation plan](docs/first-release/implementation-plan.md) connects them with interfaces and assertions. Selected paths are new code to create, not features claimed present.

- **Create foundation:** `platform_api/__init__.py`, `app.py`, `config.py`, `health.py`, `cli.py`; `platform_api/db/{__init__,base,session,schema}.py`; `platform_api/models/__init__.py`. New independent metadata, fresh-DB initializer and app factory; no auto-import of local metadata.
- **Create features:** auth/accounts files listed in the auth contract; `platform_api/agents/{models,schemas,validation,publishing,service,routes}.py`; `platform_api/sessions/{models,schemas,service,routes,admission}.py`; `platform_api/public/{models,service,routes}.py`; `platform_api/billing/{__init__,models,schemas,pricing,service,routes}.py`; `platform_api/events/{__init__,schemas,models,repository,consumer}.py`; `platform_api/admin/{__init__,routes,service}.py`; `platform_api/qa/{__init__,models,schemas,service,routes,worker}.py`; `platform_api/testing/chat.py`; common `platform_api/audit.py`. Each model has one metadata owner.
- **Create shared/runtime:** pure `bot/flow/` package from its contract; `bot/runtime/{__init__,snapshots,budget_client,session_task,outbox,transport_auth}.py`; `bot/saas_runtime.py`; `bot/flow_runtime.py`; `bot/adapter_qualification.py`. Extract reusable pipeline construction behind a backend-neutral entry without changing local defaults.
- **Create frontend:** platform/auth client/types/guards/pages from auth contract; `web/src/lib/platform-agent-client.ts`; workspace flow/test components; dashboard/conversation/billing/admin pages; `web/e2e/`, `web/playwright.config.ts`, `web/bunfig.toml`. Reuse existing forms, VoiceSession and transcript helpers.
- **Create verification/ops:** exact files in verification plan; `scripts/verify.py`; dedicated test Compose; `.github/workflows/verify.yml`; private ingress/RabbitMQ config under `observability/platform/`. Add observability without renaming existing series. Existing databases/data/fixtures are not reset by test startup.
- **Modify:** selected pins/locks, `.env.example`, `scripts/voice-ai.sh`, root Compose profiles/limits; bot runner/composition/observer/transcript integration, existing compiler wrapper and router capabilities; frontend layout/sidebar plus profile-specific client selection and versioned transcript transport. Measured README tables change only when implementation measurements actually change.
- **Delete:** no current source files or stored records. Remove customer Playground navigation only after integrated Voice Test passes; retain offline `/client` and local management. No source tree move, legacy backfill or database migration is a task in this release.

## IV.2 Stable phase dependency graph

```text
0 foundation/shared schema/tests -> 1 auth -> 2 flow/schema/controller/adapter spike
  -> 3 drafts/versions/publishing -> 4 customer UI/generation/chat
  -> 5 basic wallet + synchronous meter -> 6 authorized voice
  -> 7 guest/security preconditions -> 8 durable history events
  -> 9 conversations/billing/admin -> 10 Flow QA
  -> 11 retention/restore/ops -> 12 release qualification
```

The sequence fixes earlier circular gates: publication follows the executable compiler; generation follows schemas; accounting proof is durable synchronously before broker history; guest protection is implemented before public admission can be enabled. Every phase leaves the current offline path working and the unfinished platform feature disabled. No phase claims the whole product is complete because a module or route name exists.

### Phase 0 — Foundation, initial schema and verification harness

Create the API app/settings/DB/CLI, health endpoints and schema contract, selected pins and new verifier/fixtures. Inventory actual interfaces; keep all feature flags false. Use a disposable PostgreSQL cluster for development/tests, one shared runtime database with `public`/`platform`/`phoenix` schemas, and no SQLite fallback or local DB imports. Record baseline commands, backend/model conditions and host port/memory constraints. Test empty platform-schema initialization, repeat no-op, wrong/nonempty platform-schema refusal, wrong schema readiness and zero GPU side effects on API import. Baseline code tests remain runnable.

### Phase 1 — User/admin auth and account initialization

Implement the auth contract, zero-balance wallet/account creation hook, cookie/CSRF/owner guards, admin CLI/TOTP/step-up, reset/verification delivery and auth UI. All domain routes except explicit public/bootstrap/health allowlist deny anonymous access. Test cross-user identity, concurrent reset/session revocation and MFA replay. Exit: two users and an enrolled admin can authenticate independently; local mode remains account-free and loopback-only.

### Phase 2 — Shared flow engine, schemas and qualification spike

Implement the linked authoring/executable schemas, normalized compiler/hash vectors, pure reducer, explicit edge/completion/interrupt events and deterministic card/loan fixtures. Use fake adapters for contract tests, then run the selected real adapter/playback qualification spike. Record a concrete supported capability tuple or `unsupported`; unsupported voice remains disabled rather than silently substituting generated text for played speech. No business action tool exists unless code-registered with a complete contract. Exit: executable fixtures, validation and state assertions pass; the real support matrix is evidence, not an assumption.

### Phase 3 — Agent drafts, templates and immutable publication

Create owner-scoped new agent records, copied default templates, policy versions, draft revision CAS and authoring hashes. Save incomplete graphs, block executable testing/publication until valid, then compile and atomically publish immutable versions. Never use local global activation. Test two-owner isolation, lost-response publish replay, concurrent edit 409, restore-to-draft and pinned policy/provider/hash identity. Exit: versions are actually immutable and executable configuration exists before UI claims publish success.

### Phase 4 — Customer navigation, generation and Chat Test

Adapt existing web UI with customer/admin separation; keep Playground usable until Phase 6 replacement passes. Add top-five owned agents, both creation paths, structured editor/view and integrated Chat Test. Generation is a bounded proposal job, not auto-publish. Chat consumes the exact shared reducer/version and commits messages before completion. Exit: manual/generated drafts validate/publish using Phase 2/3 foundations; deterministic out-of-order/correction/refusal paths work and no audio slot is used. Enable generation only for qualified configured model/caps.

### Phase 5 — Basic prepaid wallet and durable meter API

Implement integer pricing, wallet/entries/per-call budgets/operations/checkpoints and admin adjustments per the accounting contract. No pool coordinator. API transaction records prove usage before asynchronous history exists. Use the executable accounting oracle independently from service code. Test actual PostgreSQL concurrent last-credit reservations, mixed rates, cumulative partial seconds, ambiguous response replay, denied renewals and stopped/expired crash write-off. Real paid/public flags remain false; tests/nonproduction use synthetic money. Exit: shared funds cannot be double-reserved and no unproven time is charged.

### Phase 6 — Authenticated Voice Test and runtime ownership

Build authorized registered-session signaling, single-use joins, immutable snapshot load, budget loop and correct ACTIVE/RECONNECTING/ending transitions. Integrate shared flow/capability tuple, cancellation, locale routing and delivered-history semantics. Adapt metric attribution/process sampler lifetime. Test config edits/global overrides cannot change a pinned call; two concurrent model bindings report correctly after one ends. Pass EN/HI actual audio and barge-in/reconnect on the selected deployment; only then switch customer test navigation to integrated Voice Test. Unsupported combinations are unavailable, not falsely successful.

### Phase 7 — Hosted guest admission and security preconditions

Implement opaque deployment IDs, server-derived owner billing, hashed guest capabilities, own-session reconnect/end, expiry/replay/peer replacement, admission limits and TLS/TURN/trusted ingress checks. Owner/admin resources stay inaccessible to guests. Verify two-user, cross-participant, rate, zero-credit and disabled-deployment cases in staging with synthetic credit. Exit: public admission preconditions pass; global public/real-paid release flags remain false.

### Phase 8 — Spool, RabbitMQ and durable history

Implement critical event capture, persistent session sequence allocation, confirmed-routing publisher, idempotent consumer and confirmed retry/quarantine protocol. Explicit bounded storage/backpressure and loss policies. Test restart/replay, conflicting IDs, out-of-order transcript pagination, DB/broker outage and retry destination failure after initial outbox removal. This phase is history durability, not the source of wallet truth.

### Phase 9 — Conversations, wallet and admin operations UI

Wire owner history/transcripts/config snapshots, wallet entries and usage to real data with pending/estimated labels. Build admin user/agent/call inspection, audited credit/refund/rate/default-template/prompt/model changes and emergency admission control. Privileged access/step-up must already exist from Phase 1. Never expose provider secrets or unrestricted transcript content. Test every owner/admin/guest boundary and new-tariff versus pinned-call behavior.

### Phase 10 — Bounded Flow QA

Reuse the Phase 2 validator/controller; add frozen deterministic branch scenarios, capped LLM utterance expansion, same-engine simulations, optional evidence-bearing judge and draft-only suggestions. Jobs get per-user/global budgets and yield to voice. Test malformed scenario rejection, coverage, corrections, cancellation, no auto-publish and rerun of the same regression corpus. Required for the first full paid release.

### Phase 11 — Privacy, restore and operational qualification

Implement configured retention/export/deletion without deleting immutable money/audit evidence, redaction and bounded artifacts, fresh-DB backup/restore drills, queue/storage/runbook ownership and per-profile network resource checks. Verify all auth/public controls again; do not first implement them here. Record target deployment latency/throughput/memory and compare identical model/audio conditions. Store deployment-sized limits in config; no borrowed machine numbers.

### Phase 12 — Complete launch qualification

Run the verification plan's mandatory unit/PostgreSQL/browser/actual-audio/flow/QA/concurrency/fault/restore gates with zero required skips. Walk through signup, manual/generated agent, publish, chat, voice, guest, shared wallet, history and admin correction. Qualify only advertised backend/browser combinations. Every Phase 0–11 gate and selected operational policy must pass before public/real-paid flags can be enabled. Gateway remains off unless its independent verified integration exists. Missing evidence is failed/not-run, never inferred from a selected dependency or plan checkbox.

## IV.3 Cross-subsystem interfaces

- API principal/guard/error/session/account hook: exact types in auth contract; all ownership uses server identity.
- Graph/controller/compiler/adapter events: versioned types, schemas, canonical hashes and fixtures in flow contract. Private helper names can vary; observable rules cannot.
- Money: exact `charge_minor`, `checkpoint_and_reserve`, close/expiry APIs and independent reference oracle in accounting contract; same cumulative time across reconnects.
- Runtime config: `GET /internal/sessions/{id}/runtime-config` returns only registered immutable source/normalized/artifact/provider references, session/tariff/budget/participant binding. Missing/wrong hash fails before provider initialization; no mutable/default fallback.
- Session/admission: `reserve_session(principal, agent_version_id, kind, operation_id) -> RegisteredSession`; `register_runtime(session_id, runner_id, registration_id) -> RuntimeReservation`; `issue_join(session_id, participant_id) -> JoinDetails`. Commands return the same persisted outcome for the same input/ID, enforce the state CAS and perform remote work outside DB locks.
- Event ingestion: `append_event(event) -> durable_position`; `publish_pending(limit) -> PublishBatchResult`; `consume_event(body) -> inserted|duplicate|quarantine`. No consumption side effect touches wallet entries. Replica counts stay one for API/runtime/consumer initially; scale is a separate measured change.

## IV.4 Verification commands and truthful evidence

The [verification plan](docs/first-release/verification-plan.md) names exact current commands, new wrapper targets, required environment, fixtures, CI files and pass/skip policy. Its new commands are implementation deliverables. Existing evidence from the earlier review was 266 passed/5 skipped in the unchanged baseline suite; that is not a new platform test result. Flow JSON fixtures and accounting oracle can be validated now; no current auth or SaaS feature is claimed passing until code exists and its gate runs.

On every code slice: write the owned negative/happy-path assertions, observe them fail against absent/incorrect behavior, implement the smallest module, run its focused gate and baseline regressions, and record exact output and conditions. Do not move files or rewrite tests to hide a broken modularity/metric contract. Release qualification includes deliberately real media input and actual selected models, not just silent input/text messages or a stub LLM benchmark.

## IV.5 Review disposition for the user's correction

Earlier R-001 migration/backfill is superseded by the explicit fresh-install scope, not a condition the user must satisfy. Earlier pooled-account accounting R-002 is replaced by the bounded simple wallet contract and executable examples. R-003 file/dependency mapping, R-004 identity, R-005 flow schema/adapter protocol, R-006 phase dependencies, and R-009 verification/fixtures now have concrete decisions/artifacts. New-feature file creation is expected and does not mean existing code drift. Implementation and environment qualification remain work to execute under these plans, not unresolved requests for the user to invent architecture.

The next engineering slice is Phase 0's concrete foundation task followed by Phase 1 auth, not a migration project and not simultaneous implementation of every feature. No user approval pause is required by the current planning instruction; no deployment, database reset, external message or paid call is implied.

---

**End of first-release specification.** The earlier snapshots and review reports remain audit history; this v3 scope and its linked contracts supersede their migration and pooled-coordinator requirements.

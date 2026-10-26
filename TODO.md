# TODO — finish the current local agent studio implementation

This checklist tracks the current review-and-finish work in this repository.
It is not the SaaS backlog in the Downloads reference documents. Keep this
project local and air-gapped, preserve its swappable providers, and do not
commit generated data or audio fixtures.

## Working agreement

Before starting future work, record its owner and acceptance checks here, then
update this ledger as work and verification finish. Implementation subagents do
assigned reading, writing, and tests; the parent plans and reviews work and
serializes focused commits. Worker agents do not stage, change the index, or
commit.

## Current pass — UI bug fixes only (2026-10-27)

The redesign was cancelled by user decision; the committed warm UI stays. Plan:
`docs/superpowers/plans/2026-10-27-ui-bugfix.md`. Workers write and test; parent
plans, reviews every diff, and commits. Task 1 below is kept because it fixes a
real session bug and supplies tested helpers for the fixes.

- [x] Task 1 — behavioral core, test-first: duration format, transcript log,
      form payload builders, mic-before-activate session, aligned ranges.
      Committed `38bdf08`, 57 tests.
- [x] B1 — call modal lifecycle: per-session transcript log, remote audio and
      bot-speaking timers cleared, body scroll lock, focus containment, send
      gating, dead interim state removed. Committed `2d007c4`.
- [x] B2 — field focus ring restored; `/agents/[id]` truthful error + stale
      guards; `/agents` stale guard. Committed `6be9655`; error mapping covered
      by 13 new tests (70 total).
- [x] B3 — editor ranges from constants, derived slug on create, validated
      payloads that never send slug/is_active, single dirty check, honest
      token-estimate label. Committed `de1607e`; 73 tests. Follow-up `32b37e7`
      reports a malformed slug as malformed instead of "required".
- [x] Browser-only call bug found by the fake-media integration pass: native
      `fetch` was invoked as a method of the dependencies object and threw
      `Illegal invocation`, so every real WebRTC offer failed. Fixed in
      `6c487eb` by calling it through `globalThis`, with a receiver test.
- [x] B4 — call transcript fragments: raw data-channel capture showed one
      assistant turn arriving as 23 `bot-llm-text` token messages plus
      `bot-transcription`, `bot-output`, `user-input` and project `transcript`
      duplicates, and the dialog rendered every representation. The dialog now
      reads only the project's completed `transcript` messages and merges
      consecutive assistant segments into one turn; a user line starts a new
      turn. Committed `ee9c365`; 11 protocol tests added, full web suite 85 pass
      across 8 files (222 expectations), typecheck and production build clean.
      Browser fake-media call rendered exactly one user line and one merged
      assistant reply, no token fragments, no duplicated reply, no console
      errors. Limit: the model text can still contain a missing space inside a
      single spoken segment (`youneed!`) because the backend forwarder emits
      Pipecat's aggregated text as-is; this client-only pass does not rewrite
      word boundaries.
- [x] B5 — live transcript streaming: assistant tokens now grow one bubble
      from `bot-llm-text`, and user speech grows one bubble from interim
      `user-input` transcription deltas, each with a caret. The project's final
      user `transcript` replaces the live bubble in place; duplicate
      `user-transcription` / final `user-input` copies are ignored. Two real
      ordering edges the synthetic replay missed: `user-started-speaking` can
      arrive *after* the first STT delta and even after the final transcript, so
      it cannot be a turn boundary. Late deltas are now classified by whether
      they continue the finalized utterance's text toward its final copy; the
      first non-continuing word starts the next turn. Verified on 2026-10-27
      with the real realtime STT backend in headless Chrome, feeding locally
      normalized FLEURS audio through a Web Audio microphone. One turn
      (`en_us/0000.wav`, 10.56 s): 50 interim deltas, one user bubble that grew
      before the final, caret visible while streaming, final transcript
      replaced it in place and dropped the caret, trailing `.` and space
      ignored, no console errors. Two turns (`0000.wav` then `0004.wav`,
      10.56 s + 4.32 s): the second turn streamed from `Many` through 32 deltas
      into one bubble, its final replaced it, both bubbles were exact matches,
      and the start event arrived only after that final. Synthetic data-channel
      pass: 15/15 checks. Real text-input pass: one turn and two turns rendered
      whole replies with no fragments or duplicates; close restored scroll and
      focus. Web suite 117 pass across 8 files (308 expectations); typecheck
      clean; production build clean (8/8 static pages); Python suite 214 pass,
      5 skipped (PostgreSQL cases need `TEST_DATABASE_URL`). Limit: the browser
      used an injected audio stream, not a physical microphone; the real STT
      server, VAD, interruption path and final transcripts were exercised
      end-to-end.
      One headless run hit a backend output-transport `error` mid-turn; after
      the truncated final, the STT replayed that utterance's partials, so the
      run was excluded from the passing measurement. The clean rerun passed.
- [x] Closeout — full web suite, build, Python suite, browser pass, commits.
      Web: 74 pass across 7 files, 185 expectations; typecheck and production
      build (8/8 static pages) clean. Python: 214 passed, 5 skipped (PostgreSQL
      cases need `TEST_DATABASE_URL`), 2 upstream deprecation warnings. Browser
      route pass (Chrome headless against the dev server on `:3001`): all five
      routes render with no console errors, no horizontal overflow at 390px,
      keyboard focus shows a 2px solid `rgb(219, 161, 111)` ring with the accent
      border, the speed hint reads `0.50x to 2.00x`, and the token badge reads
      `~39 tokens (est.)`. Browser call integration with Chrome's fake media
      device: the modal reached `Connected`, locked body scroll, enabled the
      input, sent a text turn (user bubble rendered, draft cleared, no error),
      then closed with body scroll restored, focus returned to `Talk with
      Agent`, and no console errors. Limits: no physical microphone, real STT
      recognition or speaker playback was used; the send-failure branch was
      verified by inspection and unit-level behavior only.

## Completed (prior pass)

- [x] **Audit and preserve existing work — owner: parent.** Read repository
  instructions and reference material; reviewed the staged and unstaged work
  without discarding it. Recorded the existing verification baselines: 15
  backend-focused tests, 161 full-suite tests, and the web build baseline.
- [x] **Backend API and persistence behavior — owner: backend agent.** Added
  bounded agent validation, null rejection, local provider/voice validation,
  duplicate and missing-resource status handling, prompt alias/empty-value
  semantics, settings-copy application including `llm_max_tokens`, compiler
  strict-variable checking, and safe database error handling.
- [x] **Backend concurrency and existing local schema — owner: backend agent.**
  Added atomic prompt revision allocation, refreshed agent state after the
  activation lock, idempotent concurrent seeding, partial active-agent and
  prompt-revision uniqueness indexes, and an idempotent upgrade for existing
  SQLite/PostgreSQL tables. Database configuration now comes from typed
  `Settings.database_url` and `Settings.sqlite_database_url` fields.
- [x] **Backend focused verification — owner: backend agent.** SQLite-focused
  tests passed (35 tests); disposable-schema PostgreSQL integration tests
  passed (4 tests) using only `TEST_DATABASE_URL`; the schema-upgrade test
  preserved existing rows and verified new constraints. PostgreSQL tests are
  skipped when `TEST_DATABASE_URL` is unset.
- [x] **Backend review and assembled suite — owner: parent.** Approved the
  backend core/API after source review. Parent's latest native full Python suite
  passed 219 tests with zero skips and two upstream deprecation warnings; the
  separately run disposable-schema PostgreSQL suite passed all four cases.
- [x] **TDD seams agreed — owner: parent and user.** Approved public API
  requests, agent/prompt persistence operations, CLI start/stop behavior,
  voice-session connection/cleanup, and local profile measurements. Record
  red-then-green evidence for each behavior.
- [x] **Parallel ownership established — owner: parent.** Parent plans and
  reviews; Luna agents own backend, runtime, and web tasks. Changes and commits
  remain serialized through parent review.

## Completed implementation and review

- [x] **Backend handoff package — owner: backend agent.** Prepared
  `/private/tmp/voice-ai-backend-review.md` with scope, changed files, recorded
  red/green commands and results, residual limitations, and small commit group
  suggestions. PostgreSQL integration used only random disposable schemas.
- [x] **Backend final review — owner: parent.** Backend core/API reviewed and
  approved; parent owns final assembled review and commits.
- [x] **Web API client handoff — owner: backend agent.** Exported the reusable
  `ApiClient`; tested public HTTP paths, methods, JSON bodies, error responses,
  delete response shape, clone name/slug limits and preserved settings. A real
  ephemeral loopback HTTP roundtrip passed with elevated loopback permission;
  sandboxed Bun runs fail to bind. With the web agent's lifecycle tests, full
  Bun suite passed 10/10 with 35 assertions; `bun run typecheck` passed. The clone
  overflow test failed under the prior behavior with a 135-character name and
  passed after the bound was applied. Parent committed the client and test in
  `ad7aa99`.
- [x] **Runtime behavior — owner: runtime agent.** Core runtime, session,
  transcript, settings, CORS, launcher, benchmark, and documentation behavior
  is reviewed and approved.
- [x] **Current runtime wiring — owner: runtime agent.** Typed database URLs
  preserve the prior SQLite path; bounded `Settings.web_port` follows launcher
  env precedence; VAD and web-port bounds passed 29 focused tests.
- [x] **Per-session profile selection — owners: runtime and web agents.** A
  bounded explicit `agent_id` selects a saved profile while calls without an id
  retain configured Settings; mode precedence, missing-ID errors, and the real
  `AgentService` integration path passed native tests.
- [x] **Local profile measurement — owner: runtime agent.** All six profile
  measurements completed natively, and the README table now records the
  measured values and conditions.
- [x] **Visual replacement and web behavior — owner: web agent.** Replaced the
  earlier cyan treatment with the latest warm ink, limestone, oxidized copper,
  and moss direction. Complete layouts for overview, agent list/editor, shared
  prompt, playground, and call experience. Web unit/integration tests and build
  passed; browser review approved the current direction and main routes.
- [x] **Agent editor save contract — owner: web agent.** Fixed the editor to
  use explicit activation, keep slugs immutable while editing, and preserve an
  empty reasoning setting. The web agent reports 10 tests, build, and typecheck
  passing; parent reviewed the updated UI.
- [x] **Browser review fix batch — owner: web agent.** Removed the empty model
  option, added a visible Reasoning selector, spaced identity fields, and
  replaced inaccurate base-prompt/XML copy. Native single-choice radios work
  with keyboard navigation. Parent reviewed and approved this focused batch.
- [x] **Web docs and local startup config — owner: web agent.** Updated the
  design reference to the accepted warm direction and actual tokens; Next
  dev/start bind to loopback with telemetry disabled, and the broken unused
  lint script was removed. Air-gap/no-auth loopback behavior is preserved;
  parent will restage/commit the final web paths.
- [x] **Impeccable sidecar check — owner: web agent.** Added YAML token
  frontmatter to `web/DESIGN.md` and schemaVersion 2
  `web/.impeccable/design.json` with actual color ramps, primitives,
  breakpoints, and narrative. JSON parses and matches the tooling reference;
  no code changes or tests/build reruns.
- [x] **Browser preview source sync — owner: runtime agent.** Refreshed the
  loopback 3001 preview to the latest built source with telemetry disabled and
  verified the Studio page shows the new design.
- [x] **Live API preview consistency — owner: runtime agent.** Verified the
  current loopback 7860 API serves the final catalog, including `mlx-whisper`,
  and the latest validation behavior. The refreshed UI was checked against
  the live local API; STT 8020, Ollama 11434, observability, and unrelated
  processes were preserved.
- [x] **Cross-surface integration — owners: runtime and web agents.** Runtime
  profile selection, API contracts, activation behavior, and session defaults
  are reviewed together; live local API and refreshed UI previews were checked.
- [x] **Local voice media-path acceptance — owner: runtime agent.** The
  strengthened live bilingual script passed once in 12.86 seconds against the
  project-local service chain. It used text-injected English/Hindi LLM turns
  and TTS with a silent synthetic microphone track and verified transcript
  messages plus remote non-silent audio. No physical microphone, STT
  recognition from real audio, real speaker playback, external provider, or
  model download was used. The test is committed as `4a469f3`.

## Completed verification and closeout

- [x] **Final unit/build checks — owner: parent.** Final native command
  `RUN_LIVE_VOICE=1 TEST_DATABASE_URL=<disposable local PostgreSQL URL> scripts/voice-ai.sh test`
  passed 219 tests, zero skips, and two upstream deprecation warnings in 19.64
  seconds. Web suite passed 10 tests with 35 assertions; typecheck and build
  passed. Six local profile measurements and the README conditions/results are
  recorded.
- [x] **Final integration closeout — owner: parent/runtime.** Live bilingual
  media-path check passed and was committed as `4a469f3`; parent combined it
  with the disposable-PostgreSQL suite above. The test used text-injected
  English/Hindi LLM turns and TTS with a silent synthetic microphone track and
  verified remote non-silent audio. Physical microphone capture, STT
  recognition from real audio, and real speaker playback were not tested.
- [x] **Final launcher and measurement checks — owner: runtime agent.** Fresh
  launcher and six-profile measurement checks passed; measured README values
  and conditions are recorded.
- [x] **Browser review — owner: parent with web agent.** Parent reviewed
  desktop overview, agents, editor, shared prompt, and call setup; 390px mobile
  form has no overflow and shared prompt remains readable. Model options,
  reasoning selector, field spacing/copy, and native radio keyboard navigation
  were reviewed and approved. The browser review did not use physical audio
  devices or exercise a connected browser dialog; the separate local synthetic
  SmallWebRTC path did verify returned TTS audio. No physical STT recognition
  or full accessibility audit is claimed.
- [x] **Final code/scope/generated-file review — owner: parent.** Parent
  reviewed backend, runtime, web, measurements, and docs. Confirmed no files
  under `data/` or `evals/fixtures/audio/` are committed; only this final TODO
  update remains to be committed.
- [x] **Small focused commits — owner: parent.** Parent committed the 18
  reviewed source groups serially; workers never staged, changed the index, or
  committed.
  - [x] Dependencies/config/storage — `71e213b`.
  - [x] Agent/prompt persistence — `2f4d02e`; compiler/schema — `7b9542e`;
    agent operations/DB integration — `117f95c`.
  - [x] REST API — `5867f54`.
  - [x] Runtime/session/transcripts/settings/CORS — `e1d7942`.
  - [x] Launcher — `ed9d6b9`; benchmark/README — `d597da4`.
  - [x] Web tooling/docs — `a5df37f`; typed API tests — `ad7aa99`.
  - [x] Voice controller — `dac7390`; call dialog — `2c3e4ea`.
  - [x] Agent editor — `58e902f`; agent routes — `87a80aa`.
  - [x] Shared instructions/call setup — `2543627`.
  - [x] Warm shell — `9e1c9df`; product/design record and sidecar — `2963700`.
  - [x] Live bilingual media-path acceptance — `4a469f3`.
  Parent commits this ledger separately and verifies clean status.
- [x] **Closeout preparation — owner: backend agent.** This ledger contains
  completed work, verification, all 18 source commit IDs, and remaining
  physical-audio/testing limits. The parent delivers the user report after
  committing this ledger and verifying clean status.

# Single Prompt Architecture Design Specification

- **Date**: 2026-11-08
- **Topic**: Unified Single-Prompt Voice Agent Architecture
- **Target Repository**: `voice_ai` (`backend` & `frontend`)

---

## 1. Problem Statement & Goals

The multi-field, multi-outcome state machine adds excessive complexity across database tables, tool-calling evaluation layers, and the UI. In practice, modern LLMs (such as GPT-4o-mini) perform reliably when guided by a single comprehensive system prompt defining persona, information gathering goals, conversation rules, and edge cases.

### Key Goals
1. **Radical Simplification**: Remove fields, conditions, outcomes, flowchart nodes, and 2-pass tool execution.
2. **Single Full-Prompt Editor**: Provide an intuitive, distraction-free large text area in the agent editor where users configure the agent's complete prompt instructions.
3. **Single-Pass Low-Latency Runtime**: The voice runtime and chat runtime directly send the system prompt (global admin guardrails + agent instructions) and dialogue history to the LLM, eliminating tool-calling roundtrips, reducing turn latency, and cutting LLM token costs.
4. **Clean Cutover**: Drop obsolete database tables (`fields`, `outcomes`, `answers`) and delete dead modules.

---

## 2. Data Model & Database Migration

### 2.1 Alembic Migration (`0011_remove_fields_and_outcomes.py`)
- Drop table `outcomes` (along with check constraints, foreign keys, and indexes).
- Drop table `fields` (along with check constraints, foreign keys, and indexes).
- Drop table `answers` (along with check constraints, foreign keys, and indexes).

### 2.2 Agent Model (`backend/app/agents.py`)
- The `Agent` entity retains:
  - `id`: UUID primary key
  - `owner_id`: UUID foreign key to `users.id`
  - `name`: string
  - `slug`: unique string
  - `description`: optional string
  - `instructions`: text (the complete agent prompt)
  - `stt_provider`, `stt_model`
  - `llm_provider`, `llm_model`
  - `tts_provider`, `tts_model`, `voice_id`
  - `languages`, `default_language`
  - `status`: "draft" | "published" | "suspended"
- Publish validation:
  - An agent requires `instructions.strip() != ""` to be published (plus configured providers). No steps or fields required.

### 2.3 Sessions & Snapshots (`backend/app/sessions.py`)
- The `FrozenCallSnapshot` schema:
  ```json
  {
    "agent": {
      "name": "string",
      "instructions": "string",
      "voice_id": "string",
      "llm_model": "string"
    },
    "global_prompt": "string",
    "policy": {
      "max_duration_seconds": 600,
      "max_concurrency": 5
    }
  }
  ```
- Remove references to `fields`, `outcomes`, or `steps` in `freeze_call_snapshot`.
- Remove `/sessions/{id}/answers` endpoint.
- Keep `/sessions/{id}/messages` and `/sessions/{id}/trace`.

### 2.4 Deleted Backend Modules
- Remove `backend/app/fields.py`.
- Remove `backend/app/goals.py`.
- Remove `backend/tests/test_fields.py`.
- Remove fields/outcomes router inclusions from `backend/app/main.py`.

---

## 3. Runtime Engine (Voice & Chat)

### 3.1 Voice Runtime (`backend/app/voice.py`)
- Remove all goal evaluation imports (`goals.py`), `save_answers` tool schemas, and outcome state logic.
- The system message sent to the LLM consists of:
  1. Global admin prompt & safety policy (from snapshot).
  2. Agent's complete prompt instructions (from snapshot).
- **Turn Flow**:
  1. Caller speech -> Deepgram STT transcribes to text.
  2. Transcription appended to recent conversation history.
  3. Single-pass LLM invocation: OpenAI generates streaming response directly without tool calls.
  4. Sentence aggregator streams text into Cartesia / ElevenLabs TTS -> Opus audio frames over WebRTC.
  5. Asynchronous persistence queue writes messages to `messages` table.

### 3.2 Chat Test Runtime (`backend/app/chat.py`)
- Remove tool execution and answer extraction.
- On incoming user message:
  1. Record user message in history.
  2. Stream OpenAI chat completions with the unified system prompt directly back over WebSocket as token deltas.
  3. Record assistant message in database.

### 3.3 AI Agent Generator (`backend/app/agent_generator.py`)
- System prompt instructs model to output a single JSON object:
  ```json
  {
    "name": "Short agent name",
    "description": "Short description",
    "instructions": "Comprehensive prompt covering persona, conversational flow, questions to ask, rules, and closing"
  }
  ```
- Creates the `Agent` record directly with `instructions`. No field or outcome generation.

---

## 4. Frontend Interface

### 4.1 Agent Editor Page (`frontend/app/agents/[id]/page.tsx`)
- Delete `goal-editor.tsx`.
- The agent editor interface has a clean 2-column or tabbed layout:
  - **Left / Main Column**:
    - **Agent Prompt**: Prominent, auto-expanding textarea with clear label, character count, and helpful placeholder guidance.
    - Autosaves via existing `useDraftAutosave`.
  - **Right Column / Collapsible Sidebar**:
    - **Voice & Model Configuration**: Speech voice picker (with audio playback sample), STT/LLM provider selectors, agent language, and name/description.
    - **Interactive Test Panel (`agent-testing.tsx`)**:
      - Voice test call (WebRTC)
      - Text test chat (WebSocket)
      - Removed the obsolete "Collected Answers" card.

### 4.2 Clean Type Definitions (`frontend/lib/types.ts`)
- Remove `AgentField`, `AgentOutcome`, `FieldCondition`, `StepBranch`, `AgentStep`, etc.
- Retain `Agent`, `CallSession`, `CallMessage`, `ProviderCatalog`.

---

## 5. Testing & Verification

1. **Alembic Migration**:
   - `alembic upgrade head` executes cleanly; drops `fields`, `outcomes`, and `answers` tables.
2. **Backend Unit & Integration Tests**:
   - Update `test_agents.py`, `test_admin.py`, `test_voice.py`, `test_chat.py`, `test_agent_generator.py`, `test_sessions.py`.
   - Ensure all tests pass.
3. **Frontend Production Build**:
   - `npm run build` succeeds with zero TypeScript errors.
4. **End-to-End Smoke Test**:
   - Start an agent with a single prompt.
   - Run a test chat conversation via WebSocket.
   - Verify speech synthesis and conversation flow.

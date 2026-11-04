import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { PlatformApiClient } from "./platform-api-client.ts";
import { PlatformAgentClient } from "./platform-agent-client.ts";

const originalFetch = globalThis.fetch;
const stamp = "2026-10-30T12:00:00Z";
const agent = {
  id: "00000000-0000-0000-0000-000000000001",
  name: "Loan guide",
  description: "Helps callers check loan eligibility.",
  persona_prompt: "Be clear and concise.",
  enabled: false,
  draft_revision: 1,
  published_version_id: null,
  created_at: stamp,
  updated_at: stamp,
};
const draft = {
  agent_id: agent.id,
  draft_revision: 1,
  authoring_hash: "a".repeat(64),
  snapshot_hash: "b".repeat(64),
  config: { schema_version: 1, steps: [] },
  validation: { errors: [], warnings: [] },
  updated_at: stamp,
};

function jsonResponse(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

describe("PlatformAgentClient", () => {
  let calls;
  let client;

  beforeEach(() => {
    calls = [];
    client = new PlatformAgentClient(new PlatformApiClient("http://127.0.0.1:8000/api/v1"));
    client.setCsrfToken("csrf-platform-agent");
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  function mockFetch(handler) {
    globalThis.fetch = async (input, init = {}) => {
      calls.push({ url: String(input), init, headers: new Headers(init.headers) });
      return handler(input, init);
    };
  }

  test("loads every owned agent page through the cursor contract", async () => {
    mockFetch(async (url) => {
      const parsed = new URL(String(url));
      if (!parsed.searchParams.has("cursor")) {
        return jsonResponse({ items: [agent], next_cursor: "page/2" });
      }
      return jsonResponse({ items: [], next_cursor: null });
    });

    const result = await client.listAllAgents();

    expect(result).toEqual([agent]);
    expect(calls.map(({ url }) => new URL(url).pathname)).toEqual([
      "/api/v1/agents",
      "/api/v1/agents",
    ]);
    expect(new URL(calls[0].url).searchParams.get("limit")).toBe("100");
    expect(new URL(calls[1].url).searchParams.get("cursor")).toBe("page/2");
    expect(calls.every(({ headers }) => !headers.has("X-CSRF-Token"))).toBe(true);
  });

  test("creates a manual draft with the platform contract and an idempotency key", async () => {
    mockFetch(async (_url, init) => {
      expect(init.method).toBe("POST");
      return jsonResponse(agent, 201);
    });

    await client.createAgent(
      { name: agent.name, description: agent.description, source: "manual" },
      "create-key-1"
    );

    expect(new URL(calls[0].url).pathname).toBe("/api/v1/agents");
    expect(JSON.parse(calls[0].init.body)).toEqual({
      name: agent.name,
      description: agent.description,
      source: "manual",
    });
    expect(calls[0].headers.get("Idempotency-Key")).toBe("create-key-1");
    expect(calls[0].headers.get("X-CSRF-Token")).toBe("csrf-platform-agent");
    expect(calls[0].init.credentials).toBe("include");
  });

  test("saves a structurally valid draft with compare-and-swap revision", async () => {
    mockFetch(async (_url, init) => {
      expect(init.method).toBe("PATCH");
      return jsonResponse({ ...draft, draft_revision: 2, config: { ...draft.config, steps: [{ id: "ask_name", type: "ask" }] } });
    });

    const result = await client.patchDraft(agent.id, {
      expected_revision: 1,
      config: { ...draft.config, steps: [{ id: "ask_name", type: "ask" }] },
    });

    expect(result.draft_revision).toBe(2);
    expect(JSON.parse(calls[0].init.body)).toEqual({
      expected_revision: 1,
      config: { schema_version: 1, steps: [{ id: "ask_name", type: "ask" }] },
    });
    expect(calls[0].headers.get("X-CSRF-Token")).toBe("csrf-platform-agent");
  });
  test("accepts the server-derived snapshot from GET and PATCH without computing it", async () => {
    const serverSnapshotHash = "c".repeat(64);
    mockFetch(async () => jsonResponse({ ...draft, snapshot_hash: serverSnapshotHash }));

    const loaded = await client.getDraft(agent.id);
    const saved = await client.patchDraft(agent.id, {
      expected_revision: 1,
      config: draft.config,
    });

    expect(loaded.snapshot_hash).toBe(serverSnapshotHash);
    expect(saved.snapshot_hash).toBe(serverSnapshotHash);
    expect(JSON.parse(calls[1].init.body)).not.toHaveProperty("snapshot_hash");
  });

  test("preserves a null server snapshot for a draft that cannot be tested", async () => {
    mockFetch(async () => jsonResponse({ ...draft, snapshot_hash: null }));

    const result = await client.getDraft(agent.id);

    expect(result.snapshot_hash).toBeNull();
    expect(new URL(calls[0].url).pathname).toBe(`/api/v1/agents/${agent.id}/draft`);
  });

  test("loads every template page before presenting the catalog", async () => {
    const first = {
      id: "template-1",
      slug: "loan-guide",
      version: 1,
      name: "Loan guide",
      description: "A verified eligibility guide.",
      created_at: "2025-01-01T00:00:00Z",
    };
    const second = { ...first, id: "template-2", slug: "loan-guide", version: 2 };
    mockFetch(async (url) => {
      if (new URL(url).searchParams.get("cursor") === "next-page") {
        return jsonResponse({ items: [second], next_cursor: null });
      }
      return jsonResponse({ items: [first], next_cursor: "next-page" });
    });

    const result = await client.listAllTemplates();

    expect(result.map((template) => template.id)).toEqual(["template-1", "template-2"]);
    expect(new URL(calls[1].url).searchParams.get("cursor")).toBe("next-page");
  });




  test("rejects catalog API errors instead of returning an empty catalog", async () => {
    mockFetch(async () => jsonResponse({
      error: {
        code: "CATALOG_UNAVAILABLE",
        message: "Qualified catalog is unavailable.",
      },
    }, 503));

    await expect(client.getCatalog()).rejects.toMatchObject({
      status: 503,
      code: "CATALOG_UNAVAILABLE",
    });
  });

  test("rejects malformed catalog responses instead of dropping catalog entries", async () => {
    mockFetch(async () => jsonResponse({ llm_models: [] }));

    await expect(client.getCatalog()).rejects.toThrow(
      "Invalid platform response: catalog.stt_providers must be an array."
    );
  });

  test("rejects catalog providers with malformed model lists", async () => {
    mockFetch(async () => jsonResponse({
      llm_models: [],
      stt_providers: [{ id: "stt", name: "Speech", models: null }],
      tts_voices: [],
      tts_providers: [],
    }));

    await expect(client.getCatalog()).rejects.toThrow(
      "Invalid platform response: catalog.stt_provider.models must be an array."
    );
  });

  test("submits a generation job, loads the typed proposal, and does not create an agent", async () => {
    const proposal = {
      name: "Loan guide",
      description: "Eligibility guidance.",
      persona_prompt: "Ask only for confirmed facts.",
      config: { flow: { schema_version: 1, steps: [] }, llm_model: "catalog-model" },
    };
    mockFetch(async (url, init) => {
      if (String(url).endsWith("/agents/generate")) {
        return jsonResponse({ job_id: "00000000-0000-0000-0000-000000000002", status: "QUEUED" }, 202);
      }
      return jsonResponse({
        status: "COMPLETED",
        proposal,
        unknown_business_facts: ["Confirm the minimum age."],
        validation_warnings: [],
        usage: { prompt_tokens: 10, completion_tokens: 20, total_tokens: 30 },
      });
    });

    const job = await client.startGenerationJob({
      requirement: "Help callers understand loan eligibility.",
      generation_model_id: "generation-model",
      token_budget: 1024,
    });
    const status = await client.getGenerationJob(job.job_id);

    expect(job.status).toBe("QUEUED");
    expect(status.proposal).toEqual(proposal);
    expect(status.unknown_business_facts).toEqual(["Confirm the minimum age."]);
    expect(new URL(calls[0].url).pathname).toBe("/api/v1/agents/generate");
    expect(JSON.parse(calls[0].init.body)).toEqual({
      requirement: "Help callers understand loan eligibility.",
      generation_model_id: "generation-model",
      token_budget: 1024,
    });
    expect(new URL(calls[1].url).pathname).toBe(
      "/api/v1/generation-jobs/00000000-0000-0000-0000-000000000002"
    );
    expect(calls.every(({ url }) => !new URL(url).pathname.endsWith("/agents"))).toBe(true);
  });

  test("accepts generation usage omitted by the provider", async () => {
    const jobId = "00000000-0000-0000-0000-000000000002";
    mockFetch(async () => jsonResponse({
      status: "CANCELLED",
      proposal: null,
      unknown_business_facts: [],
      validation_warnings: [],
      usage: null,
    }));

    const result = await client.getGenerationJob(jobId);

    expect(result.usage).toBeNull();
  });

  test("accepts null token counts when the provider reports unknown generation usage", async () => {
    const jobId = "00000000-0000-0000-0000-000000000002";
    mockFetch(async () => jsonResponse({
      status: "CANCELLED",
      proposal: null,
      unknown_business_facts: [],
      validation_warnings: [],
      usage: { prompt_tokens: null, completion_tokens: null, total_tokens: null },
    }));

    const result = await client.getGenerationJob(jobId);

    expect(result.usage).toEqual({
      prompt_tokens: null,
      completion_tokens: null,
      total_tokens: null,
    });
  });

  test("keeps an owned generation job active while provider cancellation is pending", async () => {
    const jobId = "00000000-0000-0000-0000-000000000002";
    mockFetch(async (_url, init) => {
      expect(init.method).toBe("POST");
      return jsonResponse({ job_id: jobId, status: "CANCELLING" });
    });

    const result = await client.cancelGenerationJob(jobId);

    expect(result).toEqual({ job_id: jobId, status: "CANCELLING" });
    expect(new URL(calls[0].url).pathname).toBe(
      `/api/v1/generation-jobs/${jobId}/cancel`
    );
    expect(JSON.parse(calls[0].init.body)).toEqual({});
    expect(calls[0].headers.get("X-CSRF-Token")).toBe("csrf-platform-agent");
  });


  test("pins chat turns by snapshot and sequence and can recover the committed transcript", async () => {
    const testId = "00000000-0000-0000-0000-000000000003";
    mockFetch(async (url, init) => {
      if (init.method === "POST") {
        return jsonResponse({
          test_id: testId,
          turn_sequence: 1,
          reply: "I can help with that.",
          flow_state: { active_step_id: "ask_name", outcome: "in_progress" },
          trace: [],
          outcome: "in_progress",
        });
      }
      return jsonResponse({
        test_id: testId,
        snapshot_hash: draft.snapshot_hash,
        turn_sequence: 1,
        messages: [
          { id: "user-1", turn_sequence: 1, role: "user", content: "What do you need?", created_at: stamp },
          { id: "assistant-1", turn_sequence: 1, role: "assistant", content: "I can help with that.", created_at: stamp },
        ],
        flow_state: { active_step_id: "ask_name", outcome: "in_progress" },
        pending_turn_sequence: null,
        trace: [],
        outcome: "in_progress",
      });
    });

    const turn = await client.sendChatTurn(agent.id, {
      snapshot_hash: draft.snapshot_hash,
      test_id: testId,
      expected_turn_sequence: 1,
      text: "What do you need?",
    });
    const transcript = await client.getChatTest(agent.id, testId);

    expect(turn.reply).toBe("I can help with that.");
    expect(JSON.parse(calls[0].init.body)).toEqual({
      snapshot_hash: draft.snapshot_hash,
      test_id: testId,
      expected_turn_sequence: 1,
      text: "What do you need?",
    });
    expect(new URL(calls[0].url).pathname).toBe(`/api/v1/agents/${agent.id}/test/chat`);
    expect(new URL(calls[1].url).pathname).toBe(`/api/v1/agents/${agent.id}/test/chat/${testId}`);
    expect(transcript.messages.map((message) => message.role)).toEqual(["user", "assistant"]);
    expect(transcript.snapshot_hash).toBe(draft.snapshot_hash);
  });
  test("recovers a persisted pending user message at its original sequence", async () => {
    const testId = "00000000-0000-0000-0000-000000000004";
    const pendingText = "Please keep checking the same pending turn.";
    mockFetch(async (_url, init) => {
      if (init.method === "POST") {
        return jsonResponse({
          test_id: testId,
          turn_sequence: 2,
          reply: "The turn resumed.",
          flow_state: {},
          trace: [],
          outcome: "in_progress",
        });
      }
      return jsonResponse({
        test_id: testId,
        snapshot_hash: draft.snapshot_hash,
        turn_sequence: 1,
        pending_turn_sequence: 2,
        messages: [
          { id: "user-pending", turn_sequence: 2, role: "user", content: pendingText, created_at: stamp },
        ],
        flow_state: {},
        trace: [],
        outcome: "in_progress",
      });
    });

    const transcript = await client.getChatTest(agent.id, testId);
    const pendingMessage = transcript.messages.find((message) =>
      message.role === "user" && message.turn_sequence === transcript.pending_turn_sequence
    );
    expect(transcript.pending_turn_sequence).toBe(2);
    expect(pendingMessage?.content).toBe(pendingText);

    await client.sendChatTurn(agent.id, {
      snapshot_hash: transcript.snapshot_hash,
      test_id: transcript.test_id,
      expected_turn_sequence: transcript.pending_turn_sequence,
      text: pendingMessage.content,
    });

    expect(JSON.parse(calls[1].init.body)).toEqual({
      snapshot_hash: draft.snapshot_hash,
      test_id: testId,
      expected_turn_sequence: 2,
      text: pendingText,
    });
  });

  test("preserves typed API conflicts for the workspace to resolve", async () => {
    mockFetch(async () => jsonResponse({
      error: {
        code: "REVISION_CONFLICT",
        message: "The draft changed in another session.",
        request_id: "request-1",
        retryable: false,
        fields: [],
      },
    }, 409));

    await expect(client.patchDraft(agent.id, {
      expected_revision: 1,
      config: draft.config,
    })).rejects.toMatchObject({
      status: 409,
      code: "REVISION_CONFLICT",
      requestId: "request-1",
    });
    expect(calls).toHaveLength(1);
  });
});

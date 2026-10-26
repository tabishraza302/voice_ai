import { afterEach, expect, test } from "bun:test";
import { ApiClient } from "./api-client.ts";

const originalFetch = globalThis.fetch;

const agent = {
  id: "agent-1",
  slug: "assistant",
  name: "Assistant",
  description: "A test agent",
  persona_prompt: "Be concise.",
  llm_model: "local-model:4b",
  llm_temperature: 0.6,
  llm_reasoning_effort: "none",
  llm_max_tokens: 256,
  stt_provider: "mlx-audio",
  tts_voice_en: "af_heart",
  tts_voice_hi: "hf_alpha",
  tts_speed: 1,
  is_active: true,
  is_preset: false,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function mockJsonResponses(...payloads) {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    calls.push({ url: String(input), init });
    const payload = payloads.shift();
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  return calls;
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("agent create, update, and activate use their public API methods and payloads", async () => {
  const client = new ApiClient("http://voice-agent.test/api/");
  const created = { ...agent, id: "new-agent", is_active: false };
  const calls = mockJsonResponses(created, { ...created, name: "Updated" }, agent);

  await client.createAgent({ name: "New", slug: "new", persona_prompt: "Help." });
  await client.updateAgent("agent-1", { name: "Updated", llm_max_tokens: 400 });
  await client.activateAgent("agent-1");

  expect(calls.map(({ url, init }) => [new URL(url).pathname, init.method])).toEqual([
    ["/api/agents", "POST"],
    ["/api/agents/agent-1", "PUT"],
    ["/api/agents/agent-1/activate", "POST"],
  ]);
  expect(JSON.parse(calls[0].init.body)).toEqual({
    name: "New",
    slug: "new",
    persona_prompt: "Help.",
  });
  expect(JSON.parse(calls[1].init.body)).toEqual({ name: "Updated", llm_max_tokens: 400 });
  expect(calls[2].init.body).toBeUndefined();
});

test("prompt save and preview send the expected API bodies", async () => {
  const client = new ApiClient("http://voice-agent.test/api");
  const basePrompt = { prompt_text: "Speak naturally." };
  const preview = { compiled_prompt: "compiled", token_estimate: 12 };
  const calls = mockJsonResponses(basePrompt, preview);

  await client.updateBasePrompt("Speak naturally.", "Tone update");
  await client.previewPrompt({
    base_prompt: "Base",
    persona_prompt: "Persona",
    variables: { language: "Hindi" },
  });

  expect(calls.map(({ url, init }) => [new URL(url).pathname, init.method])).toEqual([
    ["/api/prompts/base", "PUT"],
    ["/api/prompts/preview", "POST"],
  ]);
  expect(JSON.parse(calls[0].init.body)).toEqual({
    prompt_text: "Speak naturally.",
    content: "Speak naturally.",
    change_reason: "Tone update",
    change_summary: "Tone update",
  });
  expect(JSON.parse(calls[1].init.body)).toEqual({
    base_prompt: "Base",
    persona_prompt: "Persona",
    variables: { language: "Hindi" },
  });
});

test("delete returns the server's deletion result", async () => {
  const client = new ApiClient("http://voice-agent.test/api");
  const calls = mockJsonResponses({ status: "deleted", id: "agent-1" });

  await expect(client.deleteAgent("agent-1")).resolves.toEqual({
    status: "deleted",
    id: "agent-1",
  });
  expect(new URL(calls[0].url).pathname).toBe("/api/agents/agent-1");
  expect(calls[0].init.method).toBe("DELETE");
});

test("non-success responses reject with the HTTP status and response body", async () => {
  const client = new ApiClient("http://voice-agent.test/api");
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: "Preset agents cannot be deleted." }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    });

  await expect(client.deleteAgent("preset-1")).rejects.toThrow(
    'API Error [409]: {"detail":"Preset agents cannot be deleted."}',
  );
});

test("clone fits backend name and slug limits while preserving profile settings", async () => {
  const client = new ApiClient("http://voice-agent.test/api");
  const longAgent = {
    ...agent,
    slug: "s".repeat(64),
    name: "N".repeat(128),
  };
  const calls = mockJsonResponses(longAgent, { ...longAgent, id: "clone-1" });

  await client.cloneAgent(longAgent.id);

  const clone = JSON.parse(calls[1].init.body);
  expect(calls.map(({ url, init }) => [new URL(url).pathname, init.method])).toEqual([
    ["/api/agents/agent-1", undefined],
    ["/api/agents", "POST"],
  ]);
  expect(clone.name.length).toBeLessThanOrEqual(128);
  expect(clone.name.endsWith(" (Copy)")).toBe(true);
  expect(clone.slug.length).toBeLessThanOrEqual(64);
  expect(clone.slug.startsWith("s".repeat(57))).toBe(true);
  expect(clone).toMatchObject({
    description: longAgent.description,
    persona_prompt: longAgent.persona_prompt,
    llm_model: longAgent.llm_model,
    llm_temperature: longAgent.llm_temperature,
    llm_reasoning_effort: longAgent.llm_reasoning_effort,
    llm_max_tokens: longAgent.llm_max_tokens,
    stt_provider: longAgent.stt_provider,
    tts_voice_en: longAgent.tts_voice_en,
    tts_voice_hi: longAgent.tts_voice_hi,
    tts_speed: longAgent.tts_speed,
    is_active: false,
  });
});

test("clone keeps a provided short name", async () => {
  const client = new ApiClient("http://voice-agent.test/api");
  const calls = mockJsonResponses(agent, { ...agent, id: "clone-2", name: "Quick helper" });

  await client.cloneAgent(agent.id, "Quick helper");

  expect(JSON.parse(calls[1].init.body).name).toBe("Quick helper");
});

test("the client completes a real fetch roundtrip with a disposable local server", async () => {
  const requests = [];
  const server = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    fetch(request) {
      requests.push({ method: request.method, pathname: new URL(request.url).pathname });
      return Response.json([agent]);
    },
  });

  try {
    const client = new ApiClient(`http://127.0.0.1:${server.port}/api`);
    await expect(client.getAgents()).resolves.toEqual([agent]);
    expect(requests).toEqual([{ method: "GET", pathname: "/api/agents" }]);
  } finally {
    server.stop(true);
  }
});

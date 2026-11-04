import { describe, expect, test } from "bun:test";
import {
  configWithFlow,
  deriveFlowGraph,
  getFlowFromConfig,
  getVisibleAgents,
  summarizeRuntimeSettings,
} from "./platform-agent-model.ts";

const baseAgent = {
  id: "agent-1",
  description: "Helps with loan eligibility.",
  persona_prompt: "Be helpful.",
  enabled: false,
  draft_revision: 1,
  published_version_id: null,
  created_at: "2026-10-01T12:00:00Z",
  updated_at: "2026-10-01T12:00:00Z",
};

const flow = {
  schema_version: 1,
  entry_step_id: "welcome",
  exit_step_id: "finish",
  steps: [
    {
      id: "welcome",
      title: "Welcome",
      type: "ask",
      transitions: [
        { when: { operator: "is_present", field: "name" }, to: "finish" },
        { when: { operator: "else" }, to: "missing_target" },
      ],
    },
    { id: "finish", title: "Finish", type: "end" },
  ],
};

describe("platform agent authoring model", () => {
  test("preserves optional LLM settings outside bare and enveloped flow snapshots", () => {
    const bare = {
      ...flow,
      llm_provider: "ollama",
      llm_model: "runtime-model",
      llm_temperature: 0.4,
      llm_max_tokens: 777,
      stt_model: "speech-model",
      tts_provider: "speech-provider",
      tts_model: "voice-model",
    };
    const enveloped = {
      flow,
      llm_model: "runtime-model",
      llm_temperature: 0.7,
      llm_max_tokens: 2048,
      tts_voice_en: "voice-en",
    };

    expect(getFlowFromConfig(bare)).toEqual(flow);
    expect(getFlowFromConfig(enveloped)).toEqual(flow);
    expect(configWithFlow(bare, { ...flow, steps: [] })).toEqual({
      schema_version: 1,
      entry_step_id: "welcome",
      exit_step_id: "finish",
      steps: [],
      llm_provider: "ollama",
      llm_model: "runtime-model",
      llm_temperature: 0.4,
      llm_max_tokens: 777,
      stt_model: "speech-model",
      tts_provider: "speech-provider",
      tts_model: "voice-model",
    });
    expect(configWithFlow(enveloped, { ...flow, steps: [] })).toEqual({
      flow: { ...flow, steps: [] },
      llm_model: "runtime-model",
      llm_temperature: 0.7,
      llm_max_tokens: 2048,
      tts_voice_en: "voice-en",
    });
    expect(bare.steps).toHaveLength(2);
  });

  test("derives a stable graph from flow step and transition records", () => {
    const graph = deriveFlowGraph(flow);

    expect(graph.nodes.map((node) => [node.id, node.title, node.isEntry, node.isExit])).toEqual([
      ["welcome", "Welcome", true, false],
      ["finish", "Finish", false, true],
    ]);
    expect(graph.edges).toEqual([
      { fromId: "welcome", toId: "finish", label: "is_present · name", targetExists: true },
      { fromId: "welcome", toId: "missing_target", label: "else", targetExists: false },
    ]);
  });

  test("searches, filters, sorts, and paginates the owned agent records", () => {
    const agents = [
      { ...baseAgent, id: "agent-c", name: "Cedar", enabled: true, updated_at: "2026-10-02T12:00:00Z" },
      { ...baseAgent, id: "agent-a", name: "Amber", enabled: false, published_version_id: "version-a", updated_at: "2026-10-03T12:00:00Z" },
      { ...baseAgent, id: "agent-b", name: "Birch", enabled: false, updated_at: "2026-10-04T12:00:00Z" },
    ];

    expect(getVisibleAgents(agents, {
      query: "loan",
      enabled: "disabled",
      publication: "unpublished",
      sort: "name",
      page: 1,
      pageSize: 1,
    })).toMatchObject({ items: [{ id: "agent-b" }], totalItems: 1, page: 1, pageCount: 1 });

    expect(getVisibleAgents(agents, {
      query: "",
      enabled: "all",
      publication: "all",
      sort: "updated",
      page: 2,
      pageSize: 1,
    })).toMatchObject({ items: [{ id: "agent-a" }], totalItems: 3, page: 2, pageCount: 3 });
  });

  test("reports only runtime settings the saved config actually supplies", () => {
    expect(summarizeRuntimeSettings({ flow })).toEqual({
      llmModel: null,
      sttProvider: null,
      voices: null,
    });
    expect(summarizeRuntimeSettings({
      flow,
      llm_model: "qualified-model",
      stt_provider: "qualified-stt",
      tts_voice_en: "voice-en",
      tts_voice_hi: "voice-hi",
    })).toEqual({
      llmModel: "qualified-model",
      sttProvider: "qualified-stt",
      voices: "voice-en / voice-hi",
    });
  });
});

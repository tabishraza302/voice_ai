import { describe, expect, test } from "bun:test";
import { SETTINGS, getString, formatDate } from "./agent-workspace-constants.ts";

describe("agent-workspace-constants", () => {
  test("SETTINGS defines expected runtime keys in order", () => {
    expect(SETTINGS.map((s) => s.key)).toEqual([
      "llm_model",
      "stt_provider",
      "tts_voice_en",
      "tts_voice_hi",
    ]);
  });

  test("getString returns string value or empty string", () => {
    const config = {
      llm_model: "test-model",
      stt_provider: 123,
    };
    expect(getString(config, "llm_model")).toBe("test-model");
    expect(getString(config, "stt_provider")).toBe("");
    expect(getString(config, "tts_voice_en")).toBe("");
  });

  test("formatDate formats valid dates and handles invalid inputs", () => {
    expect(formatDate("2026-11-01T12:00:00Z")).not.toBe("Date unavailable");
    expect(formatDate("invalid-date")).toBe("Date unavailable");
  });

});

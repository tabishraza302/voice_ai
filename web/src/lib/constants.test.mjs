import { describe, expect, test } from "bun:test";
import { DEFAULT_CATALOG, MAX_TOKENS, SPEED, TEMPERATURE } from "./constants.ts";

// Independent source of truth: bot/agents/schemas.py.
//   SUPPORTED_STT_PROVIDERS = {"mlx-audio", "mlx-audio-realtime", "mlx-whisper"}
//   llm_temperature: ge=0.0, le=2.0
//   llm_max_tokens:  ge=1,   le=4096
//   tts_speed:       ge=0.5, le=2.0
// The fallback catalog stands in for /models when the API is unreachable, so its
// ids must be exactly the ones the backend will accept on save.

describe("fallback STT catalog", () => {
  test("offers exactly the providers the backend accepts", () => {
    const ids = DEFAULT_CATALOG.stt_providers.map((provider) => provider.id);

    expect(new Set(ids)).toEqual(new Set(["mlx-audio-realtime", "mlx-audio", "mlx-whisper"]));
    expect(ids).toHaveLength(3);
  });

  test("describes every offered provider", () => {
    for (const provider of DEFAULT_CATALOG.stt_providers) {
      expect(provider.name.trim()).not.toBe("");
      expect(provider.description.trim()).not.toBe("");
    }
  });
});

describe("editor ranges match the backend bounds", () => {
  test("tts speed", () => {
    expect([SPEED.min, SPEED.max, SPEED.step]).toEqual([0.5, 2.0, 0.05]);
  });

  test("llm temperature", () => {
    expect([TEMPERATURE.min, TEMPERATURE.max, TEMPERATURE.step]).toEqual([0, 2.0, 0.05]);
  });

  test("llm max tokens", () => {
    expect([MAX_TOKENS.min, MAX_TOKENS.max, MAX_TOKENS.step]).toEqual([1, 4096, 1]);
  });
});

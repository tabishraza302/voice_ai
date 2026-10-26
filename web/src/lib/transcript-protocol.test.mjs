import { describe, expect, test } from "bun:test";
import {
  appendTranscriptLine,
  joinTranscriptText,
  transcriptFromDataChannel,
} from "./transcript-protocol.ts";

function message(payload) {
  return JSON.stringify(payload);
}

describe("transcriptFromDataChannel", () => {
  test("accepts the project's completed user transcript", () => {
    expect(
      transcriptFromDataChannel(
        message({ type: "transcript", role: "user", text: "How can I help you today?" })
      )
    ).toEqual({ role: "user", text: "How can I help you today?" });
  });

  test("accepts the project's completed assistant transcript and trims it", () => {
    expect(
      transcriptFromDataChannel(
        message({ type: "transcript", role: "assistant", text: "  Yes, I can hear you.  " })
      )
    ).toEqual({ role: "assistant", text: "Yes, I can hear you." });
  });

  test("ignores the streamed LLM token messages that split a sentence", () => {
    for (const text of ["How", " can", " I", " help", " you", " today", "?"]) {
      expect(
        transcriptFromDataChannel(
          message({ label: "rtvi-ai", type: "bot-llm-text", data: { text } })
        )
      ).toBeNull();
    }
  });

  test("ignores Pipecat's other representations of the same completed turn", () => {
    const duplicates = [
      { label: "rtvi-ai", type: "bot-transcription", data: { text: "How can I help you today?" } },
      {
        label: "rtvi-ai",
        type: "bot-output",
        data: { text: "How can I help you today?", spoken: true, spoken_status: "completed" },
      },
      { label: "rtvi-ai", type: "bot-tts-text", data: { text: "How can I help you today?" } },
      { label: "rtvi-ai", type: "user-transcription", data: { text: "How can I help you today?", final: true } },
      { label: "rtvi-ai", type: "user-llm-text", data: { text: "How can I help you today?" } },
      { label: "rtvi-ai", type: "user-input", data: { text: "How can I help you today?" } },
    ];

    for (const payload of duplicates) {
      expect(transcriptFromDataChannel(message(payload))).toBeNull();
    }
  });

  test("ignores malformed, incomplete and non-text payloads", () => {
    const invalid = [
      "not json",
      "",
      "null",
      "[]",
      message({ type: "transcript" }),
      message({ type: "transcript", role: "system", text: "Hello" }),
      message({ type: "transcript", role: "assistant" }),
      message({ type: "transcript", role: "assistant", text: "   " }),
      message({ type: "transcript", role: "assistant", text: 42 }),
      message({ role: "assistant", text: "Hello" }),
      null,
      undefined,
    ];

    for (const payload of invalid) {
      expect(transcriptFromDataChannel(payload)).toBeNull();
    }
  });
});

describe("joinTranscriptText", () => {
  test("joins consecutive spoken sentences with one space", () => {
    expect(joinTranscriptText("Yes, I can hear you.", "How can I help you today?")).toBe(
      "Yes, I can hear you. How can I help you today?"
    );
  });

  test("normalizes whitespace at the boundary without touching the text", () => {
    expect(joinTranscriptText("First sentence.  ", "  Second sentence.")).toBe(
      "First sentence. Second sentence."
    );
    expect(joinTranscriptText("Line one\n", "\nLine two")).toBe("Line one Line two");
  });

  test("returns the non-empty side when the other side is blank", () => {
    expect(joinTranscriptText("", "Hello")).toBe("Hello");
    expect(joinTranscriptText("Hello", "   ")).toBe("Hello");
    expect(joinTranscriptText("", "")).toBe("");
  });
});

describe("appendTranscriptLine", () => {
  const user = { id: "u1", role: "user", text: "Hello", time: "01:00:00" };
  const first = { id: "a1", role: "assistant", text: "Yes, I can hear you.", time: "01:00:01" };
  const second = { id: "a2", role: "assistant", text: "How can I help you today?", time: "01:00:02" };

  test("merges consecutive assistant segments into one turn", () => {
    const lines = appendTranscriptLine([user, first], second);

    expect(lines).toEqual([
      user,
      {
        id: "a1",
        role: "assistant",
        text: "Yes, I can hear you. How can I help you today?",
        time: "01:00:01",
      },
    ]);
  });

  test("starts a new assistant turn after the user speaks again", () => {
    const nextUser = { id: "u2", role: "user", text: "Thanks", time: "01:00:03" };
    const lines = appendTranscriptLine([user, first, nextUser], second);

    expect(lines.at(-1)).toEqual(second);
    expect(lines).toHaveLength(4);
  });

  test("never merges two user transcripts", () => {
    const nextUser = { id: "u2", role: "user", text: "Again", time: "01:00:02" };
    const lines = appendTranscriptLine([user], nextUser);

    expect(lines).toEqual([user, nextUser]);
  });
});

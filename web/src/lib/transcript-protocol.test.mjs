import { describe, expect, test } from "bun:test";
import {
  appendAssistantTokenLine,
  appendTranscriptLine,
  appendUserPartialLine,
  assistantTokenFromDataChannel,
  classifyUserDeltaAfterFinal,
  dataChannelEventType,
  finishLiveLine,
  joinTranscriptText,
  replaceTranscriptLine,
  transcriptFromDataChannel,
  userPartialFromDataChannel,
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

describe("assistantTokenFromDataChannel", () => {
  test("accepts a streamed token without trimming its leading space", () => {
    expect(
      assistantTokenFromDataChannel(
        message({ label: "rtvi-ai", type: "bot-llm-text", data: { text: " ready" } })
      )
    ).toBe(" ready");
  });

  test("reconstructs a correctly spaced reply from its token deltas", () => {
    const tokens = [
      "I",
      "'m",
      " ready",
      " to",
      " chat",
      " or",
      " help",
      " with",
      " anything",
      " you",
      " need",
      "!",
    ];

    const reconstructed = tokens
      .map((text) =>
        assistantTokenFromDataChannel(
          message({ label: "rtvi-ai", type: "bot-llm-text", data: { text } })
        )
      )
      .join("");

    expect(reconstructed).toBe("I'm ready to chat or help with anything you need!");
  });

  test("accepts a top-level text field for non-RTVI senders", () => {
    expect(
      assistantTokenFromDataChannel(message({ type: "bot-llm-text", text: "Hello" }))
    ).toBe("Hello");
  });

  test("ignores completed representations, other events and malformed payloads", () => {
    const ignored = [
      "not json",
      "",
      "null",
      "[]",
      message({ type: "transcript", role: "assistant", text: "Hello" }),
      message({ label: "rtvi-ai", type: "bot-transcription", data: { text: "Hello" } }),
      message({ label: "rtvi-ai", type: "bot-output", data: { text: "Hello" } }),
      message({ label: "rtvi-ai", type: "bot-tts-text", data: { text: "Hello" } }),
      message({ label: "rtvi-ai", type: "user-transcription", data: { text: "Hello" } }),
      message({ label: "rtvi-ai", type: "bot-llm-text" }),
      message({ label: "rtvi-ai", type: "bot-llm-text", data: {} }),
      message({ label: "rtvi-ai", type: "bot-llm-text", data: { text: 42 } }),
      message({ label: "rtvi-ai", type: "bot-llm-text", data: { text: "" } }),
      message({ label: "rtvi-ai", type: "bot-llm-text", text: 42 }),
      null,
      undefined,
    ];

    for (const payload of ignored) {
      expect(assistantTokenFromDataChannel(payload)).toBeNull();
    }
  });
});

describe("appendAssistantTokenLine", () => {
  const line = (text, id = "a1", time = "01:00:01") => ({
    id,
    role: "assistant",
    text,
    time,
  });
  const user = { id: "u1", role: "user", text: "Hello", time: "01:00:00" };

  test("creates one assistant bubble and appends every later token in place", () => {
    let lines = [];
    for (const [index, token] of ["I", "'m", " ready", "!"].entries()) {
      lines = appendAssistantTokenLine(lines, line(token, `a${index}`));
    }

    expect(lines).toEqual([
      { id: "a0", role: "assistant", text: "I'm ready!", time: "01:00:01" },
    ]);
  });

  test("adds tokens without inserting a space of its own", () => {
    const lines = appendAssistantTokenLine([line("Hello")], line(" world"));

    expect(lines[0].text).toBe("Hello world");
  });

  test("keeps the first token's id and time while the reply streams", () => {
    const lines = appendAssistantTokenLine(
      [line("First", "a0", "01:00:01")],
      line(" second", "a1", "01:00:02")
    );

    expect(lines).toHaveLength(1);
    expect(lines[0].id).toBe("a0");
    expect(lines[0].time).toBe("01:00:01");
  });

  test("starts a new assistant bubble after a user line", () => {
    const lines = appendAssistantTokenLine([line("Previous"), user], line("Next", "a2", "01:00:03"));

    expect(lines).toHaveLength(3);
    expect(lines.at(-1)).toEqual(line("Next", "a2", "01:00:03"));
  });

  test("ignores an empty token and does not open a bubble for whitespace", () => {
    expect(appendAssistantTokenLine([], line(""))).toEqual([]);
    expect(appendAssistantTokenLine([], line("   "))).toEqual([]);
    expect(appendAssistantTokenLine([user], line(""))).toEqual([user]);
  });

  test("keeps a whitespace token when it separates two spoken words", () => {
    const lines = appendAssistantTokenLine([line("Hello")], line(" ", "a2"));

    expect(lines[0].text).toBe("Hello ");
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

describe("dataChannelEventType", () => {
  test("reads the event type from a JSON data-channel message", () => {
    expect(dataChannelEventType(message({ label: "rtvi-ai", type: "user-started-speaking" }))).toBe(
      "user-started-speaking"
    );
    expect(dataChannelEventType(message({ type: "bot-llm-stopped" }))).toBe("bot-llm-stopped");
  });

  test("ignores malformed and type-less payloads", () => {
    for (const payload of ["not json", "", "null", "[]", message({}), message({ type: 42 }), null, undefined]) {
      expect(dataChannelEventType(payload)).toBeNull();
    }
  });
});

describe("userPartialFromDataChannel", () => {
  test("accepts an interim transcription delta without trimming its leading space", () => {
    expect(
      userPartialFromDataChannel(
        message({
          label: "rtvi-ai",
          type: "user-input",
          data: { text: " can", input_type: "transcription", final: false },
        })
      )
    ).toBe(" can");
  });

  test("reconstructs the running user transcript from realtime deltas", () => {
    const deltas = ["How", "ever", ",", " due", " t", "o", " the", " ", "sl", "ow"];
    const reconstructed = deltas
      .map((text) =>
        userPartialFromDataChannel(
          message({
            label: "rtvi-ai",
            type: "user-input",
            data: { text, input_type: "transcription", final: false },
          })
        )
      )
      .join("");

    expect(reconstructed).toBe("However, due to the slow");
  });

  test("ignores the final copy and duplicate representations of the same turn", () => {
    const ignored = [
      { type: "user-input", data: { text: "Hello", input_type: "transcription", final: true } },
      { type: "user-input", data: { text: "Hello", input_type: "text", final: false } },
      { type: "user-transcription", data: { text: "Hello", final: false } },
      { type: "user-llm-text", data: { text: "Hello" } },
      { type: "transcript", role: "user", text: "Hello" },
      { type: "bot-llm-text", data: { text: "Hello" } },
    ];

    for (const payload of ignored) {
      expect(userPartialFromDataChannel(message({ label: "rtvi-ai", ...payload }))).toBeNull();
    }
  });

  test("ignores malformed, incomplete and empty payloads", () => {
    const ignored = [
      "not json",
      "",
      "null",
      "[]",
      message({ type: "user-input" }),
      message({ type: "user-input", data: {} }),
      message({ type: "user-input", data: { text: "Hello", input_type: "transcription" } }),
      message({ type: "user-input", data: { text: "Hello", final: false } }),
      message({ type: "user-input", data: { text: 42, input_type: "transcription", final: false } }),
      message({ type: "user-input", data: { text: "", input_type: "transcription", final: false } }),
      null,
      undefined,
    ];

    for (const payload of ignored) {
      expect(userPartialFromDataChannel(payload)).toBeNull();
    }
  });
});

describe("appendUserPartialLine", () => {
  const line = (text, id = "u-partial-1", time = "01:00:00") => ({
    id,
    role: "user",
    text,
    time,
    live: true,
  });
  const assistant = { id: "a1", role: "assistant", text: "Hello", time: "00:59:59" };
  const finalUser = { id: "u-final", role: "user", text: "Previous", time: "00:58:00" };

  test("streams every delta into one live user bubble", () => {
    let lines = [];
    for (const [index, delta] of ["How", " can", " I", " help", "?"].entries()) {
      lines = appendUserPartialLine(lines, line(delta, "u-partial-1", `01:00:0${index}`));
    }

    expect(lines).toEqual([line("How can I help?", "u-partial-1", "01:00:00")]);
  });

  test("starts a new bubble for a new utterance id", () => {
    const first = line("First");
    const lines = appendUserPartialLine([first], line("Second", "u-partial-2", "01:00:05"));

    expect(lines).toHaveLength(2);
    expect(lines[1]).toEqual(line("Second", "u-partial-2", "01:00:05"));
  });

  test("starts a new bubble after an assistant line or a finalized user line", () => {
    expect(appendUserPartialLine([assistant], line("Next"))).toEqual([assistant, line("Next")]);
    expect(appendUserPartialLine([finalUser], line("Next"))).toEqual([finalUser, line("Next")]);
  });

  test("ignores an empty or whitespace-only first delta", () => {
    expect(appendUserPartialLine([], line(""))).toEqual([]);
    expect(appendUserPartialLine([], line("   "))).toEqual([]);
  });

  test("keeps a whitespace delta inside the live bubble", () => {
    const lines = appendUserPartialLine([line("Hello")], line(" ", "u-partial-1"));

    expect(lines).toEqual([line("Hello ", "u-partial-1", "01:00:00")]);
  });
});

describe("replaceTranscriptLine", () => {
  const partial = { id: "u1", role: "user", text: "How can I", time: "01:00:00", live: true };
  const assistant = { id: "a1", role: "assistant", text: "Hello", time: "00:59:59" };

  test("replaces the live bubble with the final text and clears the caret", () => {
    const lines = replaceTranscriptLine([assistant, partial], "u1", "How can I help you today?");

    expect(lines).toEqual([
      assistant,
      { id: "u1", role: "user", text: "How can I help you today?", time: "01:00:00", live: false },
    ]);
  });

  test("leaves every other line untouched", () => {
    const lines = replaceTranscriptLine([partial, assistant], "u1", "Final");

    expect(lines?.[1]).toEqual(assistant);
  });

  test("returns null when the line is gone", () => {
    expect(replaceTranscriptLine([assistant], "u1", "Final")).toBeNull();
    expect(replaceTranscriptLine([], "u1", "Final")).toBeNull();
  });
});

describe("finishLiveLine", () => {
  const user = { id: "u1", role: "user", text: "Hello", time: "01:00:00" };
  const liveAssistant = { id: "a1", role: "assistant", text: "Hello", time: "01:00:01", live: true };

  test("clears the caret on the last matching line", () => {
    expect(finishLiveLine([user, liveAssistant], "assistant")).toEqual([
      user,
      { id: "a1", role: "assistant", text: "Hello", time: "01:00:01", live: false },
    ]);
  });

  test("does nothing for another role, an already finished line, or an empty transcript", () => {
    expect(finishLiveLine([user, liveAssistant], "user")).toEqual([user, liveAssistant]);
    expect(finishLiveLine([user], "assistant")).toEqual([user]);
    expect(
      finishLiveLine([{ ...liveAssistant, live: false }], "assistant")
    ).toEqual([{ ...liveAssistant, live: false }]);
    expect(finishLiveLine([], "assistant")).toEqual([]);
  });
});

describe("classifyUserDeltaAfterFinal", () => {
  const final = "However, due to the slow communication channels, styles in the West could lag behind by twenty five to thirty years.";

  test("treats the trailing punctuation and space as part of the finalized turn", () => {
    let live = "However, due to the slow communication channels, styles in the West could lag behind by twenty five to thirty years";

    const period = classifyUserDeltaAfterFinal(live, final, ".");
    expect(period).toEqual({ late: true, liveText: `${live}.` });
    live = period.liveText;

    const space = classifyUserDeltaAfterFinal(live, final, " ");
    expect(space).toEqual({ late: true, liveText: `${live} ` });
  });

  test("accepts the first word of the next turn", () => {
    const live = "However, due to the slow communication channels, styles in the West could lag behind by twenty five to thirty years.";

    expect(classifyUserDeltaAfterFinal(live, final, "Ma")).toEqual({
      late: false,
      liveText: "",
    });
  });

  test("accepts a new turn even when the start event has not arrived", () => {
    expect(classifyUserDeltaAfterFinal("Hello", "Hello there.", " Next")).toEqual({
      late: false,
      liveText: "",
    });
  });

  test("consumes a late delta that extends the live text toward the final", () => {
    const first = classifyUserDeltaAfterFinal("Hello", "Hello there.", " the");
    expect(first).toEqual({ late: true, liveText: "Hello the" });

    const second = classifyUserDeltaAfterFinal(first.liveText, "Hello there.", "re");
    expect(second).toEqual({ late: true, liveText: "Hello there" });

    expect(classifyUserDeltaAfterFinal(second.liveText, "Hello there.", ".")).toEqual({
      late: true,
      liveText: "Hello there.",
    });
  });

  test("treats punctuation-only deltas as late even when the final has no tail", () => {
    expect(classifyUserDeltaAfterFinal("Hello", "Hello", ".")).toEqual({
      late: true,
      liveText: "Hello.",
    });
  });

  test("accepts any delta when the finalized turn never streamed live text", () => {
    expect(classifyUserDeltaAfterFinal("", "Hello", "Next")).toEqual({
      late: false,
      liveText: "",
    });
  });
});

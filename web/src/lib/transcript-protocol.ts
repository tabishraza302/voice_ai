export type TranscriptRole = "user" | "assistant";

export interface CompletedTranscript {
  role: TranscriptRole;
  text: string;
}

/**
 * Reads the project's completed `transcript` message from the voice data
 * channel. The same turn also arrives as streamed `bot-llm-text` tokens,
 * interim `user-input` transcription deltas, and duplicate
 * `bot-transcription` / `bot-output` / `user-transcription` representations.
 * The live parsers below render tokens and deltas once; this completed copy
 * is the final user source and the assistant fallback when no tokens arrived.
 */
export function transcriptFromDataChannel(payload: unknown): CompletedTranscript | null {
  const record = parseDataChannelMessage(payload);
  if (!record || record.type !== "transcript") return null;
  if (record.role !== "user" && record.role !== "assistant") return null;
  if (typeof record.text !== "string") return null;

  const text = record.text.trim();
  if (!text) return null;
  return { role: record.role, text };
}

/** Joins two spoken segments without adding boundary whitespace. */
export function joinTranscriptText(left: string, right: string): string {
  const first = left.trim();
  const second = right.trim();
  if (!first) return second;
  if (!second) return first;
  return `${first} ${second}`;
}

/**
 * Validates a client text turn against the backend utterance character limit.
 * Character count is evaluated using Unicode code points to match backend Python `len(text)`.
 */
export function validateClientUtterance(
  text: string,
  maxChars = 4000
): { valid: true; text: string } | { valid: false; text: string; codePointCount: number; maxChars: number } {
  const trimmed = text.trim();
  const codePointCount = Array.from(trimmed).length;
  if (codePointCount === 0 || codePointCount > maxChars) {
    return { valid: false, text: trimmed, codePointCount, maxChars };
  }
  return { valid: true, text: trimmed };
}

export interface TranscriptLine {
  id: string;
  role: TranscriptRole;
  text: string;
  live?: boolean;
}

function parseDataChannelMessage(payload: unknown): Record<string, unknown> | null {
  if (typeof payload !== "string") return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    return null;
  }

  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  return parsed as Record<string, unknown>;
}

/** Reads the RTVI event type without requiring a text payload. */
export function dataChannelEventType(payload: unknown): string | null {
  const message = parseDataChannelMessage(payload);
  if (!message || typeof message.type !== "string") return null;
  return message.type;
}

/**
 * Reads one streamed assistant token from RTVI's `bot-llm-text` message.
 * The completed project transcript only arrives after TTS has spoken the
 * sentence, so the token stream is what makes the dialog feel realtime.
 * Tokens are returned verbatim: their leading spaces are the word
 * boundaries, and trimming them would glue words together.
 */
export function assistantTokenFromDataChannel(payload: unknown): string | null {
  const record = parseDataChannelMessage(payload);
  if (!record || record.type !== "bot-llm-text") return null;

  let text: unknown;
  if (record.data && typeof record.data === "object" && !Array.isArray(record.data)) {
    text = (record.data as Record<string, unknown>).text;
  }
  if (typeof text !== "string" && typeof record.text === "string") {
    text = record.text;
  }
  if (typeof text !== "string" || text.length === 0) return null;
  return text;
}

/**
 * Reads one interim STT delta from RTVI's `user-input` message. The realtime
 * STT streams text while the user is still speaking; `user-input` is the
 * non-deprecated carrier (`user-transcription` repeats the same data).
 * Deltas are returned verbatim because leading spaces separate words.
 */
export function userPartialFromDataChannel(payload: unknown): string | null {
  const message = parseDataChannelMessage(payload);
  if (!message || message.type !== "user-input") return null;

  const data = message.data;
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const record = data as Record<string, unknown>;
  if (record.input_type !== "transcription") return null;
  if (record.final !== false) return null;
  if (typeof record.text !== "string" || record.text.length === 0) return null;
  return record.text;
}

/**
 * Appends a streamed token to the reply that is already on screen. Every
 * token of one LLM turn belongs to one bubble, so the dialog grows a
 * sentence in place instead of adding a fragment per token. A user line
 * starts a new assistant bubble.
 */
export function appendAssistantTokenLine<T extends TranscriptLine>(
  lines: readonly T[],
  next: T
): T[] {
  if (next.text.length === 0) return [...lines];

  const last = lines[lines.length - 1];
  if (last?.role === "assistant") {
    return [
      ...lines.slice(0, -1),
      { ...last, text: last.text + next.text },
    ];
  }
  if (!next.text.trim()) return [...lines];
  return [...lines, next];
}

/** Appends a streamed interim STT delta to the current user bubble. */
export function appendUserPartialLine<T extends TranscriptLine>(
  lines: readonly T[],
  next: T
): T[] {
  if (next.text.length === 0) return [...lines];

  const last = lines[lines.length - 1];
  if (last?.role === "user" && last.id === next.id) {
    return [
      ...lines.slice(0, -1),
      { ...last, text: last.text + next.text, live: true },
    ];
  }
  if (!next.text.trim()) return [...lines];
  return [...lines, { ...next, live: true }];
}

/** Replaces a live line with its final text and drops the typing caret. */
export function replaceTranscriptLine<T extends TranscriptLine>(
  lines: readonly T[],
  id: string,
  text: string
): T[] | null {
  const index = lines.findIndex((line) => line.id === id);
  if (index === -1) return null;

  const next = [...lines];
  next[index] = { ...next[index], text, live: false };
  return next;
}

/** Removes the typing caret from the last line of the given role. */
export function finishLiveLine<T extends TranscriptLine>(
  lines: readonly T[],
  role: TranscriptRole
): T[] {
  const last = lines[lines.length - 1];
  if (last?.role !== role || !last.live) return [...lines];
  return [...lines.slice(0, -1), { ...last, live: false }];
}

export interface UserDeltaAfterFinal {
  /** True when the delta belongs to the utterance that was just finalized. */
  late: boolean;
  /** The finalized utterance's live text including any consumed late delta. */
  liveText: string;
}

/**
 * Separates the trailing deltas of a finalized user utterance from the first
 * delta of the next turn. `user-started-speaking` cannot do this: the real
 * realtime stream can emit it after the first delta, or even after the final
 * transcript. Late deltas continue the live text toward the final text;
 * anything else starts a new turn. Punctuation and whitespace are always
 * treated as trailing because a user turn never starts with them.
 */
export function classifyUserDeltaAfterFinal(
  liveText: string,
  finalText: string,
  delta: string
): UserDeltaAfterFinal {
  if (!liveText) return { late: false, liveText: "" };

  const candidate = liveText + delta;
  const hasWord = /[\p{L}\p{N}]/u.test(delta);
  if (!hasWord || finalText.trim().startsWith(candidate.trim())) {
    return { late: true, liveText: candidate };
  }
  return { late: false, liveText: "" };
}

/**
 * Adds a completed line to the rendered transcript. Consecutive assistant
 * segments are one spoken reply, not separate turns, so they share one
 * bubble; a user line always starts a new turn.
 */
export function appendTranscriptLine<T extends TranscriptLine>(
  lines: readonly T[],
  next: T
): T[] {
  const last = lines[lines.length - 1];
  if (next.role === "assistant" && last?.role === "assistant") {
    const merged: T = {
      ...last,
      text: joinTranscriptText(last.text, next.text),
    };
    return [...lines.slice(0, -1), merged];
  }
  return [...lines, next];
}

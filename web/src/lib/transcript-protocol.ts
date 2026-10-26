export type TranscriptRole = "user" | "assistant";

export interface CompletedTranscript {
  role: TranscriptRole;
  text: string;
}

/**
 * Reads the project's completed `transcript` message from the voice data
 * channel. Pipecat also sends the same turn as streamed `bot-llm-text`
 * tokens and as `bot-transcription` / `bot-output` / `user-transcription`
 * representations; rendering those as well splits one turn into fragments
 * and repeats it. Only the completed project transcript is authoritative.
 */
export function transcriptFromDataChannel(payload: unknown): CompletedTranscript | null {
  if (typeof payload !== "string") return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    return null;
  }

  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const record = parsed as Record<string, unknown>;
  if (record.type !== "transcript") return null;
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

export interface TranscriptLine {
  id: string;
  role: TranscriptRole;
  text: string;
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

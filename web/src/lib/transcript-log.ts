export type TranscriptRole = "user" | "assistant";

export interface TranscriptEntry {
  id: string;
  role: TranscriptRole;
  text: string;
  at: number;
}

let fallbackIdCounter = 0;

function createId(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") {
    try {
      return cryptoApi.randomUUID();
    } catch {
      // Fall through to the counter for environments that expose but reject it.
    }
  }
  fallbackIdCounter += 1;
  return `transcript-${fallbackIdCounter}`;
}

/**
 * An append-only transcript for one call. Identical `role + text` lines that
 * arrive inside the dedupe window are dropped, because the voice pipeline can
 * emit the same final transcript twice while a turn settles.
 */
export class TranscriptLog {
  private readonly items: TranscriptEntry[] = [];
  private readonly lastAcceptedAt = new Map<string, number>();

  constructor(
    private readonly now: () => number = Date.now,
    private readonly dedupeWindowMs = 1200,
  ) {}

  append(role: TranscriptRole, text: string): TranscriptEntry | null {
    const trimmed = text.trim();
    if (!trimmed) return null;

    const at = this.now();
    const key = `${role}\u0000${trimmed}`;
    const previous = this.lastAcceptedAt.get(key);
    if (previous !== undefined && at - previous < this.dedupeWindowMs) return null;

    const entry: TranscriptEntry = { id: createId(), role, text: trimmed, at };
    this.lastAcceptedAt.set(key, at);
    this.items.push(entry);
    return entry;
  }

  reset(): void {
    this.items.length = 0;
    this.lastAcceptedAt.clear();
  }

  get entries(): readonly TranscriptEntry[] {
    return this.items;
  }
}

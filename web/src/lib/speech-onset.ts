/**
 * A tiny energy-gated speech onset detector for the call dialog.
 *
 * The transcript's clock should be the moment the user started speaking, not
 * the moment the STT server finally emitted text. The backend's VAD event is
 * not on the wire today, so the dialog watches the same microphone stream it
 * sends and records a sustained rise above a low RMS threshold. This is a
 * display clock, not a turn detector: the pipeline still owns turn boundaries.
 *
 * `sustainMs` rejects a click or a single noisy frame. `releaseMs` keeps a
 * short pause between words from starting a second onset in the same turn.
 */
export interface SpeechOnsetTrackerOptions {
  /** Normalized RMS (0..1) that counts as speech. */
  threshold?: number;
  /** How long the level must stay above the threshold before it counts. */
  sustainMs?: number;
  /** How long the level must stay below the threshold before re-arming. */
  releaseMs?: number;
}

export class SpeechOnsetTracker {
  private readonly threshold: number;
  private readonly sustainMs: number;
  private readonly releaseMs: number;
  private aboveSince: number | null = null;
  private quietSince: number | null = null;
  private armed = true;
  private pending: number | null = null;
  private consumed = false;

  constructor(options: SpeechOnsetTrackerOptions = {}) {
    this.threshold = options.threshold ?? 0.015;
    this.sustainMs = options.sustainMs ?? 80;
    this.releaseMs = options.releaseMs ?? 300;
  }

  /**
   * Feed one analyser frame. Returns the onset timestamp when it is first
   * confirmed, or null while the level is too quiet, still being confirmed,
   * or part of an utterance that already fired.
   */
  observe(level: number, atMs: number): number | null {
    if (!Number.isFinite(level) || !Number.isFinite(atMs)) return null;

    // One onset belongs to one turn. Once a bubble has taken it, stay quiet
    // until the turn ends and `reset` arms the next one: a long pause inside
    // the same utterance must not leave an onset for the next turn to inherit.
    if (this.consumed) return null;

    if (this.armed) {
      if (level < this.threshold) {
        this.aboveSince = null;
        return null;
      }
      if (this.aboveSince === null || atMs < this.aboveSince) {
        this.aboveSince = atMs;
      }
      if (atMs - this.aboveSince < this.sustainMs) return null;

      this.armed = false;
      this.quietSince = null;
      this.pending = this.aboveSince;
      return this.pending;
    }

    if (level < this.threshold) {
      if (this.quietSince === null || atMs < this.quietSince) {
        this.quietSince = atMs;
      }
      if (atMs - this.quietSince >= this.releaseMs) {
        this.armed = true;
        this.aboveSince = null;
        this.quietSince = null;
      }
    } else {
      this.quietSince = null;
    }
    return null;
  }

  /** The pending onset, or null. Taking one consumes it until `reset`. */
  take(): number | null {
    const onset = this.pending;
    this.pending = null;
    if (onset !== null) this.consumed = true;
    return onset;
  }

  /**
   * Drop an onset that did not become a turn (for example, a cough) while
   * leaving an ongoing utterance's release state intact.
   */
  clearPending(): void {
    this.pending = null;
  }

  /** Start a new call from a clean slate. */
  reset(): void {
    this.aboveSince = null;
    this.quietSince = null;
    this.armed = true;
    this.pending = null;
    this.consumed = false;
  }
}

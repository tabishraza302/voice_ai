export type PlaybackBinding = {
  session_id: string;
  transport_epoch: string;
  turn_id: string;
  generation_id: string;
  manifest_id: string;
  fragment_id: string;
  content_hash: string;
  capability_report_hash: string;
};

export type PcmSampleRange = {
  sample_start: number;
  sample_end: number;
  sample_rate: number;
};

export type RenderedFrameRange = {
  frame_start: number;
  frame_end: number;
  sample_rate: number;
};

export type PlaybackReceiptMessage = {
  type: "flow-playback-receipt";
  version: 1;
  binding: PlaybackBinding;
  media_samples: PcmSampleRange;
  rendered_frames: RenderedFrameRange;
  evidence_id: string;
};

export type PlaybackMarker = {
  type: "flow-playback-marker";
  version: 1;
  binding: PlaybackBinding;
  media_samples: PcmSampleRange;
};

export type PlaybackCancellation = {
  type: "flow-playback-cancel";
  version: 1;
  session_id: string;
  transport_epoch: string;
  turn_id: string;
  generation_id: string;
  media_ranges: PcmSampleRange[];
};

export type PlaybackEnableGrant = {
  type: "flow-playback-enable";
  version: 1;
  session_id: string;
  transport_epoch: string;
  capability_report_hash: string;
  browser_tuple_hash: string;
  adapter_tuple_hash: string;
  interruption_stop_drain_proven: true;
};

export type PlaybackCapabilitiesMessage = {
  type: "flow-playback-capabilities";
  version: 1;
  session_id: string;
  browser_tuple_hash: string;
};

export type RenderedPlaybackObservation = {
  type: "rendered";
  binding: PlaybackBinding;
  media_samples: PcmSampleRange;
  rendered_frames: RenderedFrameRange;
};

export type PlaybackControl =
  | { kind: "marker"; marker: PlaybackMarker }
  | { kind: "cancel"; cancellation: PlaybackCancellation }
  | { kind: "invalid-marker" }
  | { kind: "invalid-cancel" };

const BINDING_KEYS = [
  "session_id",
  "transport_epoch",
  "turn_id",
  "generation_id",
  "manifest_id",
  "fragment_id",
  "content_hash",
  "capability_report_hash",
] as const;
const CONTROL_LIMIT_BYTES = 4096;
const MAX_CANCELLED_RANGES = 256;
const MAX_SAFE_INTEGER = 2 ** 53 - 1;
export const MIN_PLAYBACK_SAMPLE_RATE = 8000;
export const MAX_PLAYBACK_SAMPLE_RATE = 192000;
const SHA256 = /^[0-9a-f]{64}$/;

export function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const ownKeys = Object.keys(value);
  return ownKeys.length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

function isText(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.trim().length > 0 && value.length <= 256;
}

function isSafeInteger(value: unknown, minimum = 0): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= MAX_SAFE_INTEGER;
}

function isSampleRate(value: unknown): value is number {
  return isSafeInteger(value, MIN_PLAYBACK_SAMPLE_RATE) && value <= MAX_PLAYBACK_SAMPLE_RATE;
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && SHA256.test(value);
}

function parseBinding(value: unknown): PlaybackBinding | null {
  if (!isPlainRecord(value) || !hasExactKeys(value, BINDING_KEYS)) return null;
  if (!BINDING_KEYS.every((key) => isText(value[key]))) return null;
  if (!isSha256(value.content_hash) || !isSha256(value.capability_report_hash)) return null;
  return value as PlaybackBinding;
}

function parsePcmRange(value: unknown): PcmSampleRange | null {
  if (!isPlainRecord(value) || !hasExactKeys(value, ["sample_start", "sample_end", "sample_rate"])) return null;
  if (
    !isSafeInteger(value.sample_start)
    || !isSafeInteger(value.sample_end, 1)
    || !isSampleRate(value.sample_rate)
    || value.sample_end <= value.sample_start
  ) return null;
  return value as PcmSampleRange;
}

function parseRenderedRange(value: unknown): RenderedFrameRange | null {
  if (!isPlainRecord(value) || !hasExactKeys(value, ["frame_start", "frame_end", "sample_rate"])) return null;
  if (
    !isSafeInteger(value.frame_start)
    || !isSafeInteger(value.frame_end, 1)
    || !isSampleRate(value.sample_rate)
    || value.frame_end <= value.frame_start
  ) return null;
  return value as RenderedFrameRange;
}

function durationsMatch(mediaSamples: PcmSampleRange, renderedFrames: RenderedFrameRange): boolean {
  const mediaDuration = (mediaSamples.sample_end - mediaSamples.sample_start) / mediaSamples.sample_rate;
  const renderedDuration = (renderedFrames.frame_end - renderedFrames.frame_start) / renderedFrames.sample_rate;
  const roundingAllowance = 0.5 / mediaSamples.sample_rate + 0.5 / renderedFrames.sample_rate;
  return Number.isFinite(mediaDuration)
    && Number.isFinite(renderedDuration)
    && Math.abs(mediaDuration - renderedDuration) <= roundingAllowance;
}

function parseJsonOrRecord(raw: unknown): unknown | null {
  if (typeof raw !== "string") return raw;
  if (typeof TextEncoder !== "function" || new TextEncoder().encode(raw).byteLength > CONTROL_LIMIT_BYTES) return null;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
}

export function parsePlaybackEnableGrant(raw: unknown): PlaybackEnableGrant | null {
  const value = parseJsonOrRecord(raw);
  const keys = [
    "type",
    "version",
    "session_id",
    "transport_epoch",
    "capability_report_hash",
    "browser_tuple_hash",
    "adapter_tuple_hash",
    "interruption_stop_drain_proven",
  ];
  if (!isPlainRecord(value) || !hasExactKeys(value, keys)) return null;
  if (
    value.type !== "flow-playback-enable"
    || value.version !== 1
    || !isText(value.session_id)
    || !isText(value.transport_epoch)
    || !isSha256(value.capability_report_hash)
    || !isSha256(value.browser_tuple_hash)
    || !isSha256(value.adapter_tuple_hash)
    || value.interruption_stop_drain_proven !== true
  ) return null;
  return value as PlaybackEnableGrant;
}

export function parsePlaybackCapabilities(raw: unknown): PlaybackCapabilitiesMessage | null {
  const value = parseJsonOrRecord(raw);
  if (
    !isPlainRecord(value)
    || !hasExactKeys(value, ["type", "version", "session_id", "browser_tuple_hash"])
    || value.type !== "flow-playback-capabilities"
    || value.version !== 1
    || !isText(value.session_id)
    || !isSha256(value.browser_tuple_hash)
  ) return null;
  return value as PlaybackCapabilitiesMessage;
}

export function parsePlaybackControl(raw: unknown): PlaybackControl | null {
  const value = parseJsonOrRecord(raw);
  if (!isPlainRecord(value) || typeof value.type !== "string") return null;

  if (value.type === "flow-playback-marker") {
    if (!hasExactKeys(value, ["type", "version", "binding", "media_samples"]) || value.version !== 1) {
      return { kind: "invalid-marker" };
    }
    const binding = parseBinding(value.binding);
    const mediaSamples = parsePcmRange(value.media_samples);
    if (!binding || !mediaSamples) return { kind: "invalid-marker" };
    return {
      kind: "marker",
      marker: { type: "flow-playback-marker", version: 1, binding, media_samples: mediaSamples },
    };
  }

  if (value.type === "flow-playback-cancel") {
    const keys = [
      "type",
      "version",
      "session_id",
      "transport_epoch",
      "turn_id",
      "generation_id",
      "media_ranges",
    ];
    if (
      !hasExactKeys(value, keys)
      || value.version !== 1
      || !isText(value.session_id)
      || !isText(value.transport_epoch)
      || !isText(value.turn_id)
      || !isText(value.generation_id)
      || !Array.isArray(value.media_ranges)
      || value.media_ranges.length > MAX_CANCELLED_RANGES
    ) return { kind: "invalid-cancel" };
    const mediaRanges = value.media_ranges.map(parsePcmRange);
    if (mediaRanges.some((range) => range === null)) return { kind: "invalid-cancel" };
    return {
      kind: "cancel",
      cancellation: {
        type: "flow-playback-cancel",
        version: 1,
        session_id: value.session_id,
        transport_epoch: value.transport_epoch,
        turn_id: value.turn_id,
        generation_id: value.generation_id,
        media_ranges: mediaRanges as PcmSampleRange[],
      },
    };
  }
  return null;
}

export function parsePlaybackReceipt(raw: unknown): PlaybackReceiptMessage | null {
  const value = parseJsonOrRecord(raw);
  if (
    !isPlainRecord(value)
    || !hasExactKeys(value, ["type", "version", "binding", "media_samples", "rendered_frames", "evidence_id"])
    || value.type !== "flow-playback-receipt"
    || value.version !== 1
    || !isText(value.evidence_id)
    || !/^[0-9a-f]{32}$/.test(value.evidence_id)
  ) return null;
  const binding = parseBinding(value.binding);
  const mediaSamples = parsePcmRange(value.media_samples);
  const renderedFrames = parseRenderedRange(value.rendered_frames);
  if (!binding || !mediaSamples || !renderedFrames || !durationsMatch(mediaSamples, renderedFrames)) return null;
  return {
    type: "flow-playback-receipt",
    version: 1,
    binding,
    media_samples: mediaSamples,
    rendered_frames: renderedFrames,
    evidence_id: value.evidence_id,
  };
}

export function parseRenderedObservation(raw: unknown): RenderedPlaybackObservation | null {
  if (
    !isPlainRecord(raw)
    || !hasExactKeys(raw, ["type", "binding", "media_samples", "rendered_frames"])
    || raw.type !== "rendered"
  ) return null;
  const binding = parseBinding(raw.binding);
  const mediaSamples = parsePcmRange(raw.media_samples);
  const renderedFrames = parseRenderedRange(raw.rendered_frames);
  if (!binding || !mediaSamples || !renderedFrames || !durationsMatch(mediaSamples, renderedFrames)) return null;
  return { type: "rendered", binding, media_samples: mediaSamples, rendered_frames: renderedFrames };
}

const ProcessorBase = globalThis.AudioWorkletProcessor ?? class {
  constructor() {
    this.port = { onmessage: null, postMessage() {}, close() {} };
  }
};

const MAX_BUFFER_SECONDS = 2;
const MAX_HISTORY_SECONDS = 10;
const MAX_PENDING_MARKERS = 128;
const MAX_SEEN_MARKERS = 4096;
const MAX_SAFE_INTEGER = 2 ** 53 - 1;
const MIN_SAMPLE_RATE = 8000;
const MAX_SAMPLE_RATE = 192000;
const BINDING_KEYS = [
  "session_id",
  "transport_epoch",
  "turn_id",
  "generation_id",
  "manifest_id",
  "fragment_id",
  "content_hash",
  "capability_report_hash",
];

function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value, keys) {
  const ownKeys = Object.keys(value);
  return ownKeys.length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

function isText(value) {
  return typeof value === "string" && value.length > 0 && value.trim().length > 0 && value.length <= 256;
}

function isSafeInteger(value, minimum = 0) {
  return Number.isSafeInteger(value) && value >= minimum && value <= MAX_SAFE_INTEGER;
}

function isSampleRate(value) {
  return isSafeInteger(value, MIN_SAMPLE_RATE) && value <= MAX_SAMPLE_RATE;
}

function parseBinding(value) {
  if (!isRecord(value) || !hasExactKeys(value, BINDING_KEYS)) return null;
  if (!BINDING_KEYS.every((key) => isText(value[key]))) return null;
  if (!/^[0-9a-f]{64}$/.test(value.content_hash) || !/^[0-9a-f]{64}$/.test(value.capability_report_hash)) return null;
  return value;
}

function parseSampleRange(value) {
  if (!isRecord(value) || !hasExactKeys(value, ["sample_start", "sample_end", "sample_rate"])) return null;
  if (
    !isSafeInteger(value.sample_start)
    || !isSafeInteger(value.sample_end, 1)
    || !isSampleRate(value.sample_rate)
    || value.sample_end <= value.sample_start
  ) return null;
  return value;
}

function parseMarker(value) {
  if (!isRecord(value) || !hasExactKeys(value, ["type", "version", "binding", "media_samples"])) return null;
  if (value.type !== "flow-playback-marker" || value.version !== 1) return null;
  const binding = parseBinding(value.binding);
  const mediaSamples = parseSampleRange(value.media_samples);
  if (!binding || !mediaSamples) return null;
  return { binding, mediaSamples };
}

function parseCancellation(value) {
  const keys = [
    "type",
    "version",
    "session_id",
    "transport_epoch",
    "turn_id",
    "generation_id",
    "media_ranges",
  ];
  if (!isRecord(value) || !hasExactKeys(value, keys)) return null;
  if (
    value.type !== "flow-playback-cancel"
    || value.version !== 1
    || !isText(value.session_id)
    || !isText(value.transport_epoch)
    || !isText(value.turn_id)
    || !isText(value.generation_id)
    || !Array.isArray(value.media_ranges)
    || value.media_ranges.length > 256
  ) return null;
  const mediaRanges = value.media_ranges.map(parseSampleRange);
  if (mediaRanges.some((range) => range === null)) return null;
  return {
    sessionId: value.session_id,
    transportEpoch: value.transport_epoch,
  };
}

function scopeKey(sessionId, transportEpoch) {
  return `${sessionId}\u0000${transportEpoch}`;
}

function bindingKey(binding) {
  return BINDING_KEYS.map((key) => binding[key]).join("\u0000");
}

function rangeTime(range) {
  const startUs = (range.sample_start * 1_000_000) / range.sample_rate;
  const endUs = (range.sample_end * 1_000_000) / range.sample_rate;
  if (
    !Number.isFinite(startUs)
    || !Number.isFinite(endUs)
    || startUs > MAX_SAFE_INTEGER
    || endUs > MAX_SAFE_INTEGER
    || endUs <= startUs
  ) return null;
  return { startUs, endUs };
}

export class FlowPlaybackProcessor extends ProcessorBase {
  constructor() {
    super();
    this.blocks = [];
    this.blockIndex = 0;
    this.bufferedMicroseconds = 0;
    this.lastReceivedEndUs = null;
    this.mediaAnchorUs = null;
    this.frameAnchor = null;
    this.renderedSegments = [];
    this.renderedHistoryFloorUs = null;
    this.renderedThroughUs = null;
    this.contextSampleRate = null;
    this.markers = new Map();
    this.seenMarkers = new Set();
    this.expectedScope = null;
    this.disabled = false;
    this.port.onmessage = (event) => this.receive(event.data);
  }

  receive(message) {
    if (this.disabled || !isRecord(message)) return;
    if (message.type === "scope") {
      if (
        !hasExactKeys(message, ["type", "session_id", "transport_epoch"])
        || !isText(message.session_id)
        || !isText(message.transport_epoch)
      ) {
        this.fail("playback renderer received an invalid authorization scope");
        return;
      }
      const nextScope = scopeKey(message.session_id, message.transport_epoch);
      if (this.expectedScope !== null && this.expectedScope !== nextScope) {
        this.fail("playback renderer authorization scope changed");
        return;
      }
      this.expectedScope = nextScope;
      return;
    }
    if (message.type === "pcm") {
      this.receivePcm(message);
      return;
    }
    if (message.type === "marker") {
      this.receiveMarker(message.marker);
      return;
    }
    if (message.type === "cancel") this.receiveCancellation(message.cancellation);
  }

  receivePcm(message) {
    const channels = message.channels;
    if (
      this.expectedScope === null
      || !isSafeInteger(message.timestampUs)
      || !isSampleRate(message.sampleRate)
      || !isSafeInteger(message.frames, 1)
      || message.frames > message.sampleRate * MAX_BUFFER_SECONDS
      || !Array.isArray(channels)
      || channels.length < 1
      || channels.length > 2
      || channels.some((channel) => !(channel instanceof Float32Array) || channel.length !== message.frames)
    ) {
      this.fail("decoded audio chunk has an unsupported shape");
      return;
    }
    const durationUs = (message.frames * 1_000_000) / message.sampleRate;
    const endUs = message.timestampUs + durationUs;
    const epsilonUs = 0.5 * (1_000_000 / message.sampleRate);
    if (
      !Number.isFinite(endUs)
      || (this.lastReceivedEndUs !== null && message.timestampUs < this.lastReceivedEndUs - epsilonUs)
    ) {
      this.fail("decoded audio timestamps moved backwards or overlapped");
      return;
    }
    if (this.bufferedMicroseconds + durationUs > MAX_BUFFER_SECONDS * 1_000_000) {
      this.fail("decoded audio queue exceeded its two-second limit");
      return;
    }
    this.blocks.push({
      timestampUs: message.timestampUs,
      endUs,
      sampleRate: message.sampleRate,
      frames: message.frames,
      channels,
    });
    this.bufferedMicroseconds += durationUs;
    this.lastReceivedEndUs = endUs;
  }

  receiveMarker(rawMarker) {
    const parsed = parseMarker(rawMarker);
    if (!parsed || this.expectedScope === null) return;
    const { binding, mediaSamples } = parsed;
    if (scopeKey(binding.session_id, binding.transport_epoch) !== this.expectedScope) return;
    const key = bindingKey(binding);
    if (this.seenMarkers.has(key) || this.markers.has(key)) return;
    const time = rangeTime(mediaSamples);
    if (!time) return;
    if (this.markers.size >= MAX_PENDING_MARKERS || this.seenMarkers.size >= MAX_SEEN_MARKERS) {
      this.fail("playback marker ledger limit exceeded");
      return;
    }
    const marker = {
      binding,
      mediaSamples,
      startUs: time.startUs,
      endUs: time.endUs,
      nextMediaUs: time.startUs,
      frameStart: null,
      frameEnd: null,
    };
    const epsilonUs = this.contextSampleRate === null ? 0 : 0.5 * (1_000_000 / this.contextSampleRate);
    if (
      this.renderedHistoryFloorUs !== null
      && marker.startUs < this.renderedHistoryFloorUs - epsilonUs
    ) {
      this.fail("playback marker arrived after its rendered evidence aged out");
      return;
    }
    this.markers.set(key, marker);
    if (this.contextSampleRate === null) return;
    for (const segment of this.renderedSegments) {
      const progress = this.advanceMarker(marker, segment, this.contextSampleRate, epsilonUs);
      if (progress === "invalid") {
        this.fail("playback marker range includes uncorrelated rendered audio");
        return;
      }
      if (progress === "complete") {
        this.emitMarker(key, marker);
        return;
      }
    }
    if (this.renderedThroughUs !== null && marker.endUs <= this.renderedThroughUs + epsilonUs) {
      this.fail("playback marker arrived after its audio range was rendered");
    }
  }

  receiveCancellation(rawCancellation) {
    const cancellation = parseCancellation(rawCancellation);
    if (!cancellation || this.expectedScope === null) return;
    if (scopeKey(cancellation.sessionId, cancellation.transportEpoch) !== this.expectedScope) return;
    this.disabled = true;
    this.clearPlaybackState();
    this.port.postMessage({ type: "interrupted", reason: "playback cancellation cannot be safely drained" });
  }

  process(_inputs, outputs) {
    const output = outputs[0];
    if (!output || output.length === 0) return true;
    for (const channel of output) channel.fill(0);
    if (this.disabled || this.expectedScope === null || this.blocks.length === 0) return true;

    const contextRate = globalThis.sampleRate;
    const frameStart = globalThis.currentFrame;
    if (!isSampleRate(contextRate) || !isSafeInteger(frameStart)) {
      this.fail("AudioWorklet render clock is unavailable");
      return true;
    }
    if (this.contextSampleRate !== null && this.contextSampleRate !== contextRate) {
      this.fail("AudioWorklet sample rate changed during playback");
      return true;
    }
    this.contextSampleRate = contextRate;
    if (this.mediaAnchorUs === null) {
      const firstBlock = this.blocks[this.blockIndex];
      if (!firstBlock) return true;
      this.mediaAnchorUs = firstBlock.timestampUs;
      this.frameAnchor = frameStart;
    }

    const runs = [];
    let currentRun = null;
    const outputFrames = output[0].length;
    const epsilonUs = 0.5 * (1_000_000 / contextRate);
    for (let index = 0; index < outputFrames; index += 1) {
      const absoluteFrame = frameStart + index;
      const mediaTimeUs = this.mediaAnchorUs + ((absoluteFrame - this.frameAnchor) * 1_000_000) / contextRate;
      const block = this.blockForTime(mediaTimeUs, epsilonUs);
      if (!block) {
        if (currentRun) runs.push(currentRun);
        currentRun = null;
        continue;
      }
      const sourcePosition = ((mediaTimeUs - block.timestampUs) * block.sampleRate) / 1_000_000;
      if (sourcePosition < -0.001 || sourcePosition >= block.frames) {
        if (currentRun) runs.push(currentRun);
        currentRun = null;
        continue;
      }
      const low = Math.max(0, Math.floor(sourcePosition));
      const high = Math.min(block.frames - 1, low + 1);
      const fraction = Math.min(1, Math.max(0, sourcePosition - low));
      for (let channelIndex = 0; channelIndex < output.length; channelIndex += 1) {
        const source = block.channels[Math.min(channelIndex, block.channels.length - 1)];
        output[channelIndex][index] = source[low] + (source[high] - source[low]) * fraction;
      }

      const mediaEndUs = mediaTimeUs + (1_000_000 / contextRate);
      if (
        currentRun
        && currentRun.frameEnd === absoluteFrame
        && Math.abs(currentRun.mediaEndUs - mediaTimeUs) <= epsilonUs
      ) {
        currentRun.frameEnd += 1;
        currentRun.mediaEndUs = mediaEndUs;
      } else {
        if (currentRun) runs.push(currentRun);
        currentRun = {
          frameStart: absoluteFrame,
          frameEnd: absoluteFrame + 1,
          mediaStartUs: mediaTimeUs,
          mediaEndUs,
        };
      }
    }
    if (currentRun) runs.push(currentRun);

    const completedMarkers = [];
    for (const run of runs) {
      if (!this.appendRenderedRun(run, contextRate, epsilonUs, completedMarkers)) {
        for (const channel of output) channel.fill(0);
        return true;
      }
    }

    const mediaEndUs = this.mediaAnchorUs
      + ((frameStart + outputFrames - this.frameAnchor) * 1_000_000) / contextRate;
    this.renderedThroughUs = mediaEndUs;
    this.dropConsumedBlocks(mediaEndUs, epsilonUs);
    this.trimHistory(frameStart + outputFrames, contextRate);
    for (const marker of this.markers.values()) {
      if (marker.endUs <= mediaEndUs + epsilonUs) {
        this.fail("playback marker range was not fully correlated with rendered audio");
        for (const channel of output) channel.fill(0);
        return true;
      }
    }
    for (const { key, marker } of completedMarkers) {
      if (!this.emitMarker(key, marker)) {
        for (const channel of output) channel.fill(0);
        return true;
      }
    }
    return true;
  }

  blockForTime(mediaTimeUs, epsilonUs) {
    while (this.blockIndex < this.blocks.length) {
      const block = this.blocks[this.blockIndex];
      if (block.endUs <= mediaTimeUs + epsilonUs) {
        this.blockIndex += 1;
        continue;
      }
      if (block.timestampUs <= mediaTimeUs + epsilonUs) return block;
      return null;
    }
    return null;
  }

  appendRenderedRun(run, contextRate, epsilonUs, completedMarkers) {
    for (const [key, marker] of this.markers) {
      const progress = this.advanceMarker(marker, run, contextRate, epsilonUs);
      if (progress === "invalid") {
        this.fail("playback marker range includes uncorrelated rendered audio");
        return false;
      }
      if (progress === "complete") {
        this.markers.delete(key);
        completedMarkers.push({ key, marker });
      }
    }
    const previous = this.renderedSegments[this.renderedSegments.length - 1];
    if (
      previous
      && previous.frameEnd === run.frameStart
      && Math.abs(previous.mediaEndUs - run.mediaStartUs) <= epsilonUs
    ) {
      previous.frameEnd = run.frameEnd;
      previous.mediaEndUs = run.mediaEndUs;
      return true;
    }
    this.renderedSegments.push({ ...run, sampleRate: contextRate });
    return true;
  }

  dropConsumedBlocks(mediaEndUs, epsilonUs) {
    let consumedCount = this.blockIndex;
    while (consumedCount < this.blocks.length && this.blocks[consumedCount].endUs <= mediaEndUs + epsilonUs) {
      consumedCount += 1;
    }
    for (let index = 0; index < consumedCount; index += 1) {
      const block = this.blocks[index];
      this.bufferedMicroseconds = Math.max(
        0,
        this.bufferedMicroseconds - (block.frames * 1_000_000) / block.sampleRate,
      );
      this.port.postMessage({
        type: "pcm-consumed",
        frames: block.frames,
        sample_rate: block.sampleRate,
      });
    }
    if (consumedCount > 0) {
      this.blocks.splice(0, consumedCount);
      this.blockIndex = 0;
    }
  }

  trimHistory(frameEnd, contextRate) {
    const cutoff = frameEnd - contextRate * MAX_HISTORY_SECONDS;
    const cutoffUs = this.mediaAnchorUs + ((cutoff - this.frameAnchor) * 1_000_000) / contextRate;
    this.renderedHistoryFloorUs = this.renderedHistoryFloorUs === null
      ? cutoffUs
      : Math.max(this.renderedHistoryFloorUs, cutoffUs);
    while (this.renderedSegments.length > 0 && this.renderedSegments[0].frameEnd <= cutoff) {
      this.renderedSegments.shift();
    }
    const first = this.renderedSegments[0];
    if (first && first.frameStart < cutoff) {
      const removedFrames = cutoff - first.frameStart;
      first.frameStart = cutoff;
      first.mediaStartUs += (removedFrames * 1_000_000) / contextRate;
    }
  }

  advanceMarker(marker, segment, contextRate, epsilonUs) {
    if (segment.mediaEndUs < marker.nextMediaUs - epsilonUs) return "pending";
    if (segment.mediaStartUs > marker.nextMediaUs + epsilonUs) {
      return segment.mediaStartUs < marker.endUs - epsilonUs ? "invalid" : "pending";
    }
    const startUs = Math.max(marker.nextMediaUs, segment.mediaStartUs);
    const frameAtStart = segment.frameStart + ((startUs - segment.mediaStartUs) * contextRate) / 1_000_000;
    if (Math.abs(frameAtStart - Math.round(frameAtStart)) > 0.001) return "invalid";
    const startFrame = Math.round(frameAtStart);
    if (marker.frameStart === null) marker.frameStart = startFrame;
    else if (marker.frameEnd !== startFrame) return "invalid";

    const coveredEndUs = Math.min(marker.endUs, segment.mediaEndUs);
    if (coveredEndUs <= startUs) return "pending";
    const frameAtEnd = segment.frameStart + ((coveredEndUs - segment.mediaStartUs) * contextRate) / 1_000_000;
    if (Math.abs(frameAtEnd - Math.round(frameAtEnd)) > 0.001) return "invalid";
    marker.frameEnd = Math.round(frameAtEnd);
    marker.nextMediaUs = coveredEndUs;
    if (marker.nextMediaUs < marker.endUs - epsilonUs) return "pending";

    const expectedDuration = ((marker.endUs - marker.startUs) * contextRate) / 1_000_000;
    if (
      marker.frameStart === null
      || marker.frameEnd <= marker.frameStart
      || Math.abs((marker.frameEnd - marker.frameStart) - expectedDuration) > 0.001
    ) return "invalid";
    return "complete";
  }

  emitMarker(key, marker) {
    if (
      this.contextSampleRate === null
      || marker.frameStart === null
      || marker.frameEnd === null
      || this.seenMarkers.size >= MAX_SEEN_MARKERS
    ) {
      this.fail("playback marker evidence ledger could not be completed safely");
      return false;
    }
    try {
      this.port.postMessage({
        type: "rendered",
        binding: marker.binding,
        media_samples: marker.mediaSamples,
        rendered_frames: {
          frame_start: marker.frameStart,
          frame_end: marker.frameEnd,
          sample_rate: this.contextSampleRate,
        },
      });
    } catch {
      this.fail("playback marker evidence could not reach the main thread");
      return false;
    }
    this.markers.delete(key);
    this.seenMarkers.add(key);
    return true;
  }

  fail(reason) {
    if (this.disabled) return;
    this.disabled = true;
    this.clearPlaybackState();
    this.port.postMessage({ type: "unsupported", reason });
  }

  clearPlaybackState() {
    this.blocks.length = 0;
    this.blockIndex = 0;
    this.bufferedMicroseconds = 0;
    this.markers.clear();
    this.renderedSegments.length = 0;
    this.renderedHistoryFloorUs = null;
    this.renderedThroughUs = null;
    this.contextSampleRate = null;
  }
}

if (typeof globalThis.registerProcessor === "function") {
  globalThis.registerProcessor("flow-playback-renderer", FlowPlaybackProcessor);
}

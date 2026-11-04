import { afterEach, describe, expect, test } from "bun:test";
import { FlowPlaybackProcessor } from "../../public/voice-playback-renderer.worklet.js";

const originalSampleRate = Object.getOwnPropertyDescriptor(globalThis, "sampleRate");
const originalCurrentFrame = Object.getOwnPropertyDescriptor(globalThis, "currentFrame");

const scope = { type: "scope", session_id: "session-1", transport_epoch: "epoch-1" };
const binding = {
  session_id: "session-1",
  transport_epoch: "epoch-1",
  turn_id: "turn-1",
  generation_id: "generation-1",
  manifest_id: "manifest-1",
  fragment_id: "fragment-1",
  content_hash: "b".repeat(64),
  capability_report_hash: "a".repeat(64),
};
const marker = {
  type: "flow-playback-marker",
  version: 1,
  binding,
  media_samples: { sample_start: 240, sample_end: 480, sample_rate: 48_000 },
};

function setRenderClock(frame, sampleRate = 48_000) {
  Object.defineProperty(globalThis, "sampleRate", { configurable: true, value: sampleRate });
  Object.defineProperty(globalThis, "currentFrame", { configurable: true, value: frame });
}

function makeProcessor() {
  const processor = new FlowPlaybackProcessor();
  const messages = [];
  processor.port = { postMessage: (message) => messages.push(message), onmessage: null, close() {} };
  processor.receive(scope);
  return { processor, messages };
}

function addPcm(processor, { timestampUs = 0, frames = 480, sample = 0.25, sampleRate = 48_000 } = {}) {
  processor.receive({
    type: "pcm",
    timestampUs,
    sampleRate,
    frames,
    channels: [new Float32Array(frames).fill(sample)],
  });
}

function render(processor, frame, sampleRate = 48_000) {
  setRenderClock(frame, sampleRate);
  const left = new Float32Array(128);
  const right = new Float32Array(128);
  processor.process([], [[left, right]]);
  return { left, right };
}

function renderedMessages(messages) {
  return messages.filter((message) => message.type === "rendered");
}

afterEach(() => {
  if (originalSampleRate) Object.defineProperty(globalThis, "sampleRate", originalSampleRate);
  else delete globalThis.sampleRate;
  if (originalCurrentFrame) Object.defineProperty(globalThis, "currentFrame", originalCurrentFrame);
  else delete globalThis.currentFrame;
});

describe("fragment-correlated playback worklet", () => {
  test("reports only the exact marked samples written into output frames", () => {
    const { processor, messages } = makeProcessor();
    processor.receive({ type: "marker", marker });
    addPcm(processor);

    expect(renderedMessages(messages)).toEqual([]);
    render(processor, 0);
    render(processor, 128);
    render(processor, 256);
    const output = render(processor, 384);

    expect(output.left[0]).toBeCloseTo(0.25);
    expect(renderedMessages(messages)).toHaveLength(1);
    expect(renderedMessages(messages)[0]).toEqual({
      type: "rendered",
      binding,
      media_samples: marker.media_samples,
      rendered_frames: { frame_start: 240, frame_end: 480, sample_rate: 48_000 },
    });
    processor.receive({ type: "marker", marker });
    expect(renderedMessages(messages)).toHaveLength(1);
  });

  test("accumulates evidence for a marked fragment longer than retained history", () => {
    const { processor, messages } = makeProcessor();
    const framesPerChunk = 8192;
    const totalFrames = framesPerChunk * 11;
    const longMarker = {
      ...marker,
      media_samples: { sample_start: 0, sample_end: totalFrames, sample_rate: 8_000 },
    };
    processor.receive({ type: "marker", marker: longMarker });

    for (let chunk = 0; chunk < 11; chunk += 1) {
      const startFrame = chunk * framesPerChunk;
      addPcm(processor, {
        timestampUs: chunk * 1_024_000,
        frames: framesPerChunk,
        sampleRate: 8_000,
      });
      for (let frame = startFrame; frame < startFrame + framesPerChunk; frame += 128) {
        render(processor, frame, 8_000);
      }
    }

    expect(renderedMessages(messages)).toEqual([{
      type: "rendered",
      binding,
      media_samples: longMarker.media_samples,
      rendered_frames: { frame_start: 0, frame_end: totalFrames, sample_rate: 8_000 },
    }]);
  });

  test("produces no evidence for mismatched decoded timestamps or gaps", () => {
    const shifted = makeProcessor();
    shifted.processor.receive({ type: "marker", marker });
    addPcm(shifted.processor, { timestampUs: 100_000 });
    for (const frame of [0, 128, 256, 384]) render(shifted.processor, frame);
    expect(renderedMessages(shifted.messages)).toEqual([]);

    const gap = makeProcessor();
    const spanningMarker = {
      ...marker,
      media_samples: { sample_start: 0, sample_end: 480, sample_rate: 48_000 },
    };
    gap.processor.receive({ type: "marker", marker: spanningMarker });
    addPcm(gap.processor, { frames: 120 });
    gap.processor.receive({
      type: "pcm",
      timestampUs: 5_000,
      sampleRate: 48_000,
      frames: 120,
      channels: [new Float32Array(120).fill(0.25)],
    });
    for (const frame of [0, 128, 256, 384]) render(gap.processor, frame);
    expect(renderedMessages(gap.messages)).toEqual([]);
  });

  test("rejects media sample ranges that cannot be represented on the audio clock", () => {
    const { processor, messages } = makeProcessor();
    processor.receive({
      type: "marker",
      marker: {
        ...marker,
        media_samples: {
          sample_start: Number.MAX_SAFE_INTEGER - 1,
          sample_end: Number.MAX_SAFE_INTEGER,
          sample_rate: 8_000,
        },
      },
    });
    addPcm(processor);
    for (const frame of [0, 128, 256, 384]) render(processor, frame);
    expect(renderedMessages(messages)).toEqual([]);
  });

  test("cancellation stops output and invalidates all pending observations", () => {
    const { processor, messages } = makeProcessor();
    processor.receive({ type: "marker", marker });
    addPcm(processor);
    processor.receive({
      type: "cancel",
      cancellation: {
        type: "flow-playback-cancel",
        version: 1,
        session_id: "session-1",
        transport_epoch: "epoch-1",
        turn_id: "turn-1",
        generation_id: "generation-1",
        media_ranges: [marker.media_samples],
      },
    });

    const output = render(processor, 0);

    expect([...output.left, ...output.right].every((sample) => sample === 0)).toBe(true);
    expect(renderedMessages(messages)).toEqual([]);
    expect(messages.some((message) => message.type === "interrupted")).toBe(true);
  });

  test("rejects marker scope mismatches and malformed decoded PCM", () => {
    const { processor, messages } = makeProcessor();
    processor.receive({ type: "marker", marker: { ...marker, binding: { ...binding, transport_epoch: "stale" } } });
    processor.receive({ type: "pcm", timestampUs: 0, sampleRate: 48_000, frames: 480, channels: [] });
    render(processor, 0);

    expect(renderedMessages(messages)).toEqual([]);
    expect(messages.some((message) => message.type === "unsupported")).toBe(true);
  });
});

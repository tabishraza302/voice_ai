import { describe, expect, test } from "bun:test";
import {
  parsePlaybackCapabilities,
  parsePlaybackControl,
  parsePlaybackEnableGrant,
  parsePlaybackReceipt,
  parseRenderedObservation,
} from "./playback-protocol.ts";

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
const mediaSamples = { sample_start: 240, sample_end: 480, sample_rate: 48_000 };
const renderedFrames = { frame_start: 480, frame_end: 960, sample_rate: 96_000 };
const marker = {
  type: "flow-playback-marker",
  version: 1,
  binding,
  media_samples: mediaSamples,
};
const receipt = {
  type: "flow-playback-receipt",
  version: 1,
  binding,
  media_samples: mediaSamples,
  rendered_frames: renderedFrames,
  evidence_id: "c".repeat(32),
};

function cancellation(overrides = {}) {
  return {
    type: "flow-playback-cancel",
    version: 1,
    session_id: "session-1",
    transport_epoch: "epoch-1",
    turn_id: "turn-1",
    generation_id: "generation-1",
    media_ranges: [mediaSamples],
    ...overrides,
  };
}

const capabilities = {
  type: "flow-playback-capabilities",
  version: 1,
  session_id: "session-1",
  browser_tuple_hash: "d".repeat(64),
};

const enableGrant = {
  type: "flow-playback-enable",
  version: 1,
  session_id: "session-1",
  transport_epoch: "epoch-1",
  capability_report_hash: "a".repeat(64),
  browser_tuple_hash: "d".repeat(64),
  adapter_tuple_hash: "e".repeat(64),
  interruption_stop_drain_proven: true,
};

describe("playback wire protocol", () => {
  test("accepts only an exact bounded capability announcement", () => {
    expect(parsePlaybackCapabilities(JSON.stringify(capabilities))).toEqual(capabilities);
    expect(parsePlaybackCapabilities({ ...capabilities, report_hash: "e".repeat(64) })).toBeNull();
    expect(parsePlaybackCapabilities({ ...capabilities, browser_tuple_hash: "invalid" })).toBeNull();
    expect(parsePlaybackCapabilities({ ...capabilities, version: 2 })).toBeNull();
  });
  test("requires an exact enable grant with proven interruption stop and drain", () => {
    expect(parsePlaybackEnableGrant(JSON.stringify(enableGrant))).toEqual(enableGrant);
    expect(parsePlaybackEnableGrant({ ...enableGrant, interruption_stop_drain_proven: false })).toBeNull();
    expect(parsePlaybackEnableGrant({ ...enableGrant, browser_tuple_hash: "unknown" })).toBeNull();
    expect(parsePlaybackEnableGrant({ ...enableGrant, extra: true })).toBeNull();
  });


  test("accepts exact v1 marker and cancellation messages", () => {
    expect(parsePlaybackControl(JSON.stringify(marker))).toEqual({ kind: "marker", marker });
    expect(parsePlaybackControl(cancellation())).toMatchObject({
      kind: "cancel",
      cancellation: cancellation(),
    });
  });

  test("rejects malformed, extra-field, oversized, and invalid sample ranges", () => {
    const invalidMarkers = [
      { ...marker, surprise: true },
      { ...marker, version: 2 },
      { ...marker, binding: { ...binding, session_id: "" } },
      { ...marker, media_samples: { ...mediaSamples, sample_end: mediaSamples.sample_start } },
      { ...marker, binding: { ...binding, content_hash: "not-a-hash" } },
    ];
    for (const invalid of invalidMarkers) {
      expect(parsePlaybackControl(invalid)).toEqual({ kind: "invalid-marker" });
    }
    expect(parsePlaybackControl(" ".repeat(4097))).toBeNull();
    expect(parsePlaybackControl(cancellation({ extra: true }))).toEqual({ kind: "invalid-cancel" });
    expect(parsePlaybackControl(cancellation({ media_ranges: Array(257).fill(mediaSamples) }))).toEqual({ kind: "invalid-cancel" });
  });

  test("accepts receipts only when media and rendered frame durations agree", () => {
    expect(parsePlaybackReceipt(receipt)).toEqual(receipt);
    expect(parsePlaybackReceipt({ ...receipt, unbound: true })).toBeNull();
    expect(parsePlaybackReceipt({ ...receipt, evidence_id: "not-an-evidence-id" })).toBeNull();
    expect(parsePlaybackReceipt({
      ...receipt,
      rendered_frames: { ...renderedFrames, frame_end: renderedFrames.frame_end + 10 },
    })).toBeNull();
  });

  test("accepts worklet observations only with the exact rendered shape", () => {
    const observation = {
      type: "rendered",
      binding,
      media_samples: mediaSamples,
      rendered_frames: renderedFrames,
    };
    expect(parseRenderedObservation(observation)).toEqual(observation);
    expect(parseRenderedObservation({ ...observation, queued: true })).toBeNull();
  });
});

import { afterEach, describe, expect, test } from "bun:test";
import { createHash, webcrypto } from "node:crypto";
import { PlaybackRenderer, computePlaybackBrowserTupleHash } from "./playback-renderer.ts";

const sentReceipts = [];
const nodes = [];

const authorization = {
  type: "flow-playback-enable",
  version: 1,
  session_id: "session-1",
  transport_epoch: "epoch-1",
  capability_report_hash: "a".repeat(64),
  browser_tuple_hash: "b".repeat(64),
  adapter_tuple_hash: "c".repeat(64),
  interruption_stop_drain_proven: true,
};
const binding = {
  session_id: "session-1",
  transport_epoch: "epoch-1",
  turn_id: "turn-1",
  generation_id: "generation-1",
  manifest_id: "manifest-1",
  fragment_id: "fragment-1",
  content_hash: "d".repeat(64),
  capability_report_hash: authorization.capability_report_hash,
};
const marker = {
  type: "flow-playback-marker",
  version: 1,
  binding,
  media_samples: { sample_start: 240, sample_end: 480, sample_rate: 48_000 },
};
const observation = {
  type: "rendered",
  binding,
  media_samples: marker.media_samples,
  rendered_frames: { frame_start: 480, frame_end: 960, sample_rate: 96_000 },
};

class FakeAudioWorkletNode {
  constructor() {
    this.port = {
      messages: [],
      onmessage: null,
      closed: false,
      postMessage(message) { this.messages.push(message); },
      close() { this.closed = true; },
    };
    this.disconnected = false;
    nodes.push(this);
  }
  connect() {}
  disconnect() { this.disconnected = true; }
}

class FakeTrackProcessor {
  constructor({ track }) {
    this.track = track;
    this.readable = {
      getReader: () => ({
        read: () => new Promise((resolve) => { this.resolveRead = resolve; }),
        cancel: () => {
          this.resolveRead?.({ done: true, value: undefined });
          return Promise.resolve();
        },
        releaseLock() {},
      }),
    };
  }
}

function playbackRuntime({ processor = true, audioData = true, cryptography = webcrypto } = {}) {
  return {
    crypto: cryptography,
    AudioWorkletNode: FakeAudioWorkletNode,
    MediaStreamTrackProcessor: processor ? FakeTrackProcessor : undefined,
    AudioData: audioData ? class FakeAudioData {} : undefined,
  };
}

function context(state = "running") {
  return {
    state,
    sampleRate: 96_000,
    destination: {},
    audioWorklet: { addModule: async () => {} },
    addEventListener() {},
    removeEventListener() {},
  };
}

function track() {
  const listeners = new Map();
  return {
    kind: "audio",
    readyState: "live",
    enabled: true,
    muted: false,
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name) { listeners.delete(name); },
    emit(name) { listeners.get(name)?.(); },
  };
}

function createRenderer({ contextValue = context(), onInterrupted, sendReceipt, runtime = playbackRuntime() } = {}) {
  return new PlaybackRenderer(contextValue, {
    authorization,
    sendReceipt: sendReceipt ?? ((receipt) => {
      sentReceipts.push(receipt);
      return true;
    }),
    onInterrupted,
    runtime,
  });
}

function workletMessage(node, data) {
  node.port.onmessage?.({ data });
}

afterEach(() => {
  sentReceipts.length = 0;
  nodes.length = 0;
});

describe("browser tuple qualification", () => {
  test("hashes a deterministic qualified browser tuple and fails closed without its APIs", async () => {
    const runtime = {
      crypto: webcrypto,
      MediaStreamTrackProcessor: class {},
      AudioData: class {},
      AudioWorkletNode: class {},
    };
    const userAgentData = {
      brands: [
        { brand: "Zeta", version: "2" },
        { brand: "Alpha", version: "1" },
      ],
      mobile: false,
      platform: "TestOS",
      getHighEntropyValues: async () => ({
        architecture: "arm",
        bitness: "64",
        fullVersionList: [
          { brand: "Zeta", version: "2.0" },
          { brand: "Alpha", version: "1.0" },
        ],
        platformVersion: "17.0",
        wow64: false,
      }),
    };
    const navigatorValue = { userAgent: "TestBrowser/1.0", userAgentData };
    const expectedTuple = {
      schema: "flow-playback-browser-tuple-v1",
      user_agent: "TestBrowser/1.0",
      platform: "TestOS",
      platform_version: "17.0",
      architecture: "arm",
      bitness: "64",
      brands: [["Alpha", "1"], ["Zeta", "2"]],
      full_version_list: [["Alpha", "1.0"], ["Zeta", "2.0"]],
      mobile: false,
      wow64: false,
      audio_context_sample_rate: 48_000,
      media_stream_track_processor: true,
      audio_data: true,
      audio_worklet_node: true,
    };
    const expectedHash = createHash("sha256").update(JSON.stringify(expectedTuple)).digest("hex");

    expect(await computePlaybackBrowserTupleHash(48_000, navigatorValue, runtime)).toBe(expectedHash);
    userAgentData.brands.reverse();
    userAgentData.getHighEntropyValues = async () => ({
      architecture: "arm",
      bitness: "64",
      fullVersionList: [
        { brand: "Alpha", version: "1.0" },
        { brand: "Zeta", version: "2.0" },
      ],
      platformVersion: "17.0",
      wow64: false,
    });
    expect(await computePlaybackBrowserTupleHash(48_000, navigatorValue, runtime)).toBe(expectedHash);
    expect(await computePlaybackBrowserTupleHash(7_999, navigatorValue, runtime)).toBeNull();
    expect(await computePlaybackBrowserTupleHash(192_001, navigatorValue, runtime)).toBeNull();
    expect(await computePlaybackBrowserTupleHash(48_000, navigatorValue, {
      ...runtime,
      crypto: undefined,
    })).toBeNull();
    expect(await computePlaybackBrowserTupleHash(48_000, { userAgent: "TestBrowser/1.0" }, runtime)).toBeNull();
    expect(await computePlaybackBrowserTupleHash(48_000, navigatorValue, {
      ...runtime,
      AudioData: undefined,
    })).toBeNull();
  });
});

describe("qualified playback renderer", () => {
  test("keeps playback receipt-free when a required browser API is unavailable", async () => {
    const renderer = createRenderer({ runtime: playbackRuntime({ processor: false }) });

    expect(await renderer.prepare()).toBe(false);
    expect(renderer.handleControlMessage(marker)).toBe(true);
    expect(sentReceipts).toEqual([]);
    expect(nodes).toHaveLength(0);
    renderer.close();
  });
  test("keeps playback receipt-free without Web Crypto", async () => {
    const renderer = createRenderer({ runtime: playbackRuntime({ cryptography: null }) });

    expect(await renderer.prepare()).toBe(false);
    expect(renderer.handleControlMessage(marker)).toBe(true);
    expect(sentReceipts).toEqual([]);
    expect(nodes).toHaveLength(0);
    renderer.close();
  });


  test("holds early markers until the qualified worklet has an attached remote track", async () => {
    const renderer = createRenderer();
    expect(await renderer.prepare()).toBe(true);
    expect(renderer.handleControlMessage(JSON.stringify(marker))).toBe(true);
    expect(nodes[0].port.messages.some((message) => message.type === "marker")).toBe(false);

    expect(renderer.attach(track())).toBe(true);
    expect(nodes[0].port.messages.filter((message) => message.type === "marker")).toEqual([
      { type: "marker", marker },
    ]);
    renderer.close();
  });

  test("keeps validated APIs stable across asynchronous worklet setup", async () => {
    const runtime = playbackRuntime();
    const contextValue = context();
    contextValue.audioWorklet.addModule = async () => {
      runtime.AudioWorkletNode = undefined;
      runtime.MediaStreamTrackProcessor = undefined;
    };
    const renderer = createRenderer({ contextValue, runtime });

    expect(await renderer.prepare()).toBe(true);
    expect(renderer.attach(track())).toBe(true);
    expect(nodes).toHaveLength(1);
    renderer.close();
  });

  test("sends matching rendered evidence and suppresses it only while output is muted", async () => {
    const renderer = createRenderer();
    await renderer.prepare();
    renderer.attach(track());
    const node = nodes[0];

    renderer.setOutputMuted(true);
    workletMessage(node, observation);
    expect(sentReceipts).toEqual([]);
    renderer.setOutputMuted(false);
    workletMessage(node, observation);

    expect(sentReceipts).toHaveLength(1);
    expect(sentReceipts[0]).toMatchObject({
      type: "flow-playback-receipt",
      version: 1,
      binding,
      media_samples: marker.media_samples,
      rendered_frames: observation.rendered_frames,
    });
    expect(sentReceipts[0].evidence_id).toMatch(/^[0-9a-f]{32}$/);
    renderer.close();
  });

  test("ignores cancellation from a different transport epoch", async () => {
    const renderer = createRenderer();
    await renderer.prepare();
    renderer.attach(track());
    const node = nodes[0];
    const staleCancellation = {
      type: "flow-playback-cancel",
      version: 1,
      session_id: authorization.session_id,
      transport_epoch: "stale-epoch",
      turn_id: binding.turn_id,
      generation_id: binding.generation_id,
      media_ranges: [marker.media_samples],
    };

    expect(renderer.handleControlMessage(staleCancellation)).toBe(true);
    workletMessage(node, observation);

    expect(node.disconnected).toBe(false);
    expect(sentReceipts).toHaveLength(1);
    renderer.close();
  });

  test("rejects a mismatched marker scope and mismatched worklet evidence", async () => {
    const unsupported = [];
    const renderer = new PlaybackRenderer(context(), {
      authorization,
      sendReceipt: (receipt) => { sentReceipts.push(receipt); return true; },
      onUnsupported: (reason) => unsupported.push(reason),
      runtime: playbackRuntime(),
    });
    await renderer.prepare();
    renderer.attach(track());

    const node = nodes[0];
    renderer.handleControlMessage({
      ...marker,
      binding: { ...binding, transport_epoch: "stale-epoch" },
    });
    expect(node.port.messages.some((message) => message.type === "marker")).toBe(false);

    workletMessage(node, {
      ...observation,
      binding: { ...binding, session_id: "other-session" },
    });
    expect(sentReceipts).toEqual([]);
    expect(unsupported).toContain("AudioWorklet emitted evidence for a different playback scope");
    expect(node.disconnected).toBe(true);
  });

  test("stops playback and invalidates pending evidence on interruption", async () => {
    const interruptions = [];
    const renderer = createRenderer({ onInterrupted: (value) => interruptions.push(value) });
    await renderer.prepare();
    renderer.attach(track());
    const node = nodes[0];
    const cancellation = {
      type: "flow-playback-cancel",
      version: 1,
      session_id: authorization.session_id,
      transport_epoch: authorization.transport_epoch,
      turn_id: binding.turn_id,
      generation_id: binding.generation_id,
      media_ranges: [marker.media_samples],
    };

    renderer.handleControlMessage(cancellation);
    workletMessage(node, { type: "interrupted" });
    workletMessage(node, observation);

    expect(interruptions).toEqual([cancellation]);
    expect(sentReceipts).toEqual([]);
    expect(node.disconnected).toBe(true);
  });
});

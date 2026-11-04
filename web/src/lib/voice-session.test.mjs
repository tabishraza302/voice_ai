import { afterEach, describe, expect, mock, test } from "bun:test";
import { VoiceSession } from "./voice-session.ts";

const sessions = new Set();

class FakeTrack {
  enabled = true;
  stopped = false;
  stop() { this.stopped = true; }
}

class FakePeerConnection {
  connectionState = "new";
  iceGatheringState = "complete";
  localDescription = { sdp: "local-sdp", type: "offer" };
  closed = false;
  tracks = [];
  transceivers = [];
  channel = {
    ordered: true,
    readyState: "connecting",
    closed: false,
    sent: [],
    onopen: null,
    onmessage: null,
    onclose: null,
    close() {
      this.closed = true;
      this.readyState = "closed";
      this.onclose?.();
    },
    send(message) { this.sent.push(message); },
  };
  constructor(config) { this.config = config; sessions.add(this); }
  addTrack(track) { this.tracks.push(track); }
  addTransceiver(kind, options) { this.transceivers.push({ kind, options }); }
  createDataChannel(label, options) {
    this.channel.label = label;
    this.channel.options = options;
    queueMicrotask(() => {
      if (!this.channel.closed) {
        this.channel.readyState = "open";
        this.channel.onopen?.();
      }
    });
    return this.channel;
  }
  async createOffer() { return { sdp: "local-sdp", type: "offer" }; }
  async setLocalDescription(description) { this.localDescription = description; }
  async setRemoteDescription(description) {
    this.remoteDescription = description;
    this.connectionState = "connected";
  }
  close() { this.closed = true; this.connectionState = "closed"; }
}

function createHarness(options = {}) {
  const offerResponse = options.offerResponse ?? { ok: true, json: async () => ({ pc_id: "pc-1", sdp: "remote-sdp", type: "answer" }) };
  const track = new FakeTrack();
  const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
  const fetcher = options.fetcher ?? mock(async () => offerResponse);
  const peerConnectionFactory = mock((config) => new FakePeerConnection(config));
  const getUserMedia = options.getUserMedia ?? mock(async () => stream);
  const statuses = [];
  const session = new VoiceSession({
    getUserMedia,
    createPeerConnection: peerConnectionFactory,
    fetcher,
    onDataChannelMessage: options.onDataChannelMessage,
    onDataChannelOpen: options.onDataChannelOpen,
    onDataChannelClose: options.onDataChannelClose,
    onStatus: (status) => statuses.push(status),
  });
  const channel = () => [...sessions].at(-1)?.channel;
  return { session, track, stream, fetcher, peerConnectionFactory, getUserMedia, statuses, channel };
}

const OFFER_URL = "http://localhost:7860/api/offer";

const agent = { id: "agent-42" };

afterEach(() => {
  for (const peer of sessions) peer.close();
  sessions.clear();
});

describe("voice session lifecycle", () => {
  test("connects to the configured local offer endpoint and releases every resource on close", async () => {
    const { session, track, fetcher, peerConnectionFactory } = createHarness();

    await session.connect({ offerUrl: "http://localhost:7860/api/offer", agentId: "agent-42" });
    const peer = [...sessions].at(-1);

    expect(peerConnectionFactory).toHaveBeenCalledWith({ iceServers: [] });
    expect(fetcher).toHaveBeenCalledWith("http://localhost:7860/api/offer", expect.objectContaining({
      method: "POST",
      body: expect.stringContaining('"request_data":{"agent_id":"agent-42"}'),
    }));
    expect(peer.tracks).toHaveLength(1);
    expect(peer.transceivers).toEqual([
      { kind: "video", options: { direction: "sendrecv" } },
      { kind: "video", options: { direction: "sendonly" } },
    ]);
    expect(peer.remoteDescription).toEqual({ sdp: "remote-sdp", type: "answer" });

    session.close();

    expect(track.stopped).toBe(true);
    expect(peer.channel.closed).toBe(true);
    expect(peer.closed).toBe(true);
  });

  test("sends a bound guest session JWT as the Pipecat request_data", async () => {
    const { session, fetcher } = createHarness();
    const guestRequest = {
      session_id: "session-guest-1",
      join_token: "signed-guest-join-token",
    };

    await session.connect({
      offerUrl: "https://voice.example/api/offer",
      requestData: guestRequest,
    });

    const requestBody = JSON.parse(fetcher.mock.calls[0][1].body);
    expect(requestBody.request_data).toEqual(guestRequest);
    expect(requestBody.request_data.agent_id).toBeUndefined();
  });

  test("calls the injected fetcher with the global receiver", async () => {
    let receiver = null;
    const response = { ok: true, json: async () => ({ pc_id: "pc-1", sdp: "remote-sdp", type: "answer" }) };
    const harness = createHarness({
      fetcher: function () {
        receiver = this;
        return Promise.resolve(response);
      },
    });

    await harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id });

    // Native browser fetch throws "Illegal invocation" when called with the
    // dependencies object as its receiver.
    expect(receiver).toBe(globalThis);
  });

  test("stops a microphone stream that resolves after the caller cancels", async () => {
    let finishMedia;
    const harness = createHarness();
    harness.session = new VoiceSession({
      getUserMedia: () => new Promise((resolve) => { finishMedia = resolve; }),
      createPeerConnection: harness.peerConnectionFactory,
      fetcher: harness.fetcher,
    });
    const connecting = harness.session.connect({ offerUrl: "http://localhost:7860/api/offer", agentId: "agent-42" });
    await Promise.resolve();
    harness.session.close();
    finishMedia(harness.stream);

    await expect(connecting).rejects.toThrow("cancelled");
    expect(harness.track.stopped).toBe(true);
    expect(harness.fetcher).not.toHaveBeenCalled();
    expect(harness.peerConnectionFactory).not.toHaveBeenCalled();
  });

  test("rejects a failed offer and stops acquired media", async () => {
    const failedResponse = { ok: false, status: 503, text: async () => "pipeline unavailable" };
    const { session, track } = createHarness({ offerResponse: failedResponse });

    await expect(session.connect({ offerUrl: "http://localhost:7860/api/offer", agentId: "agent-42" })).rejects.toThrow("pipeline unavailable");
    expect(track.stopped).toBe(true);
    expect([...sessions].at(-1).closed).toBe(true);
  });
});

describe("sendText", () => {
  test("returns false when no data channel exists yet", () => {
    const harness = createHarness();

    expect(harness.session.sendText("hello")).toBe(false);
  });

});
describe("playback data channel", () => {
  const receipt = {
    type: "flow-playback-receipt",
    version: 1,
    binding: {
      session_id: "session-1",
      transport_epoch: "epoch-1",
      turn_id: "turn-1",
      generation_id: "generation-1",
      manifest_id: "manifest-1",
      fragment_id: "fragment-1",
      content_hash: "b".repeat(64),
      capability_report_hash: "a".repeat(64),
    },
    media_samples: { sample_start: 240, sample_end: 480, sample_rate: 48_000 },
    rendered_frames: { frame_start: 480, frame_end: 960, sample_rate: 96_000 },
    evidence_id: "c".repeat(32),
  };

  test("sends only validated receipts over the active ordered chat channel", async () => {
    const harness = createHarness();
    await harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id });
    const channel = harness.channel();

    expect(channel.options).toEqual({ ordered: true });
    expect(harness.session.sendPlaybackReceipt(receipt)).toBe(true);
    expect(channel.sent.at(-1)).toBe(JSON.stringify(receipt));
    expect(harness.session.sendPlaybackReceipt({ ...receipt, unbound: true })).toBe(false);
    channel.ordered = false;
    expect(harness.session.sendPlaybackReceipt(receipt)).toBe(false);
  });

  test("sends only exact v1 browser capabilities over the active ordered channel", async () => {
    const harness = createHarness();
    await harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id });
    const channel = harness.channel();
    const capabilities = {
      type: "flow-playback-capabilities",
      version: 1,
      session_id: "session-1",
      browser_tuple_hash: "d".repeat(64),
    };

    expect(harness.session.sendPlaybackCapabilities(capabilities)).toBe(true);
    expect(channel.sent.at(-1)).toBe(JSON.stringify(capabilities));
    expect(harness.session.sendPlaybackCapabilities({ ...capabilities, report_hash: "e".repeat(64) })).toBe(false);
    channel.ordered = false;
    expect(harness.session.sendPlaybackCapabilities(capabilities)).toBe(false);
  });

  test("dispatches control messages only from the current channel and ignores stale peer events", async () => {
    const received = [];
    const harness = createHarness({
      onDataChannelMessage: (event, channel) => received.push({ event, channel }),
    });
    await harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id });
    const activeChannel = harness.channel();
    const peer = [...sessions].at(-1);
    const otherChannel = {
      ordered: true,
      closed: false,
      close() { this.closed = true; },
    };

    peer.ondatachannel({ channel: otherChannel });
    otherChannel.onmessage?.({ data: "forged-channel" });
    activeChannel.onmessage({ data: "active-peer" });
    expect(otherChannel.closed).toBe(true);
    expect(received.map(({ event }) => event.data)).toEqual(["active-peer"]);
    expect(received[0].channel).toBe(activeChannel);

    harness.session.close();
    activeChannel.onmessage({ data: "stale-peer" });
    expect(received).toHaveLength(1);
  });
});

describe("mute management", () => {
  test("starts unmuted by default and signals enabled track status on open", async () => {
    const harness = createHarness();
    await harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id });
    const channel = harness.channel();

    expect(harness.session.isMuted()).toBe(false);
    expect(harness.track.enabled).toBe(true);
    expect(channel.sent).toContain(
      JSON.stringify({
        type: "signalling",
        message: { type: "trackStatus", receiver_index: 0, enabled: true },
      })
    );
  });

  test("preserves muted state set before media is connected", async () => {
    const harness = createHarness();
    harness.session.setMuted(true);
    expect(harness.session.isMuted()).toBe(true);

    await harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id });
    const channel = harness.channel();

    expect(harness.track.enabled).toBe(false);
    expect(channel.sent).toContain(
      JSON.stringify({
        type: "signalling",
        message: { type: "trackStatus", receiver_index: 0, enabled: false },
      })
    );
    expect(channel.sent).not.toContain(
      JSON.stringify({
        type: "signalling",
        message: { type: "trackStatus", receiver_index: 0, enabled: true },
      })
    );
  });

  test("toggles track enabled and sends signalling on active connection", async () => {
    const harness = createHarness();
    await harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id });
    const channel = harness.channel();

    harness.session.setMuted(true);
    expect(harness.session.isMuted()).toBe(true);
    expect(harness.track.enabled).toBe(false);
    expect(channel.sent.at(-1)).toBe(
      JSON.stringify({
        type: "signalling",
        message: { type: "trackStatus", receiver_index: 0, enabled: false },
      })
    );

    harness.session.setMuted(false);
    expect(harness.session.isMuted()).toBe(false);
    expect(harness.track.enabled).toBe(true);
    expect(channel.sent.at(-1)).toBe(
      JSON.stringify({
        type: "signalling",
        message: { type: "trackStatus", receiver_index: 0, enabled: true },
      })
    );
  });
});

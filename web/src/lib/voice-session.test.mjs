import { afterEach, describe, expect, mock, test } from "bun:test";
import { VoiceSession } from "./voice-session.ts";

const sessions = new Set();

class FakeTrack {
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
    readyState: "open",
    closed: false,
    sent: [],
    close() { this.closed = true; this.readyState = "closed"; },
    send(message) { this.sent.push(message); },
  };
  constructor(config) { this.config = config; sessions.add(this); }
  addTrack(track) { this.tracks.push(track); }
  addTransceiver(kind, options) { this.transceivers.push({ kind, options }); }
  createDataChannel(label, options) { this.channel.label = label; this.channel.options = options; return this.channel; }
  async createOffer() { return { sdp: "local-sdp", type: "offer" }; }
  async setLocalDescription(description) { this.localDescription = description; }
  async setRemoteDescription(description) { this.remoteDescription = description; }
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
  const activated = [];
  const session = new VoiceSession({
    getUserMedia,
    createPeerConnection: peerConnectionFactory,
    fetcher,
    onStatus: (status) => statuses.push(status),
    onActivated: (agent) => activated.push(agent),
  });
  const channel = () => [...sessions].at(-1)?.channel;
  return { session, track, stream, fetcher, peerConnectionFactory, getUserMedia, statuses, activated, channel };
}

const OFFER_URL = "http://localhost:7860/api/offer";

const agent = {
  id: "agent-42",
  slug: "assistant",
  name: "Assistant",
  description: "A test agent",
  persona_prompt: "Be concise.",
  llm_model: "qwen3.5:4b",
  llm_temperature: 0.7,
  llm_reasoning_effort: "",
  llm_max_tokens: 150,
  stt_provider: "mlx-audio",
  tts_voice_en: "af_heart",
  tts_voice_hi: "hf_alpha",
  tts_speed: 1,
  is_active: false,
  is_preset: false,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function permissionError(name) {
  return Object.assign(new Error("microphone unavailable"), { name });
}

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

describe("microphone before activation", () => {
  test("requests media, then activates, then connects", async () => {
    const harness = createHarness();
    const activate = mock(async () => agent);

    await harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id, isActive: false, activate });

    expect(harness.statuses).toEqual(["requesting_media", "activating", "connecting"]);
    expect(activate).toHaveBeenCalledTimes(1);
    expect(harness.activated).toEqual([agent]);
    expect(harness.getUserMedia).toHaveBeenCalledTimes(1);
  });

  test("does not activate when microphone permission is denied", async () => {
    const harness = createHarness({
      getUserMedia: mock(async () => { throw permissionError("NotAllowedError"); }),
    });
    const activate = mock(async () => agent);

    await expect(harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id, isActive: false, activate }))
      .rejects.toThrow("Microphone access was denied. Allow microphone access in your browser, then try again.");

    expect(activate).not.toHaveBeenCalled();
    expect(harness.activated).toEqual([]);
    expect(harness.statuses).toEqual(["requesting_media"]);
    expect(harness.peerConnectionFactory).not.toHaveBeenCalled();
  });

  test("skips activation when the agent is already active", async () => {
    const harness = createHarness();
    const activate = mock(async () => agent);

    await harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id, isActive: true, activate });

    expect(harness.statuses).toEqual(["requesting_media", "connecting"]);
    expect(activate).not.toHaveBeenCalled();
    expect(harness.activated).toEqual([]);
  });

  test("stops acquired media and propagates a rejected activation", async () => {
    const harness = createHarness();
    const activate = mock(async () => { throw new Error("Agent activation failed."); });

    await expect(harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id, isActive: false, activate }))
      .rejects.toThrow("Agent activation failed.");

    expect(harness.track.stopped).toBe(true);
    expect(harness.activated).toEqual([]);
    expect(harness.statuses).toEqual(["requesting_media", "activating"]);
    expect(harness.fetcher).not.toHaveBeenCalled();
    expect(harness.peerConnectionFactory).not.toHaveBeenCalled();
  });

  test("stops acquired media and reports cancellation during activation", async () => {
    let activationStarted;
    let finishActivation;
    const started = new Promise((resolve) => { activationStarted = resolve; });
    const activate = mock(() => new Promise((resolve) => {
      finishActivation = resolve;
      activationStarted();
    }));
    const harness = createHarness();

    const connecting = harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id, isActive: false, activate });
    await started;
    expect(harness.statuses).toEqual(["requesting_media", "activating"]);

    harness.session.close();
    finishActivation(agent);

    await expect(connecting).rejects.toThrow("cancelled");
    expect(harness.track.stopped).toBe(true);
    expect(harness.activated).toEqual([]);
    expect(harness.fetcher).not.toHaveBeenCalled();
  });
});

describe("sendText", () => {
  test("returns false when no data channel exists yet", () => {
    const harness = createHarness();

    expect(harness.session.sendText("hello")).toBe(false);
  });

  test("returns true and sends the text message on an open channel", async () => {
    const harness = createHarness();
    await harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id });
    const channel = harness.channel();

    expect(harness.session.sendText("hello")).toBe(true);
    expect(channel.sent).toEqual([JSON.stringify({ type: "send-text", text: "hello" })]);
  });

  test("returns false without throwing on a closed channel", async () => {
    const harness = createHarness();
    await harness.session.connect({ offerUrl: OFFER_URL, agentId: agent.id });
    const channel = harness.channel();
    channel.readyState = "closed";

    expect(harness.session.sendText("hello")).toBe(false);
    expect(() => harness.session.sendText("again")).not.toThrow();
    expect(channel.sent).toEqual([]);
  });
});

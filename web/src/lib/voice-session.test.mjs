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
  const fetcher = mock(async () => offerResponse);
  const peerConnectionFactory = mock((config) => new FakePeerConnection(config));
  const session = new VoiceSession({
    getUserMedia: mock(async () => stream),
    createPeerConnection: peerConnectionFactory,
    fetcher,
  });
  return { session, track, stream, fetcher, peerConnectionFactory };
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

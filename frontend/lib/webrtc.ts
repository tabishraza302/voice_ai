import { api } from "./api";

export type CallSessionGrant = {
  session_id: string;
  signaling_token: string;
};

export type CallCaption = {
  role: "user" | "assistant";
  text: string;
  final?: boolean;
};

export type CallTransportEvent =
  | { type: "connected" }
  | { type: "reconnecting" }
  | { type: "speaking"; value: boolean }
  | { type: "caption"; caption: CallCaption }
  | { type: "audio-blocked" }
  | { type: "audio-ready" }
  | { type: "completed" }
  | { type: "ended"; reason: "local" | "remote" }
  | { type: "failed" };

export type SmallWebRtcCall = {
  connect(grant: ValidatedCallSessionGrant): Promise<void>;
  setMuted(muted: boolean): void;
  enableAudio(): Promise<void>;
  end(): Promise<void>;
  close(): void;
};

type SessionId = string & { readonly __brand: "SessionId" };
type SignalingToken = string & { readonly __brand: "SignalingToken" };

type ValidatedCallSessionGrant = {
  sessionId: SessionId;
  signalingToken: SignalingToken;
};

type CandidatePatch = {
  candidate: string;
  sdp_mid: string;
  sdp_mline_index: number;
};

type SignalingAnswer = {
  sdp: string;
  pcId: string;
  renegotiationToken: string;
};

type CallOptions = {
  iceServers: RTCIceServer[];
  localStream: MediaStream;
  audioElement: HTMLAudioElement;
  onEvent: (event: CallTransportEvent) => void;
};

function isUnknownArray(value: unknown): value is unknown[] {
  return Array.isArray(value);
}

function isSessionId(value: unknown): value is SessionId {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
  );
}

function isSignalingToken(value: unknown): value is SignalingToken {
  return typeof value === "string" && /^[A-Za-z0-9_-]{32,256}$/.test(value);
}

export function parseCallSessionGrant(value: unknown): ValidatedCallSessionGrant {
  if (
    typeof value !== "object" ||
    value === null ||
    isUnknownArray(value) ||
    !("session_id" in value) ||
    !("signaling_token" in value) ||
    !isSessionId(value.session_id) ||
    !isSignalingToken(value.signaling_token)
  ) {
    throw new Error("The call service returned an invalid session.");
  }

  return {
    sessionId: value.session_id,
    signalingToken: value.signaling_token,
  };
}

function isIceServerUrls(value: unknown): value is string | string[] {
  return (
    typeof value === "string" ||
    (Array.isArray(value) && value.length > 0 && value.every((entry) => typeof entry === "string"))
  );
}

function parseIceServers(value: unknown): RTCIceServer[] {
  if (
    typeof value !== "object" ||
    value === null ||
    isUnknownArray(value) ||
    !("iceServers" in value) ||
    !Array.isArray(value.iceServers) ||
    value.iceServers.length === 0
  ) {
    throw new Error("Voice calling is not available right now.");
  }

  const servers: RTCIceServer[] = [];
  for (const valueServer of value.iceServers) {
    if (
      typeof valueServer !== "object" ||
      valueServer === null ||
      isUnknownArray(valueServer) ||
      !("urls" in valueServer) ||
      !isIceServerUrls(valueServer.urls) ||
      ("username" in valueServer &&
        valueServer.username !== undefined &&
        typeof valueServer.username !== "string") ||
      ("credential" in valueServer &&
        valueServer.credential !== undefined &&
        typeof valueServer.credential !== "string")
    ) {
      throw new Error("Voice calling is not available right now.");
    }

    const server: RTCIceServer = { urls: valueServer.urls };
    if ("username" in valueServer && typeof valueServer.username === "string") {
      server.username = valueServer.username;
    }
    if ("credential" in valueServer && typeof valueServer.credential === "string") {
      server.credential = valueServer.credential;
    }
    servers.push(server);
  }

  return servers;
}

export async function loadIceServers(): Promise<RTCIceServer[]> {
  const response = await api<unknown>("/signaling/ice-servers");
  return parseIceServers(response);
}

function parseSignalingAnswer(value: unknown): SignalingAnswer {
  if (
    typeof value !== "object" ||
    value === null ||
    isUnknownArray(value) ||
    !("type" in value) ||
    value.type !== "answer" ||
    !("sdp" in value) ||
    typeof value.sdp !== "string" ||
    value.sdp.length === 0 ||
    !("pc_id" in value) ||
    typeof value.pc_id !== "string" ||
    value.pc_id.length === 0 ||
    !("renegotiation_token" in value) ||
    !isSignalingToken(value.renegotiation_token)
  ) {
    throw new Error("The voice connection returned an invalid answer.");
  }

  return {
    sdp: value.sdp,
    pcId: value.pc_id,
    renegotiationToken: value.renegotiation_token,
  };
}

class SmallWebRtcCallController implements SmallWebRtcCall {
  private peerConnection: RTCPeerConnection | null = null;
  private dataChannel: RTCDataChannel | null = null;
  private pcId: string | null = null;
  private renegotiationToken: string | null = null;
  private connectionTimeout: number | null = null;
  private disconnectedTimeout: number | null = null;
  private candidateFlush: Promise<void> | null = null;
  private candidates: CandidatePatch[] = [];
  private negotiation: Promise<void> | null = null;
  private negotiationQueued = false;
  private restartQueued = false;
  private restartAttempted = false;
  private remoteAudioContext: AudioContext | null = null;
  private remoteAudioSource: MediaStreamAudioSourceNode | null = null;
  private remoteAnalyser: AnalyserNode | null = null;
  private speechTimer: number | null = null;
  private speakingUntil = 0;
  private speaking = false;
  private closing = false;
  private initialSignalComplete = false;

  constructor(private readonly options: CallOptions) {}

  async connect(grant: ValidatedCallSessionGrant): Promise<void> {
    if (this.peerConnection || this.closing) {
      throw new Error("This call connection has already been started.");
    }


    try {
      const connection = new RTCPeerConnection({ iceServers: this.options.iceServers });
      this.peerConnection = connection;
      connection.onicecandidate = (event) => this.handleIceCandidate(event);
      connection.ontrack = (event) => this.handleRemoteTrack(event);
      connection.onconnectionstatechange = () => this.handleConnectionState();
      connection.onsignalingstatechange = () => {
        if (connection.signalingState === "stable" && this.negotiationQueued) {
          this.requestNegotiation();
        }
      };
      connection.onnegotiationneeded = () => {
        if (this.initialSignalComplete) this.requestNegotiation();
      };

      for (const track of this.options.localStream.getAudioTracks()) {
        connection.addTrack(track, this.options.localStream);
      }

      const dataChannel = connection.createDataChannel("caller-control");
      this.dataChannel = dataChannel;
      dataChannel.onmessage = (event) => this.handleDataMessage(event.data);

      const offer = await connection.createOffer();
      await connection.setLocalDescription(offer);
      const localDescription = connection.localDescription;
      if (!localDescription?.sdp) {
        throw new Error("The browser could not create a voice offer.");
      }

      this.connectionTimeout = window.setTimeout(() => this.fail(), 40_000);
      const response = await api<unknown>("/signaling/offer", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          sdp: localDescription.sdp,
          type: localDescription.type,
          signaling_token: grant.signalingToken,
        }),
      });
      const answer = parseSignalingAnswer(response);
      this.pcId = answer.pcId;
      this.renegotiationToken = answer.renegotiationToken;
      await connection.setRemoteDescription({ type: "answer", sdp: answer.sdp });
      this.initialSignalComplete = true;
      this.flushCandidates();
    } catch (error) {
      this.close();
      throw error;
    }
  }

  setMuted(muted: boolean): void {
    for (const track of this.options.localStream.getAudioTracks()) track.enabled = !muted;
  }

  async enableAudio(): Promise<void> {
    try {
      await this.remoteAudioContext?.resume();
      await this.options.audioElement.play();
      this.emit({ type: "audio-ready" });
    } catch {
      this.emit({ type: "audio-blocked" });
    }
  }

  async end(): Promise<void> {
    if (this.closing) return;
    this.closing = true;
    if (this.dataChannel?.readyState === "open") {
      try {
        this.dataChannel.send(JSON.stringify({ type: "end" }));
        const completion = Promise.withResolvers<void>();
        window.setTimeout(completion.resolve, 120);
        await completion.promise;
      } catch {
        // Closing the peer connection still ends the session when the channel cannot send.
      }
    }
    this.dispose();
    this.emit({ type: "ended", reason: "local" });
  }

  close(): void {
    if (this.closing) return;
    this.closing = true;
    this.dispose();
  }

  private handleIceCandidate(event: RTCPeerConnectionIceEvent): void {
    const candidate = event.candidate;
    if (!candidate || candidate.sdpMid === null || candidate.sdpMLineIndex === null) return;

    const text = candidate.candidate.startsWith("candidate:")
      ? candidate.candidate.slice("candidate:".length)
      : candidate.candidate;
    this.candidates.push({
      candidate: text,
      sdp_mid: candidate.sdpMid,
      sdp_mline_index: candidate.sdpMLineIndex,
    });
    this.flushCandidates();
  }

  private flushCandidates(): void {
    if (
      this.closing ||
      !this.pcId ||
      !this.renegotiationToken ||
      this.candidates.length === 0 ||
      this.candidateFlush
    ) {
      return;
    }

    const task = this.sendCandidateBatches();
    this.candidateFlush = task;
    void task
      .catch(() => this.fail())
      .finally(() => {
        if (this.candidateFlush === task) this.candidateFlush = null;
        if (this.candidates.length > 0) this.flushCandidates();
      });
  }

  private async sendCandidateBatches(): Promise<void> {
    while (!this.closing && this.candidates.length > 0 && this.pcId && this.renegotiationToken) {
      const candidates = this.candidates.splice(0, 64);
      const response = await api<unknown>("/signaling/candidates", {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          pc_id: this.pcId,
          renegotiation_token: this.renegotiationToken,
          candidates,
        }),
      });
      if (
        typeof response !== "object" ||
        response === null ||
        isUnknownArray(response) ||
        !("accepted" in response) ||
        response.accepted !== true
      ) {
        throw new Error("The voice connection rejected an ICE candidate.");
      }
    }
  }

  private handleRemoteTrack(event: RTCTrackEvent): void {
    if (event.track.kind !== "audio") return;
    const stream = event.streams[0] ?? new MediaStream([event.track]);
    this.options.audioElement.srcObject = stream;
    void this.options.audioElement
      .play()
      .then(() => this.emit({ type: "audio-ready" }))
      .catch(() => this.emit({ type: "audio-blocked" }));
    this.monitorRemoteSpeech(stream);
  }

  private monitorRemoteSpeech(stream: MediaStream): void {
    if (typeof window.AudioContext === "undefined") return;
    try {
      const context = new window.AudioContext();
      const source = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);
      this.remoteAudioContext = context;
      this.remoteAudioSource = source;
      this.remoteAnalyser = analyser;
      const samples = new Float32Array(analyser.fftSize);
      this.speechTimer = window.setInterval(() => {
        if (!this.remoteAnalyser) return;
        this.remoteAnalyser.getFloatTimeDomainData(samples);
        let energy = 0;
        for (const sample of samples) energy += sample * sample;
        if (Math.sqrt(energy / samples.length) > 0.018) {
          this.speakingUntil = Date.now() + 350;
        }
        const speaking = Date.now() < this.speakingUntil;
        if (speaking !== this.speaking) {
          this.speaking = speaking;
          this.emit({ type: "speaking", value: speaking });
        }
      }, 100);
      void context.resume().catch(() => undefined);
    } catch {
      return;
    }
  }

  private handleConnectionState(): void {
    const connection = this.peerConnection;
    if (!connection || this.closing) return;

    if (connection.connectionState === "connected") {
      this.clearConnectionTimers();
      this.restartAttempted = false;
      this.emit({ type: "connected" });
      return;
    }

    if (connection.connectionState === "disconnected") {
      this.emit({ type: "reconnecting" });
      if (this.disconnectedTimeout === null) {
        this.disconnectedTimeout = window.setTimeout(() => {
          this.disconnectedTimeout = null;
          if (this.peerConnection?.connectionState === "disconnected") {
            this.restartIce();
          }
        }, 8_000);
      }
      return;
    }

    if (connection.connectionState === "failed") {
      this.restartIce();
      return;
    }

    if (connection.connectionState === "closed") this.finishRemote();
  }

  private restartIce(): void {
    if (this.closing) return;
    if (this.restartAttempted) {
      this.fail();
      return;
    }
    this.restartAttempted = true;
    this.emit({ type: "reconnecting" });
    this.requestNegotiation(true);
  }

  private requestNegotiation(restart = false): void {
    if (this.closing || !this.initialSignalComplete) return;
    this.negotiationQueued = true;
    this.restartQueued ||= restart;
    if (this.negotiation || this.peerConnection?.signalingState !== "stable") return;

    const task = this.sendRenegotiations();
    this.negotiation = task;
    void task
      .catch(() => this.fail())
      .finally(() => {
        if (this.negotiation === task) this.negotiation = null;
        if (this.negotiationQueued && this.peerConnection?.signalingState === "stable") {
          this.requestNegotiation();
        }
      });
  }

  private async sendRenegotiations(): Promise<void> {
    const connection = this.peerConnection;
    if (!connection || !this.pcId || !this.renegotiationToken) return;

    while (!this.closing && this.negotiationQueued && connection.signalingState === "stable") {
      const restart = this.restartQueued;
      this.negotiationQueued = false;
      this.restartQueued = false;
      const offer = await connection.createOffer(restart ? { iceRestart: true } : undefined);
      await connection.setLocalDescription(offer);
      const localDescription = connection.localDescription;
      if (!localDescription?.sdp) {
        throw new Error("The browser could not create a renegotiation offer.");
      }
      const response = await api<unknown>("/signaling/offer", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          sdp: localDescription.sdp,
          type: localDescription.type,
          pc_id: this.pcId,
          renegotiation_token: this.renegotiationToken,
          restart_pc: restart,
        }),
      });
      const answer = parseSignalingAnswer(response);
      if (answer.pcId !== this.pcId || answer.renegotiationToken !== this.renegotiationToken) {
        throw new Error("The voice connection changed during renegotiation.");
      }
      await connection.setRemoteDescription({ type: "answer", sdp: answer.sdp });
    }
  }

  private handleDataMessage(data: unknown): void {
    if (typeof data !== "string") return;
    let value: unknown;
    try {
      value = JSON.parse(data);
    } catch {
      return;
    }
    if (
      typeof value !== "object" ||
      value === null ||
      isUnknownArray(value) ||
      !("type" in value)
    ) {
      return;
    }
    if (value.type === "call.completed") {
      this.finishCompleted();
      return;
    }
    if (
      value.type === "caption" &&
      "role" in value &&
      (value.role === "user" || value.role === "assistant") &&
      "text" in value &&
      typeof value.text === "string" &&
      value.text.trim().length > 0 &&
      value.text.length <= 12_000
    ) {
      const final = "final" in value && typeof value.final === "boolean" ? value.final : true;
      this.emit({ type: "caption", caption: { role: value.role, text: value.text, final } });
      return;
    }

    if (value.type === "signalling" && "message" in value) {
      const message = value.message;
      if (
        typeof message !== "object" ||
        message === null ||
        isUnknownArray(message) ||
        !("type" in message)
      ) {
        return;
      }
      if (message.type === "renegotiate") this.requestNegotiation();
      if (message.type === "peerLeft") this.finishRemote();
    }
  }

  private finishCompleted(): void {
    if (this.closing) return;
    this.closing = true;
    this.dispose();
    this.emit({ type: "completed" });
  }

  private finishRemote(): void {
    if (this.closing) return;
    this.closing = true;
    this.dispose();
    this.emit({ type: "ended", reason: "remote" });
  }

  private fail(): void {
    if (this.closing) return;
    this.closing = true;
    this.dispose();
    this.emit({ type: "failed" });
  }

  private clearConnectionTimers(): void {
    if (this.connectionTimeout !== null) window.clearTimeout(this.connectionTimeout);
    if (this.disconnectedTimeout !== null) window.clearTimeout(this.disconnectedTimeout);
    this.connectionTimeout = null;
    this.disconnectedTimeout = null;
  }

  private dispose(): void {
    this.clearConnectionTimers();
    this.options.localStream.getTracks().forEach((track) => track.stop());
    if (this.speechTimer !== null) window.clearInterval(this.speechTimer);
    this.speechTimer = null;
    this.remoteAudioSource?.disconnect();
    this.remoteAnalyser?.disconnect();
    this.remoteAudioSource = null;
    this.remoteAnalyser = null;
    if (this.remoteAudioContext) void this.remoteAudioContext.close().catch(() => undefined);
    this.remoteAudioContext = null;
    this.options.audioElement.pause();
    this.options.audioElement.srcObject = null;
    if (this.dataChannel) {
      this.dataChannel.onmessage = null;
      if (this.dataChannel.readyState !== "closed") this.dataChannel.close();
    }
    if (this.peerConnection) {
      this.peerConnection.onicecandidate = null;
      this.peerConnection.ontrack = null;
      this.peerConnection.onconnectionstatechange = null;
      this.peerConnection.onsignalingstatechange = null;
      this.peerConnection.onnegotiationneeded = null;
      if (this.peerConnection.signalingState !== "closed") this.peerConnection.close();
    }
    this.dataChannel = null;
    this.peerConnection = null;
  }

  private emit(event: CallTransportEvent): void {
    this.options.onEvent(event);
  }
}

export function createSmallWebRtcCall(options: CallOptions): SmallWebRtcCall {
  return new SmallWebRtcCallController(options);
}

export async function requestMicrophone(): Promise<MediaStream> {
  if (typeof window !== "undefined" && !window.isSecureContext) {
    throw new Error("Microphone access requires a secure connection.");
  }
  if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
    throw new Error("Microphone access is not available in this browser.");
  }

  return navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    video: false,
  });
}

export function microphoneErrorMessage(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === "NotAllowedError" || error.name === "PermissionDeniedError") {
      return "Microphone permission was denied. Allow access in your browser settings, then try again.";
    }
    if (error.name === "NotFoundError" || error.name === "DevicesNotFoundError") {
      return "No microphone was found. Connect a microphone and try again.";
    }
    if (error.name === "NotReadableError" || error.name === "TrackStartError") {
      return "The microphone is in use by another app. Close it there and try again.";
    }
    if (error.name === "OverconstrainedError" || error.name === "ConstraintNotSatisfiedError") {
      return "The microphone cannot meet the browser’s audio requirements. Check your device and try again.";
    }
    if (error.name === "SecurityError") {
      return "Microphone access requires a secure connection. Open this page over HTTPS and try again.";
    }
  }

  if (error instanceof Error && error.message === "Microphone access requires a secure connection.") {
    return "Microphone access requires a secure connection. Open this page over HTTPS and try again.";
  }
  if (error instanceof Error && error.message === "Microphone access is not available in this browser.") {
    return "This browser cannot access a microphone. Try a current browser on a secure connection.";
  }

  return "Microphone access could not be started. Check your browser settings and try again.";
}

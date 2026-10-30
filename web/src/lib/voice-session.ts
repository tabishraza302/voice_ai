import type { Agent } from "@/types/agent";

export interface VoiceSessionConnection {
  peerConnection: RTCPeerConnection;
  dataChannel: RTCDataChannel;
  localStream: MediaStream;
  pcId: string;
}

export interface VoiceSessionDependencies {
  getUserMedia: (constraints: MediaStreamConstraints) => Promise<MediaStream>;
  createPeerConnection: (configuration: RTCConfiguration) => RTCPeerConnection;
  fetcher: typeof fetch;
  onDataChannelMessage?: (event: MessageEvent) => void;
  onRemoteTrack?: (event: RTCTrackEvent) => void;
  onConnectionState?: (state: RTCPeerConnectionState) => void;
  onStatus?: (status: "activating" | "requesting_media" | "connecting") => void;
  onActivated?: (agent: Agent) => void;
}

type GuestJoinRequestData = {
  session_id: string;
  join_token: string;
};

type VoiceSessionConnectOptions = {
  offerUrl: string;
  isActive?: boolean;
  activate?: () => Promise<Agent>;
} & (
  | { agentId: string; requestData?: never }
  | { agentId?: never; requestData: GuestJoinRequestData }
);

export class VoiceSession {
  private generation = 0;
  private abortController: AbortController | null = null;
  private peerConnection: RTCPeerConnection | null = null;
  private localStream: MediaStream | null = null;
  private dataChannel: RTCDataChannel | null = null;
  private pcId: string | null = null;
  private muted = false;

  constructor(private readonly dependencies: VoiceSessionDependencies) {}

  async connect(options: VoiceSessionConnectOptions): Promise<VoiceSessionConnection> {
    if (this.abortController || this.peerConnection) throw new Error("A voice session connection is already starting.");
    const requestData = options.requestData ??
      (options.agentId ? { agent_id: options.agentId } : null);
    if (!requestData) throw new Error("A voice session must include an agent or guest join request.");
    const generation = ++this.generation;
    const controller = new AbortController();
    this.abortController = controller;
    const isCurrent = () => generation === this.generation && !controller.signal.aborted;

    try {
      this.dependencies.onStatus?.("requesting_media");
      let stream: MediaStream;
      try {
        stream = await this.dependencies.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      } catch (error) {
        const name = error && typeof error === "object" && "name" in error ? error.name : "";
        if (name === "NotAllowedError" || name === "PermissionDeniedError") {
          throw new Error("Microphone access was denied. Allow microphone access in your browser, then try again.");
        }
        if (name === "NotFoundError" || name === "DevicesNotFoundError") {
          throw new Error("No microphone was found. Connect or enable an audio input device, then try again.");
        }
        throw new Error("Unable to capture microphone input.");
      }
      if (!isCurrent()) {
        stream.getTracks().forEach((track) => track.stop());
        throw new Error("Voice session connection was cancelled.");
      }
      this.localStream = stream;
      if (this.muted) {
        stream.getAudioTracks().forEach((track) => { track.enabled = false; });
      }

      if (!options.isActive && options.activate) {
        this.dependencies.onStatus?.("activating");
        const activated = await options.activate();
        this.assertCurrent(isCurrent);
        this.dependencies.onActivated?.(activated);
      }

      this.dependencies.onStatus?.("connecting");
      const peerConnection = this.dependencies.createPeerConnection({ iceServers: [] });
      this.peerConnection = peerConnection;
      const audioTrack = stream.getAudioTracks()[0];
      if (audioTrack) peerConnection.addTrack(audioTrack, stream);
      else peerConnection.addTransceiver("audio", { direction: "sendrecv" });
      peerConnection.addTransceiver("video", { direction: "sendrecv" });
      peerConnection.addTransceiver("video", { direction: "sendonly" });

      const dataChannel = peerConnection.createDataChannel("chat", { ordered: true });
      this.dataChannel = dataChannel;
      dataChannel.onmessage = (event) => {
        if (isCurrent()) this.dependencies.onDataChannelMessage?.(event);
      };
      dataChannel.onopen = () => {
        if (!isCurrent()) return;
        this.sendMessage({
          type: "signalling",
          message: { type: "trackStatus", receiver_index: 0, enabled: !this.muted },
        });
        this.sendMessage({
          label: "rtvi-ai",
          type: "client-ready",
          id: "client-ready",
          data: { version: "1.0.0", about: { library: "voice-ai-web", platform: "browser" } },
        });
      };
      peerConnection.ondatachannel = ({ channel }) => {
        channel.onmessage = (event) => {
          if (isCurrent()) this.dependencies.onDataChannelMessage?.(event);
        };
      };
      peerConnection.ontrack = (event) => {
        if (isCurrent()) this.dependencies.onRemoteTrack?.(event);
      };
      peerConnection.onconnectionstatechange = () => {
        if (isCurrent()) this.dependencies.onConnectionState?.(peerConnection.connectionState);
      };
      peerConnection.onicecandidate = async ({ candidate }) => {
        if (!candidate || !isCurrent() || !this.pcId) return;
        try {
          await this.request(options.offerUrl, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            signal: controller.signal,
            body: JSON.stringify({
              pc_id: this.pcId,
              candidates: [{
                candidate: candidate.candidate,
                sdp_mid: candidate.sdpMid,
                sdp_mline_index: candidate.sdpMLineIndex,
              }],
            }),
          });
        } catch {
          // ICE candidates are also bundled in the initial offer.
        }
      };

      const offer = await peerConnection.createOffer();
      this.assertCurrent(isCurrent);
      await peerConnection.setLocalDescription(offer);
      this.assertCurrent(isCurrent);
      await this.waitForIceGathering(peerConnection, controller.signal, isCurrent);
      this.assertCurrent(isCurrent);

      const response = await this.request(options.offerUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          sdp: peerConnection.localDescription?.sdp || offer.sdp,
          type: peerConnection.localDescription?.type || offer.type,
          request_data: requestData,
        }),
      });
      this.assertCurrent(isCurrent);
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Voice pipeline rejected connection (HTTP ${response.status}): ${detail || "Verify the local voice service is running."}`);
      }
      const answer = await response.json();
      this.assertCurrent(isCurrent);
      if (typeof answer.pc_id !== "string" || typeof answer.sdp !== "string" || typeof answer.type !== "string") {
        throw new Error("Voice pipeline returned an invalid WebRTC answer.");
      }
      this.pcId = answer.pc_id;
      await peerConnection.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
      this.assertCurrent(isCurrent);
      this.abortController = null;
      return { peerConnection, dataChannel, localStream: stream, pcId: answer.pc_id };
    } catch (error) {
      if (generation === this.generation) this.dispose();
      throw error;
    }
  }

  sendText(text: string): boolean {
    return this.sendMessage({ type: "send-text", text });
  }

  sendPing(): void {
    if (this.dataChannel?.readyState === "open") {
      try { this.dataChannel.send("ping"); } catch { /* transient keepalive errors are harmless */ }
    }
  }

  setMuted(muted: boolean): void {
    this.muted = muted;
    this.localStream?.getAudioTracks().forEach((track) => { track.enabled = !muted; });
    this.sendMessage({
      type: "signalling",
      message: { type: "trackStatus", receiver_index: 0, enabled: !muted },
    });
  }

  isMuted(): boolean {
    return this.muted;
  }

  getLocalStream(): MediaStream | null {
    return this.localStream;
  }

  close(): void {
    this.generation += 1;
    this.abortController?.abort();
    this.dispose();
  }

  /**
   * Calls the injected fetch through the global receiver. A browser's native
   * `fetch` throws "Illegal invocation" when invoked as a method of another
   * object, which property-call syntax on `this.dependencies` would do.
   */
  private request(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    return this.dependencies.fetcher.call(globalThis, input, init);
  }

  private sendMessage(message: unknown): boolean {
    if (this.dataChannel?.readyState !== "open") return false;
    try {
      this.dataChannel.send(JSON.stringify(message));
      return true;
    } catch {
      // The connection may close between the readyState check and the send.
      return false;
    }
  }

  private assertCurrent(isCurrent: () => boolean): void {
    if (!isCurrent()) throw new Error("Voice session connection was cancelled.");
  }

  private async waitForIceGathering(
    peerConnection: RTCPeerConnection,
    signal: AbortSignal,
    isCurrent: () => boolean,
  ): Promise<void> {
    if (peerConnection.iceGatheringState === "complete") return;
    await new Promise<void>((resolve, reject) => {
      const finish = () => {
        clearTimeout(timer);
        peerConnection.removeEventListener("icegatheringstatechange", checkState);
        signal.removeEventListener("abort", cancel);
      };
      const checkState = () => {
        if (peerConnection.iceGatheringState === "complete") {
          finish();
          resolve();
        }
      };
      const cancel = () => {
        finish();
        reject(new Error("Voice session connection was cancelled."));
      };
      const timer = setTimeout(() => {
        finish();
        resolve();
      }, 600);
      peerConnection.addEventListener("icegatheringstatechange", checkState);
      signal.addEventListener("abort", cancel, { once: true });
      if (!isCurrent()) cancel();
    });
  }

  private dispose(): void {
    this.localStream?.getTracks().forEach((track) => track.stop());
    this.localStream = null;
    this.dataChannel?.close();
    this.dataChannel = null;
    this.peerConnection?.close();
    this.peerConnection = null;
    this.pcId = null;
    this.abortController = null;
  }
}


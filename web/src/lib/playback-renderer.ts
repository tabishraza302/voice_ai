import {
  MAX_PLAYBACK_SAMPLE_RATE,
  MIN_PLAYBACK_SAMPLE_RATE,
  parsePlaybackControl,
  parsePlaybackReceipt,
  parseRenderedObservation,
} from "./playback-protocol";
import type {
  PlaybackBinding,
  PlaybackCancellation,
  PlaybackEnableGrant,
  PlaybackMarker,
  PlaybackReceiptMessage,
  PcmSampleRange,
  RenderedFrameRange,
} from "./playback-protocol";

export type {
  PlaybackBinding,
  PlaybackCancellation,
  PlaybackEnableGrant,
  PlaybackMarker,
  PlaybackReceiptMessage,
  PcmSampleRange,
  RenderedFrameRange,
} from "./playback-protocol";

interface AudioDataLike {
  timestamp: number;
  sampleRate: number;
  numberOfFrames: number;
  numberOfChannels: number;
  copyTo(destination: Float32Array, options: { planeIndex: number; format: "f32-planar" }): void;
  close(): void;
}

interface TrackProcessorLike {
  readable: ReadableStream<AudioDataLike>;
}

type TrackProcessorConstructor = new (options: { track: MediaStreamTrack }) => TrackProcessorLike;
type BrandVersion = { brand: string; version: string };
type UserAgentDataLike = {
  brands: BrandVersion[];
  mobile: boolean;
  platform: string;
  getHighEntropyValues: (hints: string[]) => Promise<{
    architecture?: string;
    bitness?: string;
    fullVersionList?: BrandVersion[];
    platformVersion?: string;
    wow64?: boolean;
  }>;
};
type NavigatorWithUserAgentData = Navigator & { userAgentData?: UserAgentDataLike };
type RuntimeWithPlayback = {
  crypto?: Crypto | null;
  AudioData?: new (...args: never[]) => unknown;
  AudioWorkletNode?: typeof AudioWorkletNode;
  MediaStreamTrackProcessor?: TrackProcessorConstructor;
};

type PlaybackRendererOptions = {
  authorization: PlaybackEnableGrant;
  sendReceipt: (receipt: PlaybackReceiptMessage) => boolean;
  onUnsupported?: (reason: string) => void;
  onInterrupted?: (cancellation: PlaybackCancellation) => void;
  runtime?: RuntimeWithPlayback;
};

const MAX_PENDING_RECEIPTS = 128;
const MAX_PENDING_MARKERS = 128;
const MAX_AUDIO_CHUNK_FRAMES = 192_000;
const MAX_SEEN_MARKERS = 4096;
function isSafeInteger(value: unknown, minimum = 0): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
}

function samePlaybackScope(
  authorization: PlaybackEnableGrant,
  value: Pick<PlaybackBinding, "session_id" | "transport_epoch" | "capability_report_hash">,
): boolean {
  return authorization.session_id === value.session_id
    && authorization.transport_epoch === value.transport_epoch
    && authorization.capability_report_hash === value.capability_report_hash;
}

function markerKey(marker: PlaybackMarker): string {
  const binding = marker.binding;
  return [
    binding.session_id,
    binding.transport_epoch,
    binding.turn_id,
    binding.generation_id,
    binding.manifest_id,
    binding.fragment_id,
    binding.content_hash,
    binding.capability_report_hash,
  ].join("\u0000");
}

function isBrandVersion(value: unknown): value is BrandVersion {
  return typeof value === "object"
    && value !== null
    && "brand" in value
    && typeof value.brand === "string"
    && value.brand.length > 0
    && value.brand.length <= 128
    && "version" in value
    && typeof value.version === "string"
    && value.version.length > 0
    && value.version.length <= 128;
}

function compareOrdinal(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function sortedBrandVersions(values: unknown): Array<[string, string]> | null {
  if (!Array.isArray(values) || values.length === 0 || values.length > 32 || !values.every(isBrandVersion)) return null;
  return values
    .map(({ brand, version }) => [brand, version] as [string, string])
    .sort(([brandA, versionA], [brandB, versionB]) => (
      compareOrdinal(brandA, brandB) || compareOrdinal(versionA, versionB)
    ));
}

/**
 * Hashes the UTF-8 JSON tuple for `flow-playback-browser-tuple-v1`. Its keys
 * appear in this exact order: `schema`, `user_agent`, `platform`,
 * `platform_version`, `architecture`, `bitness`, `brands`, `full_version_list`,
 * `mobile`, `wow64`, `audio_context_sample_rate`,
 * `media_stream_track_processor`, `audio_data`, `audio_worklet_node`. The two
 * brand lists are ordinally sorted `[brand, version]` pairs. Missing UA-CH,
 * renderer APIs, or Web Crypto returns `null`; the server must compare this
 * SHA-256 with an operator-qualified browser tuple before issuing a grant.
 */
export async function computePlaybackBrowserTupleHash(
  contextSampleRate: number,
  navigatorValue: NavigatorWithUserAgentData = globalThis.navigator as NavigatorWithUserAgentData,
  runtime: RuntimeWithPlayback = globalThis as RuntimeWithPlayback,
): Promise<string | null> {
  const userAgentData = navigatorValue?.userAgentData;
  const cryptography = runtime.crypto;
  if (
    !userAgentData
    || typeof userAgentData.getHighEntropyValues !== "function"
    || !Array.isArray(userAgentData.brands)
    || typeof userAgentData.mobile !== "boolean"
    || typeof userAgentData.platform !== "string"
    || userAgentData.platform.length === 0
    || typeof navigatorValue.userAgent !== "string"
    || navigatorValue.userAgent.length === 0
    || !Number.isSafeInteger(contextSampleRate)
    || contextSampleRate < MIN_PLAYBACK_SAMPLE_RATE
    || contextSampleRate > MAX_PLAYBACK_SAMPLE_RATE
    || typeof runtime.MediaStreamTrackProcessor !== "function"
    || typeof runtime.AudioData !== "function"
    || typeof runtime.AudioWorkletNode !== "function"
    || !cryptography
    || typeof cryptography.subtle?.digest !== "function"
    || typeof cryptography.getRandomValues !== "function"
    || typeof TextEncoder !== "function"
  ) return null;

  try {
    const highEntropy = await userAgentData.getHighEntropyValues([
      "architecture",
      "bitness",
      "fullVersionList",
      "platformVersion",
      "wow64",
    ]);
    const brands = sortedBrandVersions(userAgentData.brands);
    const fullVersionList = sortedBrandVersions(highEntropy.fullVersionList);
    if (
      !brands
      || !fullVersionList
      || typeof highEntropy.architecture !== "string"
      || highEntropy.architecture.length === 0
      || typeof highEntropy.bitness !== "string"
      || highEntropy.bitness.length === 0
      || typeof highEntropy.platformVersion !== "string"
      || highEntropy.platformVersion.length === 0
      || typeof highEntropy.wow64 !== "boolean"
  ) return null;

    // Property order is part of the v1 hash contract; brand arrays are sorted above.
    const canonicalTuple = JSON.stringify({
      schema: "flow-playback-browser-tuple-v1",
      user_agent: navigatorValue.userAgent,
      platform: userAgentData.platform,
      platform_version: highEntropy.platformVersion,
      architecture: highEntropy.architecture,
      bitness: highEntropy.bitness,
      brands,
      full_version_list: fullVersionList,
      mobile: userAgentData.mobile,
      wow64: highEntropy.wow64,
      audio_context_sample_rate: contextSampleRate,
      media_stream_track_processor: true,
      audio_data: true,
      audio_worklet_node: true,
    });
    const digest = await cryptography.subtle.digest("SHA-256", new TextEncoder().encode(canonicalTuple));
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  } catch {
    return null;
  }
}

function randomEvidenceId(cryptography: RuntimeWithPlayback["crypto"]): string | null {
  if (typeof cryptography?.getRandomValues !== "function") return null;
  try {
    const bytes = new Uint8Array(16);
    cryptography.getRandomValues(bytes);
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  } catch {
    return null;
  }
}

export class PlaybackRenderer {
  private readonly context: AudioContext;
  private readonly options: PlaybackRendererOptions;
  private readonly runtime: RuntimeWithPlayback;
  private node: AudioWorkletNode | null = null;
  private reader: ReadableStreamDefaultReader<AudioDataLike> | null = null;
  private track: MediaStreamTrack | null = null;
  private processorConstructor: TrackProcessorConstructor | null = null;
  private readerGeneration = 0;
  private prepared = false;
  private attached = false;
  private disabled = false;
  private closed = false;
  private outputMuted = false;
  private channelOpen = true;
  private inFlightPcmMicroseconds = 0;
  private readonly pcmChannels: Float32Array[] = [];
  private readonly pcmTransferBuffers: ArrayBuffer[] = [];
  private readonly pendingMarkers: PlaybackMarker[] = [];
  private readonly pendingMarkerKeys = new Set<string>();
  private readonly pendingReceipts: PlaybackReceiptMessage[] = [];

  constructor(context: AudioContext, options: PlaybackRendererOptions) {
    this.context = context;
    this.options = options;
    this.runtime = options.runtime ?? globalThis as RuntimeWithPlayback;
  }

  get available(): boolean {
    return this.prepared && this.attached && !this.disabled && !this.closed;
  }

  async prepare(): Promise<boolean> {
    if (this.closed || this.disabled) return false;
    if (this.prepared) return true;
    const runtime = this.runtime;
    const NodeConstructor = runtime.AudioWorkletNode;
    const ProcessorConstructor = runtime.MediaStreamTrackProcessor;
    const cryptography = runtime.crypto;
    if (
      this.context.state !== "running"
      || !this.context.audioWorklet
      || typeof NodeConstructor !== "function"
      || typeof ProcessorConstructor !== "function"
      || typeof runtime.AudioData !== "function"
      || typeof cryptography?.subtle?.digest !== "function"
      || typeof cryptography?.getRandomValues !== "function"
    ) {
      this.disable("qualified AudioWorklet playback APIs are unavailable or suspended");
      return false;
    }

    this.processorConstructor = ProcessorConstructor;
    try {
      await this.context.audioWorklet.addModule(WORKLET_URL);
      if (this.closed || this.disabled) return false;
      this.node = new NodeConstructor(this.context, WORKLET_NAME, {
        numberOfInputs: 0,
        numberOfOutputs: 1,
        outputChannelCount: [2],
      });
      this.node.port.onmessage = (event: MessageEvent<unknown>) => this.onWorkletMessage(event.data);
      this.node.connect(this.context.destination);
      this.node.port.postMessage({
        type: "scope",
        session_id: this.options.authorization.session_id,
        transport_epoch: this.options.authorization.transport_epoch,
      });
      this.context.addEventListener("statechange", this.onContextStateChange);
      this.prepared = true;
      return true;
    } catch {
      this.disable("AudioWorklet playback setup failed");
      return false;
    }
  }

  attach(track: MediaStreamTrack): boolean {
    if (
      !this.prepared
      || this.disabled
      || this.closed
      || this.context.state !== "running"
      || track.kind !== "audio"
      || track.readyState !== "live"
    ) return false;
    const ProcessorConstructor = this.processorConstructor;
    if (!ProcessorConstructor) {
      this.disable("decoded remote audio cannot be read with timestamps");
      return false;
    }

    this.detachTrack();
    try {
      const processor = new ProcessorConstructor({ track });
      const reader = processor.readable.getReader();
      this.track = track;
      this.reader = reader;
      this.attached = true;
      track.addEventListener("mute", this.onTrackMute);
      track.addEventListener("unmute", this.onTrackUnmute);
      const generation = ++this.readerGeneration;
      this.flushPendingMarkers();
      void this.pump(reader, generation);
      return true;
    } catch {
      this.disable("decoded remote audio stream cannot be attached");
      return false;
    }
  }

  handleControlMessage(raw: unknown): boolean {
    const control = parsePlaybackControl(raw);
    if (!control) return false;
    if (control.kind === "invalid-marker" || control.kind === "invalid-cancel") return true;
    if (this.disabled || this.closed) return true;

    if (control.kind === "marker") {
      const marker = control.marker;
      if (!samePlaybackScope(this.options.authorization, marker.binding)) return true;
      const key = markerKey(marker);
      if (this.pendingMarkerKeys.has(key)) return true;
      if (this.pendingMarkerKeys.size >= MAX_SEEN_MARKERS || this.pendingMarkers.length >= MAX_PENDING_MARKERS) {
        this.disable("playback marker queue limit exceeded");
        return true;
      }
      this.pendingMarkerKeys.add(key);
      if (this.available) {
        try {
          this.node?.port.postMessage({ type: "marker", marker });
        } catch {
          this.disable("playback marker could not reach the AudioWorklet");
        }
      } else {
        this.pendingMarkers.push(marker);
      }
      return true;
    }

    const cancellation = control.cancellation;
    if (
      cancellation.session_id !== this.options.authorization.session_id
      || cancellation.transport_epoch !== this.options.authorization.transport_epoch
    ) return true;
    this.lastCancellation = cancellation;
    if (!this.available) {
      this.interrupt(cancellation);
      return true;
    }
    try {
      this.node?.port.postMessage({ type: "cancel", cancellation });
    } catch {
      // Stop the renderer even when cancellation cannot be delivered to the worklet.
    }
    this.interrupt(cancellation);
    return true;
  }

  retryPendingReceipts(): void {
    if (!this.canSendReceipt()) return;
    while (this.pendingReceipts.length > 0) {
      const receipt = this.pendingReceipts[0];
      if (!this.sendReceipt(receipt)) return;
      this.pendingReceipts.shift();
    }
  }

  setOutputMuted(muted: boolean): void {
    this.outputMuted = muted;
    if (muted) this.pendingReceipts.length = 0;
  }

  setChannelOpen(open: boolean): void {
    this.channelOpen = open;
    if (!open) this.pendingReceipts.length = 0;
    else this.retryPendingReceipts();
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.cleanup();
  }

  private async pump(reader: ReadableStreamDefaultReader<AudioDataLike>, generation: number): Promise<void> {
    while (!this.closed && !this.disabled && generation === this.readerGeneration) {
      let result: ReadableStreamReadResult<AudioDataLike>;
      try {
        result = await reader.read();
      } catch {
        if (generation === this.readerGeneration) this.disable("decoded remote audio stream failed");
        return;
      }
      if (result.done) return;
      const audioData = result.value;
      try {
        if (generation === this.readerGeneration && this.reader === reader) this.postAudioData(audioData);
      } catch {
        this.disable("decoded remote audio format or timestamps are unsupported");
      } finally {
        audioData.close();
      }
    }
  }

  private postAudioData(audioData: AudioDataLike): void {
    if (!this.node || this.context.state !== "running" || !this.track || !this.track.enabled || this.track.muted) {
      return;
    }
    if (
      !isSafeInteger(audioData.timestamp)
      || !isSafeInteger(audioData.sampleRate, MIN_PLAYBACK_SAMPLE_RATE)
      || audioData.sampleRate > MAX_PLAYBACK_SAMPLE_RATE
      || !isSafeInteger(audioData.numberOfFrames, 1)
      || audioData.numberOfFrames > MAX_AUDIO_CHUNK_FRAMES
      || !isSafeInteger(audioData.numberOfChannels, 1)
      || audioData.numberOfChannels > 2
    ) throw new Error("Invalid decoded audio dimensions or timestamp");
    const chunkDurationUs = (audioData.numberOfFrames * 1_000_000) / audioData.sampleRate;
    if (
      !Number.isFinite(chunkDurationUs)
      || this.inFlightPcmMicroseconds + chunkDurationUs > MAX_PCM_BUFFER_MICROSECONDS
    ) throw new Error("Decoded audio exceeded the bounded AudioWorklet queue");

    const channelCount = audioData.numberOfChannels;

    const channels = this.pcmChannels;
    const transfer = this.pcmTransferBuffers;
    channels.length = 0;
    transfer.length = 0;
    for (let planeIndex = 0; planeIndex < channelCount; planeIndex += 1) {
      const plane = new Float32Array(audioData.numberOfFrames);
      audioData.copyTo(plane, { planeIndex, format: "f32-planar" });
      channels.push(plane);
      transfer.push(plane.buffer as ArrayBuffer);
    }
    this.node.port.postMessage({
      type: "pcm",
      timestampUs: audioData.timestamp,
      sampleRate: audioData.sampleRate,
      frames: audioData.numberOfFrames,
      channels,
    }, transfer);
    this.inFlightPcmMicroseconds += chunkDurationUs;
  }

  private onWorkletMessage = (event: MessageEvent<unknown>): void => {
    const raw = event.data;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return;
    const message = raw as Record<string, unknown>;
    if (message.type === "unsupported") {
      this.disable(typeof message.reason === "string" ? message.reason : "AudioWorklet playback became unsupported");
      return;
    }
    if (message.type === "interrupted") {
      const cancellation = this.lastCancellation;
      if (cancellation) this.interrupt(cancellation);
      else this.disable("AudioWorklet could not safely drain interrupted playback");
      return;
    }
    if (message.type === "pcm-consumed") {
      const frames = message.frames;
      const sampleRate = message.sample_rate;
      if (
        !isSafeInteger(frames, 1)
        || !isSafeInteger(sampleRate, MIN_PLAYBACK_SAMPLE_RATE)
        || sampleRate > MAX_PLAYBACK_SAMPLE_RATE
      ) {
        this.disable("AudioWorklet returned an invalid PCM queue receipt");
        return;
      }
      const consumedUs = (frames * 1_000_000) / sampleRate;
      if (consumedUs > this.inFlightPcmMicroseconds + 0.001) {
        this.disable("AudioWorklet returned an unexpected PCM queue receipt");
        return;
      }
      this.inFlightPcmMicroseconds = Math.max(0, this.inFlightPcmMicroseconds - consumedUs);
      return;
    }
    const observation = parseRenderedObservation(raw);
    if (!observation) return;
    if (!samePlaybackScope(this.options.authorization, observation.binding)) {
      this.disable("AudioWorklet emitted evidence for a different playback scope");
      return;
    }
    if (!this.canSendReceipt()) return;
    const evidenceId = randomEvidenceId(this.runtime.crypto);
    if (!evidenceId) {
      this.disable("secure evidence identifiers are unavailable");
      return;
    }
    const receipt = parsePlaybackReceipt({
      type: "flow-playback-receipt",
      version: 1,
      binding: observation.binding,
      media_samples: observation.media_samples,
      rendered_frames: observation.rendered_frames,
      evidence_id: evidenceId,
    });
    if (!receipt) return;
    if (this.sendReceipt(receipt)) return;
    if (this.pendingReceipts.length < MAX_PENDING_RECEIPTS) this.pendingReceipts.push(receipt);
  };

  private lastCancellation: PlaybackCancellation | null = null;

  private onContextStateChange = (): void => {
    if (this.context.state !== "running") this.pendingReceipts.length = 0;
  };

  private onTrackMute = (): void => {
    this.pendingReceipts.length = 0;
  };

  private onTrackUnmute = (): void => {
    this.retryPendingReceipts();
  };

  private canSendReceipt(): boolean {
    return this.available
      && this.channelOpen
      && this.context.state === "running"
      && !this.outputMuted
      && Boolean(this.track?.enabled)
      && !Boolean(this.track?.muted);
  }

  private sendReceipt(receipt: PlaybackReceiptMessage): boolean {
    if (!this.canSendReceipt()) return false;
    try {
      return this.options.sendReceipt(receipt);
    } catch {
      return false;
    }
  }

  private flushPendingMarkers(): void {
    if (!this.available || !this.node) return;
    try {
      for (const marker of this.pendingMarkers) {
        this.node.port.postMessage({ type: "marker", marker });
      }
      this.pendingMarkers.length = 0;
    } catch {
      this.disable("queued playback markers could not reach the AudioWorklet");
    }
  }

  private interrupt(cancellation: PlaybackCancellation): void {
    if (this.disabled || this.closed) return;
    this.lastCancellation = cancellation;
    this.disabled = true;
    this.cleanup();
    this.options.onInterrupted?.(cancellation);
  }

  private disable(reason: string): void {
    if (this.disabled || this.closed) return;
    this.disabled = true;
    this.cleanup();
    this.options.onUnsupported?.(reason);
  }

  private cleanup(): void {
    this.readerGeneration += 1;
    this.detachTrack();
    this.context.removeEventListener("statechange", this.onContextStateChange);
    this.node?.port.close();
    this.node?.disconnect();
    this.node = null;
    this.pendingMarkers.length = 0;
    this.pendingMarkerKeys.clear();
    this.pendingReceipts.length = 0;
    this.inFlightPcmMicroseconds = 0;
    this.prepared = false;
    this.processorConstructor = null;
  }

  private detachTrack(): void {
    const track = this.track;
    const reader = this.reader;
    this.track = null;
    this.reader = null;
    this.attached = false;
    track?.removeEventListener("mute", this.onTrackMute);
    track?.removeEventListener("unmute", this.onTrackUnmute);
    if (reader) {
      void reader.cancel().catch(() => {});
      try {
        reader.releaseLock();
      } catch {
        // A pending read releases its lock when cancellation settles.
      }
    }
  }
}

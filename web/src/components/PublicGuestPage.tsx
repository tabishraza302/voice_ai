"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  Mic,
  MicOff,
  PhoneOff,
  RefreshCw,
  ShieldCheck,
  Volume2,
} from "lucide-react";
import { PublicGuestApiError, PublicGuestClient } from "@/lib/public-guest-client";
import type {
  GuestAdmission,
  GuestReconnectToken,
  GuestSessionStatus,
  PublicDeploymentBootstrap,
} from "@/lib/public-guest-client";
import type { PlaybackEnableGrant } from "@/lib/playback-protocol";
import { VoiceSession } from "@/lib/voice-session";
import {
  computePlaybackBrowserTupleHash,
  PlaybackRenderer,
} from "@/lib/playback-renderer";
import {
  parsePlaybackControl,
  parsePlaybackEnableGrant,
} from "@/lib/playback-protocol";

type PageState =
  | "loading"
  | "ready"
  | "admitting"
  | "requesting_media"
  | "connecting"
  | "connected"
  | "disconnected"
  | "ending"
  | "ended"
  | "unavailable";

type SavedGuestSession = { sessionId: string; csrfToken: string };
type SavedGuestAdmissionAttempt = {
  publicId: string;
  idempotencyKey: string;
  payload: {
    disclosure_version: string;
    disclosure_acknowledged: true;
  };
};

const TERMINAL_STATES: Record<string, true> = {
  CLOSED: true,
  COMPLETED: true,
  FAILED: true,
  EXPIRED: true,
  ENDED: true,
  TERMINATED: true,
  CANCELLED: true,
  STOPPED: true,
};
const RECONNECTABLE_STATES: Record<string, true> = {
  REGISTERED: true,
  RECONNECTING: true,
};

function storageKey(publicId: string): string {
  return `public-guest-session:${publicId}`;
}

function admissionStorageKey(publicId: string): string {
  return `public-guest-admission:${publicId}`;
}

function readSavedSession(publicId: string): SavedGuestSession | null {
  try {
    const raw = window.sessionStorage.getItem(storageKey(publicId));
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (
      !value ||
      typeof value !== "object" ||
      !("sessionId" in value) ||
      typeof value.sessionId !== "string" ||
      !("csrfToken" in value) ||
      typeof value.csrfToken !== "string"
    ) {
      window.sessionStorage.removeItem(storageKey(publicId));
      return null;
    }
    return { sessionId: value.sessionId, csrfToken: value.csrfToken };
  } catch {
    return null;
  }
}

function saveSession(publicId: string, sessionId: string, csrfToken: string): boolean {
  try {
    window.sessionStorage.setItem(
      storageKey(publicId),
      JSON.stringify({ sessionId, csrfToken }),
    );
    return true;
  } catch {
    return false;
  }
}

function clearSavedSession(publicId: string): void {
  try {
    window.sessionStorage.removeItem(storageKey(publicId));
  } catch {
    // The server-side capability still expires and is revoked independently.
  }
}

function readSavedAdmissionAttempt(
  publicId: string,
  disclosureVersion: string,
): SavedGuestAdmissionAttempt | null {
  try {
    const raw = window.sessionStorage.getItem(admissionStorageKey(publicId));
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (
      !value ||
      typeof value !== "object" ||
      !("publicId" in value) ||
      typeof value.publicId !== "string" ||
      !("idempotencyKey" in value) ||
      typeof value.idempotencyKey !== "string" ||
      value.idempotencyKey.length === 0 ||
      !("payload" in value) ||
      !value.payload ||
      typeof value.payload !== "object" ||
      !("disclosure_version" in value.payload) ||
      typeof value.payload.disclosure_version !== "string" ||
      !("disclosure_acknowledged" in value.payload) ||
      value.payload.disclosure_acknowledged !== true
    ) {
      clearSavedAdmissionAttempt(publicId);
      return null;
    }
    if (
      value.publicId !== publicId ||
      value.payload.disclosure_version !== disclosureVersion
    ) {
      clearSavedAdmissionAttempt(publicId);
      return null;
    }
    return {
      publicId: value.publicId,
      idempotencyKey: value.idempotencyKey,
      payload: {
        disclosure_version: value.payload.disclosure_version,
        disclosure_acknowledged: true,
      },
    };
  } catch {
    clearSavedAdmissionAttempt(publicId);
    return null;
  }
}

function saveAdmissionAttempt(publicId: string, attempt: SavedGuestAdmissionAttempt): boolean {
  try {
    window.sessionStorage.setItem(admissionStorageKey(publicId), JSON.stringify(attempt));
    return true;
  } catch {
    return false;
  }
}

function clearSavedAdmissionAttempt(publicId: string): void {
  try {
    window.sessionStorage.removeItem(admissionStorageKey(publicId));
  } catch {
    // The page can still use the result it received.
  }
}

function messageFor(error: unknown): string {
  return error instanceof Error ? error.message : "The guest session request failed.";
}

function isTerminal(status: GuestSessionStatus | { state: string }): boolean {
  return TERMINAL_STATES[status.state] === true;
}

function openPlaybackAudioContext(): AudioContext | null {
  const webkitWindow = window as Window & { webkitAudioContext?: typeof AudioContext };
  const AudioContextConstructor = window.AudioContext || webkitWindow.webkitAudioContext;
  if (!AudioContextConstructor) return null;
  try {
    const context = new AudioContextConstructor();
    if (context.state !== "running") void context.resume().catch(() => {});
    return context;
  } catch {
    return null;
  }
}

function closePlaybackAudioContext(context: AudioContext | null): void {
  if (context && context.state !== "closed") void context.close().catch(() => {});
}

export function PublicGuestPage({ publicId }: { publicId: string }) {
  const client = useMemo(() => new PublicGuestClient(), []);
  const [bootstrap, setBootstrap] = useState<PublicDeploymentBootstrap | null>(null);
  const [pageState, setPageState] = useState<PageState>("loading");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [guestCsrf, setGuestCsrf] = useState<string | null>(null);
  const [sessionState, setSessionState] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [muted, setMuted] = useState(false);
  const [audioBlocked, setAudioBlocked] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const pageStateRef = useRef<PageState>("loading");
  const admissionAttemptRef = useRef<SavedGuestAdmissionAttempt | null>(null);
  const voiceSessionRef = useRef<VoiceSession | null>(null);
  const remoteAudioRef = useRef<HTMLAudioElement | null>(null);
  const playbackRendererRef = useRef<PlaybackRenderer | null>(null);
  const remoteTrackRef = useRef<MediaStreamTrack | null>(null);
  const remoteStreamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const connectedRef = useRef(false);

  const showState = useCallback((next: PageState) => {
    pageStateRef.current = next;
    setPageState(next);
  }, []);

  const disposeVoiceSession = useCallback((preserveAudioContext: AudioContext | null = null) => {
    const renderer = playbackRendererRef.current;
    playbackRendererRef.current = null;
    renderer?.close();
    const voiceSession = voiceSessionRef.current;
    voiceSessionRef.current = null;
    voiceSession?.close();
    connectedRef.current = false;
    remoteTrackRef.current = null;
    remoteStreamRef.current = null;
    const context = audioContextRef.current;
    audioContextRef.current = preserveAudioContext;
    if (context !== preserveAudioContext) closePlaybackAudioContext(context);
    if (remoteAudioRef.current) {
      remoteAudioRef.current.pause();
      remoteAudioRef.current.srcObject = null;
    }
  }, []);

  const playRemoteAudioFallback = useCallback((track: MediaStreamTrack, stream?: MediaStream) => {
    const audio = remoteAudioRef.current;
    if (!audio || track.readyState !== "live") return;
    audio.srcObject = stream ?? remoteStreamRef.current ?? new MediaStream([track]);
    void audio.play().then(
      () => setAudioBlocked(false),
      () => setAudioBlocked(true),
    );
  }, []);

  const openAudioContextFromGesture = useCallback(() => {
    const current = audioContextRef.current;
    if (current && current.state !== "closed") {
      if (current.state !== "running") void current.resume().catch(() => {});
      return current;
    }
    const context = openPlaybackAudioContext();
    audioContextRef.current = context;
    return context;
  }, []);

  useEffect(() => {
    let cancelled = false;
    admissionAttemptRef.current = null;
    showState("loading");
    setBootstrap(null);
    setError(null);
    setAcknowledged(false);

    const load = async () => {
      const saved = readSavedSession(publicId);
      if (saved) clearSavedAdmissionAttempt(publicId);
      let deployment: PublicDeploymentBootstrap | null = null;
      let bootstrapError: string | null = null;

      try {
        deployment = await client.bootstrap(publicId);
        if (cancelled) return;
        setBootstrap(deployment);
        admissionAttemptRef.current = saved
          ? null
          : readSavedAdmissionAttempt(publicId, deployment.disclosure_version);
      } catch (loadError) {
        if (cancelled) return;
        bootstrapError = messageFor(loadError);
        setBootstrap(null);
        setError(bootstrapError);
      }

      if (!saved) {
        setSessionId(null);
        setGuestCsrf(null);
        setSessionState(null);
        showState(deployment ? "ready" : "unavailable");
        return;
      }

      try {
        const current = await client.status(saved.sessionId);
        if (cancelled) return;
        setError(null);
        setSessionId(saved.sessionId);
        setGuestCsrf(saved.csrfToken);
        setSessionState(current.state);
        if (isTerminal(current)) {
          clearSavedSession(publicId);
          setSessionId(null);
          setGuestCsrf(null);
          showState("ended");
        } else {
          showState(current.state === "ENDING" ? "ending" : "disconnected");
        }
      } catch (statusError) {
        if (cancelled) return;
        if (
          statusError instanceof PublicGuestApiError &&
          [401, 404].includes(statusError.status)
        ) {
          clearSavedSession(publicId);
          setSessionId(null);
          setGuestCsrf(null);
          setSessionState(null);
          setError(
            bootstrapError ? "This guest session is no longer available." : null,
          );
          showState(deployment ? "ready" : "unavailable");
          return;
        }
        setSessionId(saved.sessionId);
        setGuestCsrf(saved.csrfToken);
        setError(`Could not check the saved guest session. ${messageFor(statusError)}`);
        showState("disconnected");
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [client, loadAttempt, publicId, showState]);

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    let requestPending = false;

    const refresh = async () => {
      if (requestPending) return;
      requestPending = true;
      try {
        const current = await client.status(sessionId);
        if (cancelled) return;
        setSessionState(current.state);
        if (isTerminal(current)) {
          setError(null);
          disposeVoiceSession();
          clearSavedSession(publicId);
          setSessionId(null);
          setGuestCsrf(null);
          showState("ended");
        } else if (current.state === "ENDING") {
          setError(null);
          disposeVoiceSession();
          showState("ending");
        }
      } catch (statusError) {
        if (
          !cancelled &&
          statusError instanceof PublicGuestApiError &&
          [401, 404].includes(statusError.status)
        ) {
          disposeVoiceSession();
          clearSavedSession(publicId);
          setSessionId(null);
          setGuestCsrf(null);
          setSessionState(null);
          setError("This guest session is no longer available.");
          showState("ended");
        }
      } finally {
        requestPending = false;
      }
    };

    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [client, disposeVoiceSession, publicId, sessionId, showState]);

  useEffect(() => () => disposeVoiceSession(), [disposeVoiceSession]);

  const connectTransport = useCallback(async (
    session: Pick<GuestAdmission | GuestReconnectToken, "session_id" | "signaling_url" | "join_token">,
    playbackContext: AudioContext | null,
  ) => {
    disposeVoiceSession(playbackContext);
    if (playbackContext) audioContextRef.current = playbackContext;
    connectedRef.current = false;
    setAudioBlocked(false);
    showState("connecting");

    let activeChannel: RTCDataChannel | null = null;
    let authorizationAttempt = 0;
    let grantPending = false;
    let pendingGrant: PlaybackEnableGrant | null = null;
    let pendingControls: unknown[] = [];
    let capabilitiesSentChannel: RTCDataChannel | null = null;
    let voiceSession!: VoiceSession;
    const isCurrentSession = () => voiceSessionRef.current === voiceSession;
    let browserTupleHashPromise: Promise<string | null> | null = null;
    const getBrowserTupleHash = () => {
      if (!playbackContext) return Promise.resolve(null);
      browserTupleHashPromise ??= computePlaybackBrowserTupleHash(playbackContext.sampleRate);
      return browserTupleHashPromise;
    };
    const announceCapabilities = async (channel: RTCDataChannel) => {
      const context = playbackContext;
      if (!context) return;
      if (context.state !== "running") {
        try {
          await context.resume();
        } catch {
          return;
        }
      }
      if (context.state !== "running") return;
      const browserTupleHash = await getBrowserTupleHash();
      if (
        !browserTupleHash
        || context.state !== "running"
        || !isCurrentSession()
        || channel !== activeChannel
        || !channel.ordered
        || channel.readyState !== "open"
        || capabilitiesSentChannel === channel
      ) return;

      if (voiceSession.sendPlaybackCapabilities({
        type: "flow-playback-capabilities",
        version: 1,
        session_id: session.session_id,
        browser_tuple_hash: browserTupleHash,
      })) capabilitiesSentChannel = channel;
    };
    const closeInterruptedTransport = () => {
      if (!isCurrentSession()) return;
      disposeVoiceSession();
      setError("Playback was interrupted. The browser stopped this voice connection.");
      if (!["ending", "ended"].includes(pageStateRef.current)) showState("disconnected");
    };
    const handleControl = (event: MessageEvent, channel: RTCDataChannel) => {
      if (!isCurrentSession() || channel !== activeChannel || !channel.ordered) return;
      const grant = parsePlaybackEnableGrant(event.data);
      if (grant) {
        if (
          grantPending
          || grant.session_id !== session.session_id
          || capabilitiesSentChannel !== channel
        ) return;
        pendingGrant = grant;
        grantPending = true;
        const attempt = ++authorizationAttempt;
        void enableRenderer(grant, channel, attempt);
        return;
      }

      const control = parsePlaybackControl(event.data);
      if (!control || control.kind === "invalid-marker" || control.kind === "invalid-cancel") return;
      if (
        control.kind === "cancel"
        && control.cancellation.session_id === session.session_id
        && !playbackRendererRef.current
      ) {
        if (pendingGrant && control.cancellation.transport_epoch !== pendingGrant.transport_epoch) return;
        closeInterruptedTransport();
        return;
      }
      if (!grantPending) return;
      const renderer = playbackRendererRef.current;
      if (renderer) {
        renderer.handleControlMessage(event.data);
        return;
      }
      if (pendingControls.length >= 128) {
        authorizationAttempt += 1;
        grantPending = false;
        pendingControls = [];
        pendingGrant = null;
        return;
      }
      pendingControls.push(event.data);
    };

    const enableRenderer = async (
      grant: PlaybackEnableGrant,
      channel: RTCDataChannel,
      attempt: number,
    ) => {
      const context = playbackContext;
      if (!context || context.state !== "running") {
        grantPending = false;
        pendingGrant = null;
        pendingControls = [];
        return;
      }
      const browserTupleHash = await getBrowserTupleHash();
      if (
        !browserTupleHash
        || browserTupleHash !== grant.browser_tuple_hash
        || grant.session_id !== session.session_id
        || !grant.interruption_stop_drain_proven
        || attempt !== authorizationAttempt
        || !isCurrentSession()
        || channel !== activeChannel
        || !channel.ordered
        || channel.readyState !== "open"
      ) {
        if (attempt === authorizationAttempt) {
          grantPending = false;
          pendingGrant = null;
          pendingControls = [];
        }
        return;
      }

      let renderer: PlaybackRenderer;
      renderer = new PlaybackRenderer(context, {
        authorization: grant,
        sendReceipt: (receipt) => isCurrentSession()
          && channel === activeChannel
          && channel.readyState === "open"
          && voiceSession.sendPlaybackReceipt(receipt),
        onUnsupported: () => {
          if (playbackRendererRef.current !== renderer) return;
          playbackRendererRef.current = null;
          grantPending = false;
          pendingGrant = null;
          pendingControls = [];
          authorizationAttempt += 1;
          if (audioContextRef.current === context) audioContextRef.current = null;
          closePlaybackAudioContext(context);
          const track = remoteTrackRef.current;
          if (track) playRemoteAudioFallback(track, remoteStreamRef.current ?? undefined);
        },
        onInterrupted: () => closeInterruptedTransport(),
      });
      renderer.setChannelOpen(true);
      playbackRendererRef.current = renderer;
      const prepared = await renderer.prepare();
      if (
        !prepared
        || attempt !== authorizationAttempt
        || !isCurrentSession()
        || channel !== activeChannel
        || playbackRendererRef.current !== renderer
      ) {
        renderer.close();
        return;
      }
      const controls = pendingControls;
      pendingControls = [];
      for (const control of controls) {
        renderer.handleControlMessage(control);
        if (playbackRendererRef.current !== renderer) return;
      }
      const track = remoteTrackRef.current;
      if (track) {
        remoteAudioRef.current?.pause();
        if (remoteAudioRef.current) remoteAudioRef.current.srcObject = null;
        if (!renderer.attach(track)) {
          playRemoteAudioFallback(track, remoteStreamRef.current ?? undefined);
        } else {
          setAudioBlocked(false);
        }
      }
    };

    voiceSession = new VoiceSession({
      getUserMedia: (constraints) => navigator.mediaDevices.getUserMedia(constraints),
      createPeerConnection: (configuration) => new RTCPeerConnection(configuration),
      fetcher: fetch,
      onDataChannelMessage: handleControl,
      onDataChannelOpen: (channel) => {
        if (isCurrentSession()) {
          activeChannel = channel;
          playbackRendererRef.current?.setChannelOpen(true);
          void announceCapabilities(channel);
        }
      },
      onDataChannelClose: (channel) => {
        if (!isCurrentSession() || channel !== activeChannel) return;
        authorizationAttempt += 1;
        grantPending = false;
        pendingGrant = null;
        pendingControls = [];
        playbackRendererRef.current?.setChannelOpen(false);
        closeInterruptedTransport();
      },
      onStatus: (status) => {
        if (pageStateRef.current === "ending" || pageStateRef.current === "ended") return;
        showState(status === "requesting_media" ? "requesting_media" : "connecting");
      },
      onConnectionState: (connectionState) => {
        if (connectionState === "connected") {
          connectedRef.current = true;
          playbackRendererRef.current?.setChannelOpen(Boolean(activeChannel?.ordered && activeChannel.readyState === "open"));
          showState("connected");
        } else if (["disconnected", "failed", "closed"].includes(connectionState)) {
          authorizationAttempt += 1;
          grantPending = false;
          pendingGrant = null;
          pendingControls = [];
          connectedRef.current = false;
          playbackRendererRef.current?.setChannelOpen(false);
          closeInterruptedTransport();
        }
      },
      onRemoteTrack: (event) => {
        if (event.track.kind !== "audio") return;
        const stream = event.streams[0] || new MediaStream([event.track]);
        remoteTrackRef.current = event.track;
        remoteStreamRef.current = stream;
        const renderer = playbackRendererRef.current;
        if (renderer?.available) {
          remoteAudioRef.current?.pause();
          if (remoteAudioRef.current) remoteAudioRef.current.srcObject = null;
          if (renderer.attach(event.track)) {
            setAudioBlocked(false);
            return;
          }
        }
        playRemoteAudioFallback(event.track, stream);
      },
    });
    voiceSession.setMuted(muted);
    voiceSessionRef.current = voiceSession;

    try {
      await voiceSession.connect({
        offerUrl: session.signaling_url,
        requestData: {
          session_id: session.session_id,
          join_token: session.join_token,
        },
      });
    } catch (connectionError) {
      if (voiceSessionRef.current === voiceSession) disposeVoiceSession();
      else voiceSession.close();
      connectedRef.current = false;
      throw connectionError;
    }
  }, [disposeVoiceSession, muted, playRemoteAudioFallback, showState]);

  const startSession = async () => {
    if (!bootstrap || !acknowledged || pageStateRef.current === "admitting") return;
    const playbackContext = openAudioContextFromGesture();
    setError(null);
    showState("admitting");
    let admitted = false;
    try {
      const payload = {
        disclosure_version: bootstrap.disclosure_version,
        disclosure_acknowledged: true as const,
      };
      const currentAttempt = admissionAttemptRef.current;
      const attempt = currentAttempt &&
        currentAttempt.publicId === publicId &&
        currentAttempt.payload.disclosure_version === payload.disclosure_version &&
        currentAttempt.payload.disclosure_acknowledged === payload.disclosure_acknowledged
        ? currentAttempt
        : {
          publicId,
          idempotencyKey: crypto.randomUUID(),
          payload,
        };
      if (!saveAdmissionAttempt(publicId, attempt)) {
        throw new Error("Browser session storage is unavailable; cannot safely start session.");
      }
      admissionAttemptRef.current = attempt;
      const session = await client.admit(
        publicId,
        bootstrap,
        attempt.idempotencyKey,
      );
      admitted = true;
      setSessionId(session.session_id);
      setGuestCsrf(session.csrf_token);
      setSessionState(session.state);
      if (saveSession(publicId, session.session_id, session.csrf_token)) {
        clearSavedAdmissionAttempt(publicId);
        admissionAttemptRef.current = null;
      }
      await connectTransport(session, playbackContext);
    } catch (startError) {
      disposeVoiceSession();
      setError(messageFor(startError));
      if (
        !admitted &&
        startError instanceof PublicGuestApiError &&
        startError.status >= 400 &&
        startError.status < 500 &&
        startError.status !== 429
      ) {
        clearSavedAdmissionAttempt(publicId);
        admissionAttemptRef.current = null;
      }
      if (!["ending", "ended"].includes(pageStateRef.current)) {
        showState(admitted ? "disconnected" : "ready");
      }
    }
  };

  const reconnectSession = async () => {
    if (!sessionId || !guestCsrf || !sessionState || connectedRef.current) return;
    if (!RECONNECTABLE_STATES[sessionState]) return;
    const playbackContext = openAudioContextFromGesture();
    setError(null);
    showState("connecting");
    try {
      const join = await client.reconnect(sessionId, guestCsrf, crypto.randomUUID());
      setSessionState(join.state);
      await connectTransport(join, playbackContext);
    } catch (reconnectError) {
      disposeVoiceSession();
      setError(messageFor(reconnectError));
      if (!["ending", "ended"].includes(pageStateRef.current)) {
        showState("disconnected");
      }
    }
  };

  const endSession = async () => {
    if (!sessionId || !guestCsrf || pageStateRef.current === "ending") return;
    const endingSessionId = sessionId;
    const csrfToken = guestCsrf;
    setError(null);
    showState("ending");
    disposeVoiceSession();
    try {
      const ended = await client.end(
        endingSessionId,
        csrfToken,
        crypto.randomUUID(),
      );
      setSessionState(ended.state);
      if (TERMINAL_STATES[ended.state]) {
        clearSavedSession(publicId);
        setSessionId(null);
        setGuestCsrf(null);
        showState("ended");
      }
    } catch (endError) {
      setError(messageFor(endError));
      if (pageStateRef.current !== "ended") showState("disconnected");
    }
  };

  const toggleMute = () => {
    const nextMuted = !muted;
    setMuted(nextMuted);
    voiceSessionRef.current?.setMuted(nextMuted);
  };

  const retryLoad = () => {
    setLoadAttempt((attempt) => attempt + 1);
  };

  const startAnotherSession = () => {
    clearSavedAdmissionAttempt(publicId);
    admissionAttemptRef.current = null;
    setError(null);
    setAcknowledged(false);
    setSessionState(null);
    showState("ready");
  };

  const canReconnect = Boolean(
    sessionId &&
    guestCsrf &&
    sessionState &&
    RECONNECTABLE_STATES[sessionState] &&
    pageState === "disconnected",
  );
  const hasGuestSession = Boolean(sessionId && guestCsrf);
  const startDisabled = !acknowledged || pageState === "admitting" || pageState === "requesting_media" || pageState === "connecting";

  const statusTextByState: Record<PageState, string> = {
    loading: "Loading the public session…",
    ready: "Review the disclosure, then choose whether to start.",
    admitting: "Preparing your guest session…",
    requesting_media: "Waiting for microphone permission…",
    connecting: "Connecting to the voice service…",
    connected: "Connected. Speak naturally; live audio is active.",
    disconnected: sessionState === "ACTIVE"
      ? "The connection was interrupted. Waiting for the media service before reconnecting."
      : "The session is not connected. You can retry once the service is ready.",
    ending: "Ending the session. Waiting for the service to confirm shutdown…",
    ended: "This guest session has ended.",
    unavailable: "This public session link is unavailable.",
  };
  const statusText = statusTextByState[pageState];

  return (
    <main className="mx-auto w-full max-w-3xl space-y-7 pb-10">
      <header className="flex items-center gap-3 border-b border-ink-800 pb-5">
        <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-copper-800 bg-copper-950 text-copper-300">
          <Mic className="h-5 w-5" aria-hidden="true" />
        </div>
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-copper-300">Voice AI</p>
          <p className="mt-1 text-sm text-limestone-400">Guest voice session</p>
        </div>
      </header>

      <section className="surface overflow-hidden">
        <div className="border-b border-ink-800 bg-ink-900/50 p-6 sm:p-8">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-copper-300">Public invitation</p>
          <h1 className="mt-3 text-3xl font-semibold tracking-tight text-limestone-100">
            {bootstrap?.agent_name || "Voice session"}
          </h1>
          {bootstrap?.agent_description && (
            <p className="mt-3 max-w-2xl text-sm leading-6 text-limestone-300">
              {bootstrap.agent_description}
            </p>
          )}
        </div>

        <div className="space-y-6 p-6 sm:p-8">
          {pageState === "loading" && (
            <p role="status" className="flex items-center gap-3 text-sm text-limestone-400">
              <RefreshCw className="h-4 w-4 animate-spin text-copper-300" />Loading public session details…
            </p>
          )}

          {pageState === "unavailable" && (
            <div role="alert" className="flex items-start gap-3 rounded-xl border border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
              <div className="space-y-3">
                <p>{error || statusText}</p>
                <button className="button-secondary" onClick={retryLoad}>Try again</button>
              </div>
            </div>
          )}

          {(bootstrap || hasGuestSession || pageState === "ended") &&
            pageState !== "loading" &&
            pageState !== "unavailable" && (
            <>
              {bootstrap && (
                <div className="rounded-xl border border-ink-700 bg-ink-950/60 p-5">
                  <div className="flex items-start gap-3">
                    <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-copper-300" aria-hidden="true" />
                    <div className="min-w-0 space-y-3">
                      <h2 className="font-semibold text-limestone-100">Before you begin</h2>
                      <p className="whitespace-pre-wrap text-sm leading-6 text-limestone-300">
                        {bootstrap.disclosure_text}
                      </p>
                      <p className="text-xs text-limestone-500">Disclosure version {bootstrap.disclosure_version}</p>
                    </div>
                  </div>
                </div>
              )}

              <div role="status" aria-live="polite" className="flex items-start gap-3 rounded-xl border border-ink-700 bg-ink-900/40 p-4 text-sm text-limestone-200">
                {pageState === "admitting" || pageState === "requesting_media" || pageState === "connecting" || pageState === "ending"
                  ? <RefreshCw className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-copper-300" aria-hidden="true" />
                  : <Volume2 className="mt-0.5 h-4 w-4 shrink-0 text-copper-300" aria-hidden="true" />}
                <span>{statusText}</span>
              </div>

              {error && (
                <div role="alert" className="rounded-lg border border-rust-800 bg-rust-950/30 p-3 text-sm text-rust-200">
                  {error}
                </div>
              )}

              {pageState === "ready" && (
                <label className="flex cursor-pointer items-start gap-3 text-sm leading-6 text-limestone-300">
                  <input
                    type="checkbox"
                    checked={acknowledged}
                    onChange={(event) => setAcknowledged(event.currentTarget.checked)}
                    className="mt-1 h-4 w-4 accent-copper-400"
                  />
                  <span>I have read and acknowledge the disclosure above.</span>
                </label>
              )}

              <audio
                ref={remoteAudioRef}
                autoPlay
                playsInline
                className="sr-only"
                aria-label="Voice assistant audio"
              />

              {audioBlocked && (
                <button
                  className="button-secondary"
                  onClick={() => remoteAudioRef.current?.play().catch(() => setAudioBlocked(true))}
                >
                  Play assistant audio
                </button>
              )}

              <div className="flex flex-wrap gap-3">
                {pageState === "ready" && (
                  <button className="button-primary" disabled={startDisabled} onClick={() => void startSession()}>
                    <Mic className="h-4 w-4" aria-hidden="true" />Start voice session
                  </button>
                )}
                {pageState === "ended" && bootstrap && (
                  <button className="button-primary" onClick={startAnotherSession}>
                    <Mic className="h-4 w-4" aria-hidden="true" />Start another session
                  </button>
                )}
                {pageState === "connected" && (
                  <button className="button-secondary" onClick={toggleMute}>
                    {muted ? <Mic className="h-4 w-4" aria-hidden="true" /> : <MicOff className="h-4 w-4" aria-hidden="true" />}
                    {muted ? "Unmute microphone" : "Mute microphone"}
                  </button>
                )}
                {canReconnect && (
                  <button className="button-primary" onClick={() => void reconnectSession()}>
                    <RefreshCw className="h-4 w-4" aria-hidden="true" />Reconnect
                  </button>
                )}
                {hasGuestSession && pageState !== "ended" && (
                  <button className="button-secondary" disabled={pageState === "ending"} onClick={() => void endSession()}>
                    <PhoneOff className="h-4 w-4" aria-hidden="true" />End session
                  </button>
                )}
              </div>

              {pageState === "disconnected" && sessionState && !RECONNECTABLE_STATES[sessionState] && (
                <p className="text-xs leading-5 text-limestone-500">
                  Session state: {sessionState.toLowerCase()}. Reconnect becomes available after the service releases the previous peer.
                </p>
              )}

              <p className="border-t border-ink-800 pt-4 text-xs leading-5 text-limestone-500">
                Your browser needs microphone access and a secure HTTPS connection. The assistant audio will play in this page when the call connects.
              </p>
            </>
          )}
        </div>
      </section>
    </main>
  );
}

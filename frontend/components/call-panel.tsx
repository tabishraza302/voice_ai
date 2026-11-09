"use client";

import { useEffect, useReducer, useRef, useState } from "react";
import {
  createSmallWebRtcCall,
  loadIceServers,
  microphoneErrorMessage,
  parseCallSessionGrant,
  requestMicrophone,
  type CallCaption,
  type CallSessionGrant,
  type CallTransportEvent,
  type SmallWebRtcCall,
} from "../lib/webrtc";
import { Button } from "./ui/button";
import { cn } from "./ui/utils";
export type { CallSessionGrant } from "../lib/webrtc";

type CaptionEntry = CallCaption & { id: number };
const EMPTY_CAPTIONS: CaptionEntry[] = [];
type CallDetails = {
  sessionId: string;
  captions: CaptionEntry[];
  muted: boolean;
  audioBlocked: boolean;
};

type PanelState =
  | { kind: "ready" }
  | { kind: "requesting-microphone" }
  | { kind: "preparing" }
  | ({ kind: "connecting" } & CallDetails)
  | ({ kind: "active"; startedAt: number; speech: "listening" | "speaking" | "reconnecting" } & CallDetails)
  | ({ kind: "ending" } & CallDetails)
  | { kind: "ended"; sessionId: string; reason: "caller" | "remote" | "completed"; captions: CaptionEntry[] }
  | { kind: "error"; message: string }
  | ({ kind: "failed"; message: string } & CallDetails);

type PanelAction =
  | { type: "request" }
  | { type: "preparing" }
  | { type: "created"; sessionId: string }
  | { type: "connected"; startedAt: number }
  | { type: "reconnecting" }
  | { type: "speaking"; value: boolean }
  | { type: "caption"; caption: CaptionEntry }
  | { type: "muted"; value: boolean }
  | { type: "audio-blocked"; value: boolean }
  | { type: "ending" }
  | { type: "ended"; reason: "caller" | "remote" | "completed" }
  | { type: "error"; message: string }
  | { type: "failed"; message: string }
  | { type: "reset" };

export type CallPanelProps = {
  createCall: () => Promise<CallSessionGrant>;
  title: string;
  onEnded?: (sessionId: string) => void;
  className?: string;
  hideHeader?: boolean;
};

const EMPTY_DETAILS: Pick<CallDetails, "captions" | "muted" | "audioBlocked"> = {
  captions: [],
  muted: false,
  audioBlocked: false,
};

function panelReducer(state: PanelState, action: PanelAction): PanelState {
  switch (action.type) {
    case "request":
      return { kind: "requesting-microphone" };
    case "preparing":
      return state.kind === "requesting-microphone" ? { kind: "preparing" } : state;
    case "created":
      return state.kind === "preparing"
        ? { kind: "connecting", sessionId: action.sessionId, ...EMPTY_DETAILS }
        : state;
    case "connected":
      if (state.kind === "connecting") {
        return { ...state, kind: "active", startedAt: action.startedAt, speech: "listening" };
      }
      if (state.kind === "active") return { ...state, speech: "listening" };
      return state;
    case "reconnecting":
      if (state.kind === "active") return { ...state, speech: "reconnecting" };
      return state;
    case "speaking":
      if (state.kind === "active") return { ...state, speech: action.value ? "speaking" : "listening" };
      return state;
    case "caption":
      if (state.kind === "connecting" || state.kind === "active" || state.kind === "ending") {
        let unfinishedIndex = -1;
        for (let index = state.captions.length - 1; index >= 0; index -= 1) {
          const caption = state.captions[index];
          if (caption.role === action.caption.role && !caption.final) {
            unfinishedIndex = index;
            break;
          }
        }
        if (unfinishedIndex >= 0) {
          const updated = [...state.captions];
          updated[unfinishedIndex] = {
            ...updated[unfinishedIndex],
            text: action.caption.text,
            final: action.caption.final,
          };
          return { ...state, captions: updated };
        }
        return { ...state, captions: [...state.captions, action.caption] };
      }
      return state;
    case "muted":
      if (state.kind === "connecting" || state.kind === "active" || state.kind === "ending") {
        return { ...state, muted: action.value };
      }
      return state;
    case "audio-blocked":
      if (state.kind === "connecting" || state.kind === "active" || state.kind === "ending") {
        return { ...state, audioBlocked: action.value };
      }
      return state;
    case "ending":
      if (state.kind === "connecting" || state.kind === "active") return { ...state, kind: "ending" };
      return state;
    case "ended":
      if (state.kind === "connecting" || state.kind === "active" || state.kind === "ending") {
        return {
          kind: "ended",
          sessionId: state.sessionId,
          reason: action.reason,
          captions: state.captions.map((c) => (c.final === false ? { ...c, final: true } : c)),
        };
      }
      return state;
    case "error":
      return { kind: "error", message: action.message };
    case "failed":
      if (state.kind === "connecting" || state.kind === "active" || state.kind === "ending") {
        return {
          ...state,
          kind: "failed",
          message: action.message,
          captions: state.captions.map((c) => (c.final === false ? { ...c, final: true } : c)),
        };
      }
      return state;
    case "reset":
      return { kind: "ready" };
  }
}

function statusForError(error: unknown): string {
  if (typeof error === "object" && error !== null && "status" in error) {
    const status = error.status;
    if (status === 404) return "This agent is not available for calls.";
    if (status === 409) return "This agent cannot take a call right now.";
    if (status === 422) return "This language is not available for this call.";
    if (status === 429) return "This agent is at capacity. Try again in a little while.";
    if (status === 502) return "The voice connection could not be established. Check your connection and try again.";
    if (status === 503) return "Voice calling is not available right now. Please try again later.";
  }
  return "We couldn't start the call. Check your connection and try again.";
}

function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
}

function isBusy(state: PanelState): boolean {
  return (
    state.kind === "requesting-microphone" ||
    state.kind === "preparing" ||
    state.kind === "connecting" ||
    state.kind === "active" ||
    state.kind === "ending"
  );
}

export function CallPanel({ createCall, title, onEnded, className, hideHeader }: CallPanelProps) {
  const [state, dispatch] = useReducer(panelReducer, { kind: "ready" });
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const stateRef = useRef(state);
  const audioRef = useRef<HTMLAudioElement>(null);
  const connectionRef = useRef<SmallWebRtcCall | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const completedSessionRef = useRef<string | null>(null);
  const attemptRef = useRef(0);
  const startInProgressRef = useRef(false);
  const captionIdRef = useRef(0);
  const onEndedRef = useRef(onEnded);
  stateRef.current = state;
  const transcriptRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    onEndedRef.current = onEnded;
  }, [onEnded]);

  const startedAt = state.kind === "active" ? state.startedAt : null;
  useEffect(() => {
    if (startedAt === null) {
      setElapsedSeconds(0);
      return;
    }
    const update = () => setElapsedSeconds(Math.floor((performance.now() - startedAt) / 1000));
    update();
    const timer = window.setInterval(update, 1_000);
    return () => window.clearInterval(timer);
  }, [startedAt]);


  useEffect(
    () => () => {
      attemptRef.current += 1;
      connectionRef.current?.close();
      connectionRef.current = null;
      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    },
    [],
  );

  function finishSession(reason: "caller" | "remote" | "completed" | "failed", message?: string): void {
    const sessionId = sessionIdRef.current;
    if (!sessionId || completedSessionRef.current === sessionId) return;
    completedSessionRef.current = sessionId;
    const connection = connectionRef.current;
    connectionRef.current = null;
    connection?.close();
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    if (reason === "failed") dispatch({ type: "failed", message: message ?? statusForError(null) });
    else dispatch({ type: "ended", reason });
    onEndedRef.current?.(sessionId);
  }

  function receiveTransportEvent(event: CallTransportEvent): void {
    switch (event.type) {
      case "connected":
        dispatch({ type: "connected", startedAt: performance.now() });
        return;
      case "reconnecting":
        dispatch({ type: "reconnecting" });
        return;
      case "speaking":
        dispatch({ type: "speaking", value: event.value });
        return;
      case "caption":
        captionIdRef.current += 1;
        dispatch({ type: "caption", caption: { ...event.caption, id: captionIdRef.current } });
        return;
      case "audio-blocked":
        dispatch({ type: "audio-blocked", value: true });
        return;
      case "audio-ready":
        dispatch({ type: "audio-blocked", value: false });
        return;
      case "completed":
        finishSession("completed");
        return;
      case "ended":
        finishSession(event.reason === "local" ? "caller" : "remote");
        return;
      case "failed":
        finishSession("failed", "The voice connection was interrupted. Check your connection and try again.");
        return;
    }
  }

  async function startCall(): Promise<void> {
    if (startInProgressRef.current || isBusy(stateRef.current)) return;
    startInProgressRef.current = true;
    const attempt = ++attemptRef.current;
    completedSessionRef.current = null;
    sessionIdRef.current = null;
    connectionRef.current?.close();
    connectionRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    dispatch({ type: "reset" });
    dispatch({ type: "request" });

    let localStream: MediaStream | null = null;
    let microphoneStage = true;
    try {
      localStream = await requestMicrophone();
      if (attempt !== attemptRef.current) {
        localStream.getTracks().forEach((track) => track.stop());
        return;
      }
      streamRef.current = localStream;
      microphoneStage = false;
      dispatch({ type: "preparing" });
      const iceServers = await loadIceServers();
      if (attempt !== attemptRef.current) return;
      const grant = parseCallSessionGrant(await createCall());
      if (attempt !== attemptRef.current) return;
      sessionIdRef.current = grant.sessionId;
      dispatch({ type: "created", sessionId: grant.sessionId });

      const audioElement = audioRef.current;
      if (!audioElement) throw new Error("Call audio is not available.");
      const connection = createSmallWebRtcCall({
        iceServers,
        localStream,
        audioElement,
        onEvent: receiveTransportEvent,
      });
      connectionRef.current = connection;
      await connection.connect(grant);
    } catch (error) {
      if (attempt !== attemptRef.current) return;
      if (sessionIdRef.current) {
        finishSession("failed", statusForError(error));
      } else {
        localStream?.getTracks().forEach((track) => track.stop());
        streamRef.current = null;
        dispatch({
          type: "error",
          message: microphoneStage ? microphoneErrorMessage(error) : statusForError(error),
        });
      }
    } finally {
      startInProgressRef.current = false;
    }
  }

  async function endCall(): Promise<void> {
    const current = stateRef.current;
    if (current.kind !== "connecting" && current.kind !== "active") return;
    attemptRef.current += 1;
    dispatch({ type: "ending" });
    await connectionRef.current?.end();
    finishSession("caller");
  }

  function toggleMute(): void {
    const current = stateRef.current;
    if (current.kind !== "active") return;
    const muted = !current.muted;
    connectionRef.current?.setMuted(muted);
    dispatch({ type: "muted", value: muted });
  }


  const active = state.kind === "active";
  const currentCaptions =
    state.kind === "connecting" || state.kind === "active" || state.kind === "ending" || state.kind === "failed"
      ? state.captions
      : state.kind === "ended"
        ? state.captions
        : EMPTY_CAPTIONS;
  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [currentCaptions]);
  const statusLabel =
    state.kind === "requesting-microphone"
      ? "Waiting for microphone permission"
      : state.kind === "preparing"
        ? "Preparing the call"
        : state.kind === "connecting"
          ? "Connecting"
          : state.kind === "active"
            ? state.speech === "speaking"
              ? "Speaking"
              : state.speech === "reconnecting"
                ? "Reconnecting"
                : "Listening"
            : state.kind === "ending"
              ? "Ending call"
              : state.kind === "ended"
                ? state.reason === "completed"
                  ? "Call complete"
                  : "Call ended"
                : state.kind === "failed"
                  ? "Call interrupted"
                  : "Ready when you are";
  const canEnd = state.kind === "connecting" || state.kind === "active";
  const canEnableAudio =
    (state.kind === "connecting" || state.kind === "active") && state.audioBlocked;

  return (
    <section className={cn("w-full rounded-2xl border bg-card p-5 text-card-foreground sm:p-7", className)} aria-label={`Call with ${title}`}>
      <audio ref={audioRef} autoPlay playsInline className="sr-only" aria-label="Call audio" />
      {!hideHeader ? (
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2 className="text-xl font-semibold tracking-tight">{title}</h2>
            <p className="mt-1 text-sm text-muted-foreground">A voice conversation with captions.</p>
          </div>
          {active ? (
            <time className="rounded-xl bg-muted px-3 py-1.5 text-sm tabular-nums text-muted-foreground" aria-label={`Call time ${formatDuration(elapsedSeconds)}`}>
              {formatDuration(elapsedSeconds)}
            </time>
          ) : null}
        </div>
      ) : null}

      <div className={cn("flex items-center justify-between gap-3", !hideHeader && "mt-6")} role="status" aria-live="polite" aria-atomic="true">
        <div className="flex items-center gap-3">
          <span
            className={`h-2.5 w-2.5 rounded-full ${state.kind === "active" && state.speech === "speaking" ? "bg-primary" : isBusy(state) ? "bg-primary/60" : "bg-muted-foreground/40"}`}
            aria-hidden="true"
          />
          <span className="text-sm font-medium text-foreground">{statusLabel}</span>
        </div>
        {hideHeader && active ? (
          <time className="rounded-xl bg-muted px-2.5 py-1 text-xs tabular-nums text-muted-foreground" aria-label={`Call time ${formatDuration(elapsedSeconds)}`}>
            {formatDuration(elapsedSeconds)}
          </time>
        ) : null}
      </div>

      <div ref={transcriptRef} className="mt-5 min-h-48 max-h-80 space-y-3 overflow-y-auto rounded-2xl bg-muted/40 p-4" aria-label="Live captions" aria-live="polite" aria-relevant="additions text">
        {currentCaptions.length > 0 ? (
          currentCaptions.map((caption) => {
            const isUser = caption.role === "user";
            return (
              <div
                key={caption.id}
                className={cn("flex flex-col gap-1 max-w-[85%]", isUser ? "ml-auto items-end" : "mr-auto items-start")}
              >
                <span className="text-[11px] font-semibold text-muted-foreground">
                  {isUser ? "You" : "Agent"}
                </span>
                <div
                  className={cn(
                    "rounded-2xl px-3.5 py-2 text-sm leading-relaxed",
                    isUser
                      ? "bg-primary text-primary-foreground rounded-br-sm"
                      : "bg-background text-foreground border border-border/60 shadow-sm rounded-bl-sm"
                  )}
                >
                  <span>{caption.text}</span>
                  {!caption.final ? (
                    <span
                      className={cn(
                        "inline-block ml-1.5 h-2 w-2 rounded-full animate-pulse align-middle",
                        isUser ? "bg-primary-foreground" : "bg-primary"
                      )}
                      aria-label="streaming speech"
                    />
                  ) : null}
                </div>
              </div>
            );
          })
        ) : (
          <p className="text-sm leading-6 text-muted-foreground">
            {state.kind === "ended"
              ? "No captions were received during this call."
              : state.kind === "failed"
                ? "Your captions remain here while you decide what to do next."
                : "Your conversation will appear here once the call starts."}
          </p>
        )}
      </div>

      {state.kind === "error" || state.kind === "failed" ? (
        <p className="mt-4 text-sm text-destructive" role="alert">{state.message}</p>
      ) : null}
      {state.kind === "ended" ? (
        <p className="mt-4 text-sm text-muted-foreground" role="status">
          {state.reason === "caller"
            ? "You ended this call."
            : state.reason === "completed"
              ? "This call is complete."
              : "Thanks for calling. This conversation has ended."}
        </p>
      ) : null}

      <div className="mt-5 flex flex-wrap gap-3">
        {!isBusy(state) ? (
          <Button onClick={() => void startCall()}>
            {state.kind === "ready" ? "Start call" : "Start another call"}
          </Button>
        ) : null}
        {active ? (
          <Button variant="outline" onClick={toggleMute} aria-pressed={state.muted}>
            {state.muted ? "Unmute microphone" : "Mute microphone"}
          </Button>
        ) : null}
        {canEnd ? (
          <Button variant="outline" className="border-destructive/40 text-destructive hover:bg-destructive/10" onClick={() => void endCall()}>
            {state.kind === "connecting" ? "Cancel connection" : "End call"}
          </Button>
        ) : null}
        {canEnableAudio ? (
          <Button variant="outline" onClick={() => void connectionRef.current?.enableAudio()}>
            Enable call audio
          </Button>
        ) : null}
      </div>
    </section>
  );
}

"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import {
  Mic,
  MicOff,
  PhoneOff,
  Volume2,
  Bot,
  X,
  WifiOff,
  AlertCircle,
  RefreshCw,
  ShieldAlert,
  MessageSquare,
  User,
  Trash2,
  Send,
} from "lucide-react";
import { api } from "@/lib/api-client";
import { MAX_CLIENT_UTTERANCE_CHARS } from "@/lib/constants";
import { VoiceSession } from "@/lib/voice-session";
import { TranscriptLog } from "@/lib/transcript-log";
import { wallClockFromPerformance } from "@/lib/format";
import { SpeechOnsetTracker } from "@/lib/speech-onset";
import {
  appendAssistantTokenLine,
  appendTranscriptLine,
  appendUserPartialLine,
  assistantTokenFromDataChannel,
  classifyUserDeltaAfterFinal,
  dataChannelEventType,
  finishLiveLine,
  replaceTranscriptLine,
  transcriptFromDataChannel,
  userPartialFromDataChannel,
  validateClientUtterance,
} from "@/lib/transcript-protocol";
import type { Agent } from "@/types/agent";

export interface TranscriptItem {
  id: string;
  role: "user" | "assistant";
  text: string;
  time: string;
  live?: boolean;
}

interface VoiceTestModalProps {
  agent: Agent;
  isOpen: boolean;
  onClose: () => void;
  onActivated?: (agent: Agent) => void;
}

type ConnectionStatus =
  | "idle"
  | "activating"
  | "requesting_media"
  | "connecting"
  | "connected"
  | "disconnected"
  | "error";

function clockTime(date: Date): string {
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

const PREFERRED_MUTE_STORAGE_KEY = "voice_ai.microphone_muted";

function readInitialMuteState(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.sessionStorage.getItem(PREFERRED_MUTE_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function persistMuteState(muted: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(PREFERRED_MUTE_STORAGE_KEY, muted ? "true" : "false");
  } catch {
    // Ignore storage quota or disabled storage errors in restricted contexts.
  }
}

export function VoiceTestModal({
  agent,
  isOpen,
  onClose,
  onActivated,
}: VoiceTestModalProps) {
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isMuted, setIsMuted] = useState(false);
  const [duration, setDuration] = useState(0);
  const [textInput, setTextInput] = useState("");
  const [textInputError, setTextInputError] = useState<string | null>(null);
  const [transcripts, setTranscripts] = useState<TranscriptItem[]>([]);
  const [isBotSpeaking, setIsBotSpeaking] = useState(false);
  const [audioBlocked, setAudioBlocked] = useState(false);

  const agentRef = useRef(agent);
  agentRef.current = agent;
  const onActivatedRef = useRef(onActivated);
  onActivatedRef.current = onActivated;
  const isMutedRef = useRef(isMuted);
  isMutedRef.current = isMuted;

  // Read the persisted preference after mount. Reading storage in the state
  // initializer makes the server render (false) and a muted client render
  // (true) disagree, which React reports as a hydration mismatch.
  useEffect(() => {
    setIsMuted(readInitialMuteState());
  }, []);
  const isSessionStartingRef = useRef(false);
  const sessionGenerationRef = useRef(0);
  // One dedupe log per mounted modal; reset when a call is torn down.
  const transcriptLogRef = useRef<TranscriptLog | null>(null);
  // Which representation is filling the current assistant bubble: the live
  // token stream (realtime) or a completed transcript (fallback when tokens
  // are unavailable). A user line starts the next turn.
  const assistantSourceRef = useRef<"none" | "tokens" | "completed">("none");
  const assistantTokenIdRef = useRef(0);
  // The live STT bubble: deltas append to one id until the final transcript.
  const userPartialIdRef = useRef<string | null>(null);
  const userPartialSeqRef = useRef(0);
  const userPartialTextRef = useRef("");
  // After a final transcript, late deltas can still arrive for that turn. Keep
  // the finalized live/final text so the first delta that does not continue it
  // can start the next turn, without waiting for an unreliable start event.
  const finalizedUserLiveRef = useRef("");
  const finalizedUserTextRef = useRef("");
  const userSourceRef = useRef<"none" | "partial" | "final">("none");
  // Timers that clear `isBotSpeaking`; cleared on teardown so none fires late.
  const botSpeakingTimersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());

  const voiceSessionRef = useRef<VoiceSession | null>(null);
  const remoteAudioRef = useRef<HTMLAudioElement | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const animationFrameRef = useRef<number | null>(null);
  const timerRef = useRef<NodeJS.Timeout | null>(null);
  const pingIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  // Auto-scroll transcript container. Token updates arrive continuously, so
  // snap to the newest line instead of restarting a smooth animation per token.
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "auto" });
  }, [transcripts]);

  const getTranscriptLog = useCallback(() => {
    if (!transcriptLogRef.current) transcriptLogRef.current = new TranscriptLog();
    return transcriptLogRef.current;
  }, []);

  // The dialog's clock source for a user turn. The STT text arrives late;
  // the microphone energy rise is when the user actually started speaking.
  const speechOnsetRef = useRef<SpeechOnsetTracker | null>(null);
  const getSpeechOnsetTracker = useCallback(() => {
    if (!speechOnsetRef.current) speechOnsetRef.current = new SpeechOnsetTracker();
    return speechOnsetRef.current;
  }, []);

  /**
   * The time to show for a new user bubble. Prefer the measured speech onset
   * over the moment the STT text arrived; fall back to receipt time when no
   * onset was observed (text input, a very quiet start, or a missed frame).
   */
  const userTurnTime = useCallback((receiptTime: string): string => {
    const onset = getSpeechOnsetTracker().take();
    if (onset === null) return receiptTime;
    return clockTime(wallClockFromPerformance(onset, performance.now(), Date.now()));
  }, [getSpeechOnsetTracker]);

  const appendTranscript = useCallback((role: TranscriptItem["role"], text: string, time: string) => {
    if (role === "user") {
      assistantSourceRef.current = "none";
      userSourceRef.current = "final";
      userPartialIdRef.current = null;
      userPartialTextRef.current = "";
      finalizedUserLiveRef.current = "";
      finalizedUserTextRef.current = "";
    }
    const entry = getTranscriptLog().append(role, text);
    if (!entry) return;
    // Consecutive spoken segments are one reply, so they share one bubble.
    setTranscripts((prev) =>
      appendTranscriptLine(prev, {
        id: entry.id,
        role: entry.role,
        text: entry.text,
        time,
      })
    );
  }, [getTranscriptLog]);

  // Tokens bypass the dedupe log: repeated words are normal inside one reply.
  const appendAssistantToken = useCallback((token: string, time: string) => {
    assistantTokenIdRef.current += 1;
    setTranscripts((prev) =>
      appendAssistantTokenLine(prev, {
        id: `assistant-token-${assistantTokenIdRef.current}`,
        role: "assistant",
        text: token,
        time,
        live: true,
      })
    );
  }, []);

  // Interim STT deltas grow one user bubble; they are not deduped because
  // each delta is a new piece of the same utterance.
  const appendUserPartial = useCallback((delta: string, time: string) => {
    if (userSourceRef.current === "final") {
      const decision = classifyUserDeltaAfterFinal(
        finalizedUserLiveRef.current,
        finalizedUserTextRef.current,
        delta
      );
      finalizedUserLiveRef.current = decision.liveText;
      if (decision.late) return;
      // A delta that no longer continues the finalized text is the next turn.
      finalizedUserTextRef.current = "";
    }
    userSourceRef.current = "partial";
    userPartialTextRef.current += delta;
    let id = userPartialIdRef.current;
    const startsTurn = id === null;
    if (!id) {
      userPartialSeqRef.current += 1;
      id = `user-partial-${userPartialSeqRef.current}`;
      userPartialIdRef.current = id;
    }
    const lineTime = startsTurn ? userTurnTime(time) : time;
    setTranscripts((prev) =>
      appendUserPartialLine(finishLiveLine(prev, "assistant"), {
        id,
        role: "user",
        text: delta,
        time: lineTime,
        live: true,
      })
    );
  }, [userTurnTime]);

  // The final user transcript replaces the live bubble instead of adding a
  // second copy of the same utterance.
  const finalizeUserTranscript = useCallback((text: string, time: string) => {
    const finalText = text.trim();
    const liveText = userPartialTextRef.current;
    const entry = getTranscriptLog().append("user", finalText);
    const partialId = userPartialIdRef.current;
    // A duplicate final must not consume the next turn's onset.
    if (!entry && !partialId) return;
    const lineTime = partialId ? time : userTurnTime(time);
    userPartialIdRef.current = null;
    userPartialTextRef.current = "";
    finalizedUserLiveRef.current = liveText;
    finalizedUserTextRef.current = finalText;
    userSourceRef.current = "final";
    assistantSourceRef.current = "none";

    setTranscripts((prev) => {
      const base = finishLiveLine(prev, "assistant");
      if (partialId) {
        const replaced = replaceTranscriptLine(base, partialId, entry?.text ?? finalText);
        if (replaced) return replaced;
      }
      if (!entry) return base;
      return appendTranscriptLine(base, {
        id: entry.id,
        role: "user",
        text: entry.text,
        time: lineTime,
      });
    });

    // The final transcript is the end of this turn. Re-arm the onset clock so
    // the next turn measures its own speech, not a pause in this one.
    getSpeechOnsetTracker().reset();
  }, [getSpeechOnsetTracker, getTranscriptLog, userTurnTime]);

  // Keep the "speaking" badge alive while tokens stream, then clear it once.
  const markBotSpeaking = useCallback(() => {
    botSpeakingTimersRef.current.forEach((timer) => clearTimeout(timer));
    botSpeakingTimersRef.current.clear();
    setIsBotSpeaking(true);
    const timer = setTimeout(() => {
      botSpeakingTimersRef.current.delete(timer);
      setIsBotSpeaking(false);
    }, 3000);
    botSpeakingTimersRef.current.add(timer);
  }, []);

  // Clean teardown helper
  const cleanup = useCallback(() => {
    sessionGenerationRef.current += 1;
    isSessionStartingRef.current = false;
    voiceSessionRef.current?.close();
    voiceSessionRef.current = null;
    if (remoteAudioRef.current) remoteAudioRef.current.srcObject = null;
    if (animationFrameRef.current) {
      cancelAnimationFrame(animationFrameRef.current);
      animationFrameRef.current = null;
    }
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (pingIntervalRef.current) {
      clearInterval(pingIntervalRef.current);
      pingIntervalRef.current = null;
    }
    botSpeakingTimersRef.current.forEach((timer) => clearTimeout(timer));
    botSpeakingTimersRef.current.clear();
    if (audioContextRef.current && audioContextRef.current.state !== "closed") {
      audioContextRef.current.close().catch(() => {});
      audioContextRef.current = null;
    }
    getTranscriptLog().reset();
    getSpeechOnsetTracker().reset();
    assistantSourceRef.current = "none";
    userSourceRef.current = "none";
    userPartialIdRef.current = null;
    userPartialTextRef.current = "";
    finalizedUserLiveRef.current = "";
    finalizedUserTextRef.current = "";
    setIsBotSpeaking(false);
    setAudioBlocked(false);
  }, [getSpeechOnsetTracker, getTranscriptLog]);

  const handleEnableAudio = useCallback(async () => {
    if (remoteAudioRef.current) {
      try {
        await remoteAudioRef.current.play();
        setAudioBlocked(false);
      } catch (err) {
        console.warn("Failed to enable audio playback:", err);
      }
    }
  }, []);

  // Canvas visualizer loop
  const startVisualizer = useCallback(
    (analyser: AnalyserNode) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      const bufferLength = analyser.frequencyBinCount;
      const dataArray = new Uint8Array(bufferLength);
      const timeArray = new Uint8Array(bufferLength);

      const render = () => {
        animationFrameRef.current = requestAnimationFrame(render);
        analyser.getByteFrequencyData(dataArray);
        analyser.getByteTimeDomainData(timeArray);

        const width = canvas.width;
        const height = canvas.height;

        ctx.clearRect(0, 0, width, height);

        // Background subtle grid
        ctx.strokeStyle = "rgba(147, 139, 125, 0.28)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, height / 2);
        ctx.lineTo(width, height / 2);
        ctx.stroke();

        // Render Waveform
        ctx.lineWidth = 2.5;
        ctx.strokeStyle = "#c98753";

        ctx.beginPath();
        const sliceWidth = width / bufferLength;
        let x = 0;
        let sumSquares = 0;

        for (let i = 0; i < bufferLength; i++) {
          const v = timeArray[i] / 128.0;
          const centered = v - 1;
          sumSquares += centered * centered;
          const y = (v * height) / 2;

          if (i === 0) {
            ctx.moveTo(x, y);
          } else {
            ctx.lineTo(x, y);
          }
          x += sliceWidth;
        }

        ctx.stroke();

        // The dialog sends this same microphone stream. Its energy is the
        // honest clock for "when the user started speaking"; the STT text can
        // arrive a second later.
        const rms = Math.sqrt(sumSquares / bufferLength);
        getSpeechOnsetTracker().observe(rms, performance.now());

        // Ambient spectral glow bars at base
        const barWidth = (width / bufferLength) * 2.5;
        let barX = 0;
        for (let i = 0; i < bufferLength; i += 2) {
          const barHeight = (dataArray[i] / 255) * (height / 2);
          ctx.fillStyle = `rgba(201, 135, 83, ${0.15 + (dataArray[i] / 255) * 0.45})`;
          ctx.fillRect(barX, height - barHeight, barWidth - 1, barHeight);
          barX += barWidth;
        }
      };

      render();
    },
    [getSpeechOnsetTracker]
  );

  // Inbound WebRTC Data Channel message dispatcher
  const handleDataChannelMessage = useCallback((event: MessageEvent) => {
    const now = clockTime(new Date());

    // The LLM's token stream is the realtime representation. Appending every
    // token to one bubble shows the reply as it is generated, without the
    // per-token fragments the old dialog created.
    const token = assistantTokenFromDataChannel(event.data);
    if (token !== null) {
      if (assistantSourceRef.current === "completed") return;
      assistantSourceRef.current = "tokens";
      markBotSpeaking();
      appendAssistantToken(token, now);
      return;
    }

    // Interim STT deltas arrive while the user is still speaking. They grow
    // one bubble; the final project transcript replaces it in place. The
    // classifier in `appendUserPartial` also handles the trailing deltas that
    // arrive after a final and the first delta of the next turn.
    const partial = userPartialFromDataChannel(event.data);
    if (partial !== null) {
      appendUserPartial(partial, now);
      return;
    }

    // `user-started-speaking` is deliberately not a turn boundary: the real
    // realtime stream can emit it after the first delta, or even after the
    // final transcript. Turn boundaries come from the deltas above.
    const eventType = dataChannelEventType(event.data);
    if (eventType === "bot-llm-stopped" || eventType === "bot-stopped-speaking") {
      setTranscripts((prev) => finishLiveLine(prev, "assistant"));
      return;
    }

    // The project's completed transcript is the final source for the user,
    // and the assistant fallback when tokens are unavailable. Its assistant
    // copy arrives only after TTS has spoken the sentence, so ignore it when
    // the tokens already showed it.
    const transcript = transcriptFromDataChannel(event.data);
    if (!transcript) return;

    if (transcript.role === "user") {
      finalizeUserTranscript(transcript.text, now);
      return;
    }

    if (assistantSourceRef.current === "tokens") return;
    assistantSourceRef.current = "completed";
    markBotSpeaking();
    appendTranscript("assistant", transcript.text, now);
  }, [
    appendAssistantToken,
    appendTranscript,
    appendUserPartial,
    finalizeUserTranscript,
    markBotSpeaking,
  ]);

  // Connect a single cancellable local voice session.
  const startSession = useCallback(async () => {
    if (isSessionStartingRef.current) return;
    cleanup();
    isSessionStartingRef.current = true;
    const generation = sessionGenerationRef.current;
    const isCurrent = () => generation === sessionGenerationRef.current;
    setErrorMessage(null);
    setTextInputError(null);
    setDuration(0);
    setTranscripts([]);
    assistantSourceRef.current = "none";
    userSourceRef.current = "none";
    userPartialIdRef.current = null;
    userPartialTextRef.current = "";
    finalizedUserLiveRef.current = "";
    finalizedUserTextRef.current = "";
    setAudioBlocked(false);
    const session = new VoiceSession({
      getUserMedia: (constraints) => navigator.mediaDevices.getUserMedia(constraints),
      createPeerConnection: (configuration) => new RTCPeerConnection(configuration),
      fetcher: fetch,
      onDataChannelMessage: handleDataChannelMessage,
      onStatus: setStatus,
      onActivated: (activatedAgent) => onActivatedRef.current?.(activatedAgent),
      onConnectionState: (connectionState) => {
        if (connectionState === "connected") setStatus("connected");
        else if (["disconnected", "failed", "closed"].includes(connectionState)) setStatus("disconnected");
      },
      onRemoteTrack: (event) => {
        if (!remoteAudioRef.current) return;
        remoteAudioRef.current.srcObject = event.streams[0] || new MediaStream([event.track]);
        remoteAudioRef.current.play().catch((error: unknown) => {
          if (error instanceof Error && error.name === "NotAllowedError") setAudioBlocked(true);
        });
      },
    });
    session.setMuted(isMutedRef.current);
    voiceSessionRef.current = session;

    try {
      const target = agentRef.current;
      const apiOrigin = api.getBaseUrl().replace(/\/api\/?$/, "");
      await session.connect({
        offerUrl: `${apiOrigin}/api/offer`,
        agentId: target.id,
        isActive: target.is_active,
        activate: async () => {
          try {
            return await api.activateAgent(target.id);
          } catch (error) {
            const message = error instanceof Error ? error.message : "Failed to activate agent";
            throw new Error(`Activation error: ${message}`);
          }
        },
      });
      if (!isCurrent()) return;

      const windowWithAudio = window as unknown as {
        AudioContext?: typeof AudioContext;
        webkitAudioContext?: typeof AudioContext;
      };
      const AudioCtx = windowWithAudio.AudioContext || windowWithAudio.webkitAudioContext;
      if (AudioCtx) {
        const audioCtx = new AudioCtx();
        audioContextRef.current = audioCtx;
        const localStream = session.getLocalStream();
        if (localStream) {
          const source = audioCtx.createMediaStreamSource(localStream);
          const analyser = audioCtx.createAnalyser();
          analyser.fftSize = 256;
          analyser.smoothingTimeConstant = 0.8;
          source.connect(analyser);
          analyserRef.current = analyser;
          startVisualizer(analyser);
        }
      }

      pingIntervalRef.current = setInterval(() => session.sendPing(), 1500);
      timerRef.current = setInterval(() => setDuration((prev) => prev + 1), 1000);
    } catch (error: unknown) {
      if (!isCurrent()) return;
      cleanup();
      setStatus("error");
      setErrorMessage(error instanceof Error ? error.message : "Unexpected failure establishing WebRTC voice connection");
    } finally {
      if (generation === sessionGenerationRef.current) isSessionStartingRef.current = false;
    }
  }, [cleanup, handleDataChannelMessage, startVisualizer]);

  const startSessionRef = useRef(startSession);
  startSessionRef.current = startSession;
  const cleanupRef = useRef(cleanup);
  cleanupRef.current = cleanup;

  // Handle open / close lifecycle
  useEffect(() => {
    if (isOpen) {
      const previouslyFocused = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
      // Lock the page behind the modal, remembering the exact inline value.
      const previousBodyOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      requestAnimationFrame(() => dialogRef.current?.focus());
      startSessionRef.current();
      return () => {
        cleanupRef.current();
        document.body.style.overflow = previousBodyOverflow;
        previouslyFocused?.focus();
      };
    } else {
      cleanupRef.current();
      setStatus("idle");
    }
    return () => {
      cleanupRef.current();
    };
  }, [isOpen]);

  // Keyboard shortcut: ESC to close
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && isOpen) {
        handleEndSession();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen]);

  const handleToggleMute = () => {
    const nextMuted = !isMuted;
    setIsMuted(nextMuted);
    isMutedRef.current = nextMuted;
    persistMuteState(nextMuted);
    voiceSessionRef.current?.setMuted(nextMuted);
  };

  const handleClearTranscript = () => {
    getTranscriptLog().reset();
    assistantSourceRef.current = "none";
    userSourceRef.current = "none";
    userPartialIdRef.current = null;
    userPartialTextRef.current = "";
    finalizedUserLiveRef.current = "";
    finalizedUserTextRef.current = "";
    setTranscripts([]);
  };

  const handleEndSession = () => {
    cleanup();
    setStatus("disconnected");
    onClose();
  };
  const handleSendMessage = (e: React.FormEvent) => {
    e.preventDefault();
    const validation = validateClientUtterance(textInput, MAX_CLIENT_UTTERANCE_CHARS);
    if (!validation.valid) {
      if (!validation.text) return;
      setTextInputError(
        `Message exceeds the ${validation.maxChars.toLocaleString()}-character limit (${validation.codePointCount.toLocaleString()} characters). Shorten the message and try again.`
      );
      return;
    }
    const trimmed = validation.text;

    const now = clockTime(new Date());

    if (!voiceSessionRef.current?.sendText(trimmed)) {
      setErrorMessage("The call disconnected before the message was sent.");
      return;
    }

    setTextInputError(null);
    appendTranscript("user", trimmed, now);
    setTextInput("");
  };


  const formatTimer = (totalSeconds: number) => {
    const mins = Math.floor(totalSeconds / 60)
      .toString()
      .padStart(2, "0");
    const secs = (totalSeconds % 60).toString().padStart(2, "0");
    return `${mins}:${secs}`;
  };

  if (!isOpen) return null;

  return (
    <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-ink-950/90 p-2 sm:p-5"
      onClick={handleEndSession}
    >
      {/* Off-screen audio element for reliable browser audio playback */}
      <audio
        ref={remoteAudioRef}
        autoPlay
        playsInline
        style={{
          position: "absolute",
          opacity: 0,
          pointerEvents: "none",
          width: 1,
          height: 1,
        }}
      />

      {/* Modal Dialog Card */}
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="voice-test-title"
        tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key !== "Tab" || !dialogRef.current) return;
          const dialog = dialogRef.current;
          const focusable = dialog.querySelectorAll<HTMLElement>(
            'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
          );
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          const active = document.activeElement;
          if (!(active instanceof Node) || !dialog.contains(active)) {
            // Focus escaped the dialog: pull it back to the first control.
            event.preventDefault();
            first?.focus();
            return;
          }
          if (event.shiftKey && (active === first || active === dialog)) {
            event.preventDefault();
            last?.focus();
          } else if (!event.shiftKey && active === last) {
            event.preventDefault();
            first?.focus();
          }
        }}
        className="relative flex max-h-[96vh] w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-ink-700 bg-ink-900 text-limestone-100 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-4 border-b border-ink-800 px-4 py-4 sm:px-6">
          <div className="min-w-0">
            <div>
              <div className="flex items-center gap-2">
                <h3 id="voice-test-title" className="truncate text-lg font-semibold tracking-tight text-limestone-100">
                  {agent.name}
                </h3>
              </div>
              <p className="mt-1 text-sm text-limestone-400">Voice session · {agent.llm_model}</p>
            </div>
          </div>
          <button
            onClick={handleEndSession}
            aria-label="Close voice session"
            className="button-secondary min-h-9 px-2.5"
            title="Close voice test"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Real-time Status Banner */}
        <div className="flex items-center justify-between gap-3 border-b border-ink-800 bg-ink-950 px-4 py-3 text-sm sm:px-6">
          <div className="flex items-center gap-2.5">
            {status === "connected" ? (
              <span className="flex items-center gap-2 text-moss-200 font-medium">
                <span className="h-2 w-2 rounded-full bg-moss-400" />
                Connected
              </span>
            ) : status === "connecting" ||
              status === "activating" ||
              status === "requesting_media" ? (
              <span className="flex items-center gap-2 text-copper-200 font-medium">
                <RefreshCw className="h-4 w-4 animate-spin text-copper-400" />
                {status === "activating"
                  ? "Activating Agent..."
                  : status === "requesting_media"
                  ? "Opening Microphone..."
                  : "Establishing WebRTC Handshake..."}
              </span>
            ) : status === "error" ? (
              <span className="flex items-center gap-1.5 text-rust-400 font-medium">
                <AlertCircle className="w-3.5 h-3.5" />
                Connection Error
              </span>
            ) : (
              <span className="flex items-center gap-1.5 text-limestone-400 font-medium">
                <WifiOff className="w-3.5 h-3.5" />
                Disconnected
              </span>
            )}

            <span className="text-limestone-400">
              {formatTimer(duration)}
            </span>
          </div>
          <span className="text-xs text-limestone-500">{agent.tts_voice_en} / {agent.tts_voice_hi}</span>
        {/* Autoplay blocked banner */}
        {audioBlocked && (
          <div className="flex items-center justify-between gap-3 border-b border-copper-800 bg-copper-950 px-4 py-3 text-sm text-copper-100 sm:px-6">
            <span>Audio playback is paused by your browser's autoplay policy.</span>
            <button
              type="button"
              onClick={handleEnableAudio}
              className="button-primary min-h-9 px-3"
            >
              Click to Enable Audio
            </button>
          </div>
        )}
        </div>

        {/* Scrollable Main Body */}
        <div className="grid grid-cols-1 gap-4 overflow-y-auto p-4 sm:p-5 md:grid-cols-[220px_minmax(0,1fr)] sm:gap-5">
          {/* Audio Waveform Canvas */}
          <div className="surface h-fit p-4">
            <div className="mb-3 flex items-center justify-between gap-2 text-sm text-limestone-300">
              <span className="flex items-center gap-2">
                <Volume2 className="h-4 w-4 text-copper-300" />Microphone
              </span>
              {isMuted ? (
                <span className="rounded-md border border-rust-700 bg-rust-950 px-2 py-1 text-xs text-rust-200">
                  Microphone Muted
                </span>
              ) : isBotSpeaking ? (
                <span className="rounded-md border border-moss-700 bg-moss-950 px-2 py-1 text-xs text-moss-200">
                  Response generated
                </span>
              ) : (
                <span className="text-xs text-limestone-500">
                  {status === "connected" ? "Listening" : "Waiting"}
                </span>
              )}
            </div>

            <div className="flex h-20 w-full items-center justify-center rounded-md bg-ink-950">
              <canvas
                ref={canvasRef}
                width={640}
                height={56}
                className="w-full h-full rounded-lg"
              />
            </div>
          </div>

          {/* Dedicated Live Transcript Section */}
          <div className="surface flex min-h-[320px] flex-col overflow-hidden">
            <div className="flex items-center justify-between border-b border-ink-800 px-4 py-3">
              <div className="flex items-center gap-2">
                <MessageSquare className="w-4 h-4 text-copper-400" />
                <span className="text-sm font-semibold text-limestone-100">
                  Conversation
                </span>
                <span className="text-xs text-limestone-500">
                  {transcripts.length} {transcripts.length === 1 ? "turn" : "turns"}
                </span>
              </div>

              <div className="flex items-center gap-2">
                {status === "connected" && (
                  <span className="inline-flex items-center gap-1.5 text-xs text-moss-300 mr-2">
                    <span className="h-1.5 w-1.5 rounded-full bg-moss-400" />
                    Connected
                  </span>
                )}
                {transcripts.length > 0 && (
                  <button
                    type="button"
                    onClick={handleClearTranscript}
                    className="p-1 rounded text-limestone-500 hover:text-limestone-300 transition-colors"
                    title="Clear transcript"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
            </div>

            {/* Transcript Messages Area */}
            <div className="min-h-[220px] max-h-[52vh] overflow-y-auto p-4 space-y-3 bg-ink-950/40 font-sans">
              {transcripts.length === 0 ? (
                <div className="h-full flex flex-col items-center justify-center text-center p-6 text-limestone-500 space-y-2">
                  <Mic className="w-8 h-8 text-limestone-600 animate-pulse" />
                  <p className="text-xs font-medium text-limestone-400">
                    Listening for voice input
                  </p>
                  <p className="text-[11px] text-limestone-500 max-w-sm">
                    Speak in English or Hindi into your microphone. Speech-to-text and generated assistant responses appear here during the session.
                  </p>
                </div>
              ) : (
                <>
                  {transcripts.map((item) => (
                    <div
                      key={item.id}
                      className={`flex flex-col gap-1 ${
                        item.role === "user" ? "items-end" : "items-start"
                      }`}
                    >
                      <div className="flex items-center gap-1.5 text-[10px] font-mono text-limestone-400 px-1">
                        {item.role === "user" ? (
                          <>
                            <span>{item.time}</span>
                            <span className="text-copper-400 font-semibold flex items-center gap-1">
                              <User className="w-3 h-3" /> You (Heard)
                            </span>
                          </>
                        ) : (
                          <>
                            <span className="text-moss-400 font-semibold flex items-center gap-1">
                              <Bot className="w-3 h-3" /> {agent.name} (Generated)
                            </span>
                            <span>{item.time}</span>
                          </>
                        )}
                      </div>

                      <div
                        className={`p-3 rounded-xl text-xs max-w-[85%] leading-relaxed ${
                          item.role === "user"
                            ? "bg-copper-500/10 border border-copper-500/30 text-copper-100 rounded-tr-sm"
                            : "bg-ink-900 border border-ink-800 text-limestone-200 rounded-tl-sm"
                        }`}
                      >
                        {item.text}
                        {item.live && (
                          <span
                            data-typing-caret="true"
                            aria-hidden="true"
                            className="ml-0.5 inline-block h-3 w-0.5 animate-pulse bg-current align-middle"
                          />
                        )}
                      </div>
                    </div>
                  ))}

                  <div ref={transcriptEndRef} />
                </>
              )}
            </div>

            {/* Text message fallback input bar */}
            <form
              onSubmit={handleSendMessage}
              className="flex shrink-0 flex-col gap-1.5 border-t border-ink-800 bg-ink-900 p-3"
            >
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  aria-label="Message the agent"
                  value={textInput}
                  onChange={(e) => {
                    setTextInput(e.target.value);
                    if (textInputError) setTextInputError(null);
                  }}
                  placeholder="Type a message or speak into your microphone..."
                  disabled={status !== "connected"}
                  className="field-control flex-1 disabled:opacity-50"
                />
                <button
                  type="submit"
                  disabled={status !== "connected" || !textInput.trim()}
                  className="button-primary min-h-10 px-3"
                >
                  <Send className="w-3.5 h-3.5" />
                  <span>Send</span>
                </button>
              </div>
              {textInputError && (
                <p role="alert" className="text-xs text-rust-300">
                  {textInputError}
                </p>
              )}
            </form>
          </div>

          {/* Error Notice */}
          {errorMessage && (
            <div className="p-4 rounded-xl bg-rust-500/10 border border-rust-500/20 text-rust-300 text-xs flex items-start gap-3">
              <ShieldAlert className="w-5 h-5 text-rust-400 shrink-0 mt-0.5" />
              <div className="space-y-1">
                <p className="font-semibold text-rust-200">Connection Failed</p>
                <p>{errorMessage}</p>
                <p className="text-limestone-400 text-[11px] mt-1 font-mono">
                  Make sure the voice agent backend is running:{" "}
                  <code className="text-copper-300">./scripts/voice-ai.sh start</code>
                </p>
              </div>
            </div>
          )}

          {/* Technical Specs Strip */}
        </div>

        {/* Footer Actions */}
        <div className="p-4 border-t border-ink-800 bg-ink-950/60 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={handleToggleMute}
              disabled={status !== "connected"}
              className={`inline-flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-semibold transition-all cursor-pointer ${
                isMuted
                  ? "bg-rust-500/20 text-rust-300 border border-rust-500/30 hover:bg-rust-500/30"
                  : "bg-ink-800 text-limestone-200 border border-ink-700 hover:bg-ink-700"
              } disabled:opacity-50 disabled:cursor-not-allowed`}
            >
              {isMuted ? (
                <>
                  <MicOff className="w-4 h-4 text-rust-400" /> Unmute Mic
                </>
              ) : (
                <>
                  <Mic className="w-4 h-4 text-copper-400" /> Mute Mic
                </>
              )}
            </button>

            {status === "error" || status === "disconnected" ? (
              <button
                type="button"
                onClick={startSession}
                className="inline-flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-semibold bg-copper-600 hover:bg-copper-500 text-white transition-colors cursor-pointer"
              >
                <RefreshCw className="w-4 h-4" /> Reconnect
              </button>
            ) : null}
          </div>

          <button
            type="button"
            onClick={handleEndSession}
            className="inline-flex items-center gap-2 px-5 py-2 rounded-xl text-xs font-semibold bg-rust-600 hover:bg-rust-500 text-white shadow-lg shadow-rust-900/30 transition-all cursor-pointer"
          >
            <PhoneOff className="w-4 h-4" /> End Session
          </button>
        </div>
      </div>
    </div>
  );
}

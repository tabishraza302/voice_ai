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
import { VoiceSession } from "@/lib/voice-session";
import { TranscriptLog } from "@/lib/transcript-log";
import type { Agent } from "@/types/agent";

export interface TranscriptItem {
  id: string;
  role: "user" | "assistant";
  text: string;
  time: string;
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
  const [transcripts, setTranscripts] = useState<TranscriptItem[]>([]);
  const [isBotSpeaking, setIsBotSpeaking] = useState(false);
  const [audioBlocked, setAudioBlocked] = useState(false);

  const agentRef = useRef(agent);
  agentRef.current = agent;
  const onActivatedRef = useRef(onActivated);
  onActivatedRef.current = onActivated;
  const isSessionStartingRef = useRef(false);
  const sessionGenerationRef = useRef(0);
  // One dedupe log per mounted modal; reset when a call is torn down.
  const transcriptLogRef = useRef<TranscriptLog | null>(null);
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
  // Auto-scroll transcript container
  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [transcripts]);

  const getTranscriptLog = useCallback(() => {
    if (!transcriptLogRef.current) transcriptLogRef.current = new TranscriptLog();
    return transcriptLogRef.current;
  }, []);

  const appendTranscript = useCallback((role: TranscriptItem["role"], text: string, time: string) => {
    const entry = getTranscriptLog().append(role, text);
    if (!entry) return;
    setTranscripts((prev) => [
      ...prev,
      { id: entry.id, role: entry.role, text: entry.text, time },
    ]);
  }, [getTranscriptLog]);

  // Show the "speaking" badge for 3000ms after each bot transcript.
  const markBotSpeaking = useCallback(() => {
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
    setIsBotSpeaking(false);
    setAudioBlocked(false);
  }, [getTranscriptLog]);

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

        for (let i = 0; i < bufferLength; i++) {
          const v = timeArray[i] / 128.0;
          const y = (v * height) / 2;

          if (i === 0) {
            ctx.moveTo(x, y);
          } else {
            ctx.lineTo(x, y);
          }
          x += sliceWidth;
        }

        ctx.stroke();

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
    []
  );

  // Inbound WebRTC Data Channel message dispatcher
  const handleDataChannelMessage = useCallback((event: MessageEvent) => {
    try {
      if (typeof event.data !== "string") return;
      const parsed: unknown = JSON.parse(event.data);
      if (!parsed || typeof parsed !== "object") return;

      const now = new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });

      // Handle standard transcript messages from pipeline
      if ("type" in parsed && parsed.type === "transcript") {
        const text =
          "text" in parsed && typeof parsed.text === "string"
            ? parsed.text.trim()
            : "";
        const role =
          "role" in parsed && parsed.role === "user"
            ? "user"
            : "assistant";

        if (text) {
          if (role === "assistant") {
            markBotSpeaking();
          }

          appendTranscript(role, text, now);
        }
      } else if (
        "type" in parsed &&
        (parsed.type === "bot-transcription" ||
          parsed.type === "bot-output" ||
          parsed.type === "bot-llm-text")
      ) {
        let text = "";
        if ("data" in parsed && parsed.data && typeof parsed.data === "object" && "text" in parsed.data && typeof parsed.data.text === "string") {
          text = parsed.data.text.trim();
        } else if ("text" in parsed && typeof parsed.text === "string") {
          text = parsed.text.trim();
        }

        if (text) {
          markBotSpeaking();
          appendTranscript("assistant", text, now);
        }
      } else if (
        "type" in parsed &&
        (parsed.type === "user-transcription" ||
          parsed.type === "user-llm-text")
      ) {
        let text = "";
        if ("data" in parsed && parsed.data && typeof parsed.data === "object" && "text" in parsed.data && typeof parsed.data.text === "string") {
          text = parsed.data.text.trim();
        } else if ("text" in parsed && typeof parsed.text === "string") {
          text = parsed.text.trim();
        }

        if (text) {
          appendTranscript("user", text, now);
        }
      }
    } catch {
      // Ignore non-JSON ping/binary messages
    }
  }, [appendTranscript, markBotSpeaking]);

  // Connect a single cancellable local voice session.
  const startSession = useCallback(async () => {
    if (isSessionStartingRef.current) return;
    cleanup();
    isSessionStartingRef.current = true;
    const generation = sessionGenerationRef.current;
    const isCurrent = () => generation === sessionGenerationRef.current;
    setErrorMessage(null);
    setDuration(0);
    setTranscripts([]);
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
    if (!voiceSessionRef.current) return;
    voiceSessionRef.current.setMuted(!isMuted);
    setIsMuted(!isMuted);
  };

  const handleClearTranscript = () => {
    getTranscriptLog().reset();
    setTranscripts([]);
  };

  const handleEndSession = () => {
    cleanup();
    setStatus("disconnected");
    onClose();
  };
  const handleSendMessage = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = textInput.trim();
    if (!trimmed) return;

    const now = new Date().toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });

    if (!voiceSessionRef.current?.sendText(trimmed)) {
      setErrorMessage("The call disconnected before the message was sent.");
      return;
    }

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
              className="flex shrink-0 items-center gap-2 border-t border-ink-800 bg-ink-900 p-3"
            >
              <input
                type="text"
                aria-label="Message the agent"
                value={textInput}
                onChange={(e) => setTextInput(e.target.value)}
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

"use client";

import { useEffect, useRef, useState } from "react";
import { api, apiErrorText } from "../lib/api";
import type { ChatSessionCreated } from "../lib/types";
import { Button } from "./ui/button";
import { cn } from "./ui/utils";

export interface ChatPanelProps {
  agentId: string;
  agentName: string;
  languages: string[];
  defaultLanguage?: string | null;
  open: boolean;
  onClose: () => void;
}

export type ChatMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
};

export type ChatState =
  | { kind: "closed" }
  | { kind: "creating" }
  | { kind: "connecting"; sessionId: string }
  | { kind: "active"; sessionId: string; messages: ChatMessage[] }
  | { kind: "ended"; sessionId: string; messages: ChatMessage[]; reason: string }
  | { kind: "error"; message: string };

export function ChatPanel({
  agentId,
  agentName,
  languages,
  defaultLanguage,
  open,
  onClose,
}: ChatPanelProps) {
  const [state, setState] = useState<ChatState>({ kind: "closed" });
  const [input, setInput] = useState("");
  const wsRef = useRef<WebSocket | null>(null);
  const nextIdRef = useRef(1);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const startSession = async () => {
    setState({ kind: "creating" });
    try {
      const selectedLanguage = defaultLanguage || (languages.length > 0 ? languages[0] : "en");
      const res = await api<ChatSessionCreated>(`/agents/${agentId}/chat-sessions`, {
        method: "POST",
        body: JSON.stringify({ language: selectedLanguage }),
      });
      setState({ kind: "connecting", sessionId: res.session_id });
    } catch (err) {
      setState({
        kind: "error",
        message: apiErrorText(err, "Could not start chat session. Check your agent configuration."),
      });
    }
  };

  // Trigger session start on open if closed
  useEffect(() => {
    if (open && state.kind === "closed") {
      startSession();
    }
  }, [open, state.kind]);

  // Clean up when panel is closed from outside
  useEffect(() => {
    if (!open && state.kind !== "closed") {
      if (wsRef.current) {
        if (wsRef.current.readyState === WebSocket.OPEN) {
          try {
            wsRef.current.send(JSON.stringify({ type: "end" }));
          } catch {}
          wsRef.current.close();
        }
        wsRef.current = null;
      }
      setState({ kind: "closed" });
    }
  }, [open]);

  // WebSocket connection effect
  const activeSessionId =
    state.kind === "connecting" || state.kind === "active" ? state.sessionId : null;

  // WebSocket connection effect
  useEffect(() => {
    if (!activeSessionId) return;
    const sessionId = activeSessionId;
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/api/agents/${agentId}/chat-sessions/${sessionId}/ws`;

    let closedNormally = false;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setState({ kind: "active", sessionId, messages: [] });
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "token") {
          setState((prev) => {
            if (prev.kind !== "active") return prev;
            const msgs = [...prev.messages];
            const last = msgs[msgs.length - 1];
            if (last && last.role === "assistant" && last.streaming) {
              msgs[msgs.length - 1] = {
                ...last,
                content: last.content + (data.content || ""),
              };
            } else {
              msgs.push({
                id: nextIdRef.current++,
                role: "assistant",
                content: data.content || "",
                streaming: true,
              });
            }
            return { ...prev, messages: msgs };
          });
        } else if (data.type === "turn_complete") {
          setState((prev) => {
            if (prev.kind !== "active") return prev;
            const msgs = [...prev.messages];
            const last = msgs[msgs.length - 1];
            if (last && last.role === "assistant" && last.streaming) {
              msgs[msgs.length - 1] = {
                ...last,
                content: data.content ?? last.content,
                streaming: false,
              };
            } else {
              msgs.push({
                id: nextIdRef.current++,
                role: "assistant",
                content: data.content || "",
                streaming: false,
              });
            }
            return { ...prev, messages: msgs };
          });
        } else if (data.type === "session_ended") {
          closedNormally = true;
          setState((prev) => {
            const prevMsgs = prev.kind === "active" || prev.kind === "ended" ? prev.messages : [];
            const msgs = prevMsgs.map((m) => (m.streaming ? { ...m, streaming: false } : m));
            return {
              kind: "ended",
              sessionId,
              messages: msgs,
              reason: data.reason || "completed",
            };
          });
        } else if (data.type === "error") {
          setState((prev) => {
            const prevMsgs = prev.kind === "active" || prev.kind === "ended" ? prev.messages : [];
            return {
              kind: "ended",
              sessionId,
              messages: prevMsgs.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
              reason: `error: ${data.message || "Unknown error"}`,
            };
          });
        }
      } catch {}
    };

    ws.onerror = () => {
      if (!closedNormally) {
        setState((prev) => {
          if (prev.kind === "ended" || prev.kind === "closed") return prev;
          return { kind: "error", message: "Failed to connect to chat session." };
        });
      }
    };

    ws.onclose = () => {
      if (!closedNormally) {
        setState((prev) => {
          if (prev.kind === "active") {
            return {
              kind: "ended",
              sessionId,
              messages: prev.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
              reason: "disconnected",
            };
          }
          return prev;
        });
      }
    };

    return () => {
      closedNormally = true;
      if (ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify({ type: "end" }));
        } catch {}
        ws.close();
      } else if (ws.readyState === WebSocket.CONNECTING) {
        ws.close();
      }
      wsRef.current = null;
    };
  }, [activeSessionId, agentId]);

  // Auto-scroll on new message content
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [state.kind === "active" || state.kind === "ended" ? state.messages : null]);

  // Auto-focus input when active
  useEffect(() => {
    if (open && state.kind === "active") {
      inputRef.current?.focus();
    }
  }, [open, state.kind]);

  const isAssistantStreaming =
    state.kind === "active" && state.messages.some((m) => m.streaming);

  const handleSendMessage = (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const text = input.trim();
    if (!text || state.kind !== "active" || isAssistantStreaming) return;

    const userMsg: ChatMessage = {
      id: nextIdRef.current++,
      role: "user",
      content: text,
    };
    setState((prev) =>
      prev.kind === "active" ? { ...prev, messages: [...prev.messages, userMsg] } : prev
    );
    setInput("");

    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "message", content: text }));
    }
  };

  const handleEndChat = () => {
    if (state.kind === "active" && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      try {
        wsRef.current.send(JSON.stringify({ type: "end" }));
      } catch {}
    }
  };

  const handleStartNewChat = () => {
    if (wsRef.current) {
      try {
        wsRef.current.close();
      } catch {}
      wsRef.current = null;
    }
    startSession();
  };

  return (
    <aside
      aria-label="Chat test panel"
      className={cn(
        "fixed inset-y-0 right-0 z-50 flex w-full max-w-[380px] flex-col border-l border-border bg-background shadow-2xl transition-transform duration-300 ease-in-out sm:w-[380px]",
        open ? "translate-x-0" : "translate-x-full pointer-events-none"
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border/80 px-4 py-3.5">
        <div className="min-w-0 pr-2">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-foreground">Chat test</span>
            {state.kind === "active" && (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-600 dark:text-emerald-400">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
                Live
              </span>
            )}
          </div>
          <p className="truncate text-xs text-muted-foreground">{agentName}</p>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {state.kind === "active" && (
            <Button
              variant="ghost"
              size="sm"
              onClick={handleEndChat}
              className="h-8 px-2.5 text-xs text-muted-foreground hover:text-destructive"
            >
              End chat
            </Button>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label="Close chat test"
            className="rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3.5 text-sm">
        {state.kind === "creating" && (
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-2 pt-16">
            <span className="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
            <span className="text-xs">Preparing chat session…</span>
          </div>
        )}
        {state.kind === "connecting" && (
          <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-2 pt-16">
            <span className="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
            <span className="text-xs">Connecting to agent…</span>
          </div>
        )}
        {state.kind === "error" && (
          <div className="rounded-xl border border-destructive/30 bg-destructive/10 p-4 text-xs text-destructive space-y-3 mt-4">
            <p className="font-medium">{state.message}</p>
            <Button variant="outline" size="sm" onClick={handleStartNewChat}>
              Retry
            </Button>
          </div>
        )}
        {(state.kind === "active" || state.kind === "ended") && (
          <>
            {state.messages.length === 0 && state.kind === "active" && (
              <div className="flex flex-col items-center justify-center text-muted-foreground py-12 text-xs">
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent mb-2" />
                <span>Waiting for agent to speak…</span>
              </div>
            )}
            {state.messages.map((msg) => (
              <div
                key={msg.id}
                className={cn(
                  "flex flex-col max-w-[85%] rounded-2xl px-3.5 py-2 text-sm leading-relaxed",
                  msg.role === "user"
                    ? "ml-auto bg-primary text-primary-foreground rounded-tr-sm"
                    : "mr-auto bg-muted text-foreground rounded-tl-sm"
                )}
              >
                <span className="whitespace-pre-wrap break-words">{msg.content}</span>
                {msg.streaming && (
                  <span className="inline-block w-1.5 h-3.5 ml-0.5 bg-foreground/60 animate-pulse align-middle" />
                )}
              </div>
            ))}
            {state.kind === "ended" && (
              <div className="mt-4 rounded-xl border border-border/80 bg-muted/40 p-3.5 text-center space-y-2.5">
                <p className="text-xs text-muted-foreground">
                  Session ended ({state.reason})
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleStartNewChat}
                  className="w-full text-xs"
                >
                  Start new chat
                </Button>
              </div>
            )}
            <div ref={messagesEndRef} />
          </>
        )}
      </div>

      {/* Input */}
      <form
        onSubmit={handleSendMessage}
        className="border-t border-border/80 bg-background/80 p-3 flex items-center gap-2"
      >
        <input
          ref={inputRef}
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={
            state.kind === "active"
              ? isAssistantStreaming
                ? "Agent is speaking…"
                : "Type a message…"
              : "Chat not active"
          }
          disabled={state.kind !== "active" || isAssistantStreaming}
          className="flex-1 rounded-xl border border-input bg-background px-3 py-2 text-sm transition-colors placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
        />
        <Button
          type="submit"
          size="sm"
          variant="primary"
          disabled={!input.trim() || state.kind !== "active" || isAssistantStreaming}
          className="h-9 w-9 p-0 rounded-xl shrink-0"
          aria-label="Send message"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h14M12 5l7 7-7 7" />
          </svg>
        </Button>
      </form>
    </aside>
  );
}

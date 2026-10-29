"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { RefreshCw, RotateCcw, Send } from "lucide-react";
import {
  platformAgentApi,
  type ChatMessage,
  type ChatOutcome,
  type ChatTestView,
  type ChatTraceEntry,
  type DraftView,
  type JsonObject,
  type VersionView,
} from "@/lib/platform-agent-client";
import { PlatformApiError } from "@/lib/platform-api-client";

interface ChatTestPanelProps {
  agentId: string;
  draft: DraftView;
  versions: VersionView[];
  onRefreshDraftSnapshot: () => Promise<boolean>;
}

interface ChatSession {
  testId: string;
  snapshotHash: string;
  snapshotLabel: string;
  turnSequence: number;
  messages: ChatMessage[];
  flowState: JsonObject;
  trace: ChatTraceEntry[];
  outcome: ChatOutcome;
}

interface PendingRecovery {
  expectedSequence: number;
  text: string;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The platform request failed.";
}

function isStaleSnapshotError(error: unknown): error is PlatformApiError {
  return error instanceof PlatformApiError &&
    error.status === 409 &&
    (error.message === "The saved draft or prompt policy changed before Chat Test started" ||
      error.message === "Chat Test snapshot does not match the pinned session");
}

function isChatRevisionConflict(error: unknown): error is PlatformApiError {
  return error instanceof PlatformApiError && error.status === 409 && error.code === "REVISION_CONFLICT";
}

function createSession(snapshotHash: string, snapshotLabel: string): ChatSession {
  if (!globalThis.crypto?.randomUUID) {
    throw new Error("This browser cannot create a pinned chat test ID.");
  }
  return {
    testId: globalThis.crypto.randomUUID(),
    snapshotHash,
    snapshotLabel,
    turnSequence: 0,
    messages: [],
    flowState: {},
    trace: [],
    outcome: "in_progress",
  };
}

function fromTranscript(transcript: ChatTestView, snapshotLabel: string): ChatSession {
  return {
    testId: transcript.test_id,
    snapshotHash: transcript.snapshot_hash,
    snapshotLabel,
    turnSequence: transcript.turn_sequence,
    messages: transcript.messages,
    flowState: transcript.flow_state,
    trace: transcript.trace,
    outcome: transcript.outcome,
  };
}

function outcomeLabel(outcome: ChatOutcome): string {
  switch (outcome) {
    case "in_progress": return "In progress";
    case "completed": return "Completed";
    case "partial": return "Partial";
    case "aborted": return "Aborted";
    case "failed": return "Failed";
  }
}

export function ChatTestPanel({ agentId, draft, versions, onRefreshDraftSnapshot }: ChatTestPanelProps) {
  const [selectedSnapshotId, setSelectedSnapshotId] = useState("draft");
  const [session, setSession] = useState<ChatSession | null>(null);
  const [messageText, setMessageText] = useState("");
  const [pendingRecovery, setPendingRecovery] = useState<PendingRecovery | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busyRef = useRef(false);

  useEffect(() => {
    if (selectedSnapshotId !== "draft" && !versions.some((version) => version.version_id === selectedSnapshotId)) {
      setSelectedSnapshotId("draft");
    }
  }, [selectedSnapshotId, versions]);

  const selectedVersion = versions.find((version) => version.version_id === selectedSnapshotId);
  const selectedSnapshotHash = selectedSnapshotId === "draft"
    ? draft.snapshot_hash
    : selectedVersion?.artifact_hash ?? null;
  const selectedSnapshotLabel = selectedSnapshotId === "draft"
    ? `Draft r${draft.draft_revision}`
    : selectedVersion ? `Published v${selectedVersion.version}` : `Draft r${draft.draft_revision}`;
  const selectedIsCurrent = selectedSnapshotHash !== null && session?.snapshotHash === selectedSnapshotHash;

  const applyTranscript = (transcript: ChatTestView, snapshotLabel: string) => {
    setSession(fromTranscript(transcript, snapshotLabel));
    setMessageText("");
    setPendingRecovery(null);
    setError(null);
  };

  const handleSend = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const text = messageText.trim();
    if (!text || busyRef.current || pendingRecovery) return;

    let activeSession = session;
    try {
      if (!activeSession) {
        if (!selectedSnapshotHash) {
          setError("This draft does not have an executable snapshot yet. Complete the flow before starting Chat Test.");
          return;
        }
        activeSession = createSession(selectedSnapshotHash, selectedSnapshotLabel);
        setSession(activeSession);
      }
    } catch (sessionError: unknown) {
      setError(errorMessage(sessionError));
      return;
    }

    const expectedSequence = activeSession.turnSequence + 1;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const turn = await platformAgentApi.sendChatTurn(agentId, {
        snapshot_hash: activeSession.snapshotHash,
        test_id: activeSession.testId,
        expected_turn_sequence: expectedSequence,
        text,
      });
      try {
        const transcript = await platformAgentApi.getChatTest(agentId, turn.test_id);
        applyTranscript(transcript, activeSession.snapshotLabel);
      } catch (transcriptError: unknown) {
        setPendingRecovery({ expectedSequence, text });
        setError(`The turn was accepted, but the durable transcript could not be refreshed: ${errorMessage(transcriptError)}`);
      }
    } catch (turnError: unknown) {
      const staleSnapshot = isStaleSnapshotError(turnError);
      const revisionConflict = isChatRevisionConflict(turnError);
      try {
        const transcript = await platformAgentApi.getChatTest(agentId, activeSession.testId);
        if (transcript.turn_sequence >= expectedSequence) {
          applyTranscript(transcript, activeSession.snapshotLabel);
        } else if (staleSnapshot) {
          const refreshed = await onRefreshDraftSnapshot();
          setError(refreshed
            ? `The pinned snapshot was rejected: ${errorMessage(turnError)}. This conversation remains pinned to its original snapshot. Start a new test on the selected snapshot to continue.`
            : `The pinned snapshot was rejected: ${errorMessage(turnError)}. The latest draft snapshot could not be loaded.`);
        } else {
          setError(errorMessage(turnError));
        }
      } catch (transcriptError: unknown) {
        if (staleSnapshot) {
          const refreshed = await onRefreshDraftSnapshot();
          setError(refreshed
            ? `The pinned snapshot was rejected: ${errorMessage(turnError)}. This conversation remains pinned to its original snapshot. Start a new test on the selected snapshot to continue.`
            : `The pinned snapshot was rejected: ${errorMessage(turnError)}. The latest draft snapshot could not be loaded. Transcript error: ${errorMessage(transcriptError)}`);
        } else if (revisionConflict) {
          setError(`The platform rejected this turn before saving it: ${errorMessage(turnError)}. Reset this test before sending again.`);
        } else {
          setPendingRecovery({ expectedSequence, text });
          setError(`The turn may have committed. Recover the transcript before sending again: ${errorMessage(turnError)}`);
        }
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const handleRecover = async () => {
    if (!session || !pendingRecovery || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const transcript = await platformAgentApi.getChatTest(agentId, session.testId);
      if (transcript.turn_sequence >= pendingRecovery.expectedSequence) {
        applyTranscript(transcript, session.snapshotLabel);
      } else {
        setPendingRecovery(null);
        setError("No committed turn was found. Your message is still in the input and can be sent again.");
      }
    } catch (recoveryError: unknown) {
      setError(`Could not recover the transcript: ${errorMessage(recoveryError)}`);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const handleReset = (snapshotHash: string, snapshotLabel: string) => {
    if (busyRef.current || pendingRecovery) return;
    try {
      setSession(createSession(snapshotHash, snapshotLabel));
      setError(null);
    } catch (sessionError: unknown) {
      setError(errorMessage(sessionError));
    }
  };

  return (
    <section aria-labelledby="chat-test-heading" className="flex min-h-[34rem] flex-col overflow-hidden rounded-2xl border border-ink-800 bg-ink-950/70 shadow-xl shadow-black/20 lg:h-[calc(100vh-2rem)]">
      <header className="space-y-3 border-b border-ink-800 p-4">
        <div>
          <div className="flex items-center justify-between gap-3">
            <h2 id="chat-test-heading" className="text-lg font-semibold text-limestone-100">Chat Test</h2>
            <span className="rounded-md border border-moss-800 bg-moss-950/60 px-2 py-1 text-xs text-moss-200">Text only</span>
          </div>
          <p className="mt-1 text-xs leading-5 text-limestone-400">Test a draft or published snapshot. This chat stays pinned to the selected hash.</p>
        </div>
        <div>
          <label htmlFor="chat-snapshot" className="field-label text-xs">Test snapshot</label>
          <select
            id="chat-snapshot"
            className="field-control text-sm"
            value={selectedSnapshotId}
            onChange={(event) => setSelectedSnapshotId(event.currentTarget.value)}
            disabled={busy || Boolean(pendingRecovery)}
          >
            <option value="draft">Draft r{draft.draft_revision}</option>
            {versions.map((version) => (
              <option key={version.version_id} value={version.version_id}>Published v{version.version}</option>
            ))}
          </select>
        </div>
        {session ? (
          <div className="rounded-lg border border-ink-800 bg-ink-950 p-3 text-xs">
            <p className="text-limestone-400">Pinned to {session.snapshotLabel}</p>
            <p className="mt-1 break-all font-mono text-[10px] text-limestone-500">{session.snapshotHash}</p>
            {!selectedIsCurrent ? (
              <p className="mt-2 text-copper-200">The selected snapshot has changed. This conversation still uses its original hash.</p>
            ) : null}
            <p className="mt-2 text-limestone-500">Test ID: <span className="font-mono">{session.testId}</span></p>
          </div>
        ) : (
          selectedSnapshotHash
            ? <p className="text-xs text-limestone-500">Pinned to {selectedSnapshotLabel}: <span className="break-all font-mono">{selectedSnapshotHash}</span></p>
            : <p role="status" className="text-xs text-copper-200">This draft has no executable snapshot yet. Complete the flow before starting Chat Test.</p>
        )}
        {session ? (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="button-secondary"
              onClick={() => handleReset(session.snapshotHash, session.snapshotLabel)}
              disabled={busy || Boolean(pendingRecovery)}
            >
              <RotateCcw className="h-4 w-4" />Reset on pinned snapshot
            </button>
            {selectedSnapshotHash && !selectedIsCurrent ? (
              <button
                type="button"
                className="button-secondary"
                onClick={() => handleReset(selectedSnapshotHash, selectedSnapshotLabel)}
                disabled={busy || Boolean(pendingRecovery)}
              >
                Start on selected snapshot
              </button>
            ) : null}
          </div>
        ) : null}
      </header>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4" aria-live="polite" aria-relevant="additions text">
        {!session || session.messages.length === 0 ? (
          <div className="rounded-xl border border-dashed border-ink-700 p-4 text-sm text-limestone-400">
            <p>No messages in this test yet.</p>
            <p className="mt-1 text-xs text-limestone-500">The first message starts at turn 1 and is stored against the pinned snapshot.</p>
          </div>
        ) : (
          <ol aria-label="Committed chat transcript" className="space-y-3">
            {session.messages.map((message) => <TranscriptMessage key={message.id} message={message} />)}
          </ol>
        )}

        {error ? <div role="alert" className="rounded-lg border border-rust-800 bg-rust-950/30 p-3 text-xs leading-5 text-rust-200">{error}</div> : null}
        {pendingRecovery ? (
          <button type="button" className="button-secondary" onClick={() => void handleRecover()} disabled={busy}>
            <RefreshCw className={`h-4 w-4 ${busy ? "animate-spin" : ""}`} />
            {busy ? "Recovering…" : "Recover committed transcript"}
          </button>
        ) : null}

        {session && session.turnSequence > 0 ? (
          <div className="space-y-3 border-t border-ink-800 pt-3">
            <p className="text-xs text-limestone-400">
              Outcome: <span className="font-medium text-limestone-200">{outcomeLabel(session.outcome)}</span>
            </p>
            <details className="rounded-lg border border-ink-800 bg-ink-950/60 p-3">
              <summary className="cursor-pointer text-xs font-medium text-limestone-300">Flow state and trace ({session.trace.length} events)</summary>
              <h3 className="mt-3 text-[11px] font-semibold uppercase tracking-wide text-limestone-500">Flow state</h3>
              <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-md bg-ink-950 p-2 font-mono text-[10px] leading-4 text-limestone-300">{JSON.stringify(session.flowState, null, 2)}</pre>
              <h3 className="mt-3 text-[11px] font-semibold uppercase tracking-wide text-limestone-500">Execution trace</h3>
              {session.trace.length === 0 ? <p className="mt-2 text-xs text-limestone-500">No trace events were returned.</p> : (
                <ol className="mt-2 space-y-2">
                  {session.trace.map((entry, index) => <TraceEntry key={`${entry.turn_sequence}-${entry.event_type}-${index}`} entry={entry} />)}
                </ol>
              )}
            </details>
          </div>
        ) : null}
      </div>

      <form onSubmit={handleSend} className="border-t border-ink-800 bg-ink-950/90 p-3">
        <label className="sr-only" htmlFor="chat-test-message">Message the agent</label>
        <div className="flex items-end gap-2">
          <textarea
            id="chat-test-message"
            className="field-control min-h-12 max-h-36 resize-y text-sm"
            rows={2}
            value={messageText}
            onChange={(event) => setMessageText(event.currentTarget.value)}
            placeholder="Write a test message…"
            disabled={busy || Boolean(pendingRecovery)}
          />
          <button type="submit" className="button-primary min-h-12 shrink-0 px-3" aria-label="Send message" disabled={busy || Boolean(pendingRecovery) || !messageText.trim() || (!session && !selectedSnapshotHash)}>
            {busy ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          </button>
        </div>
        <p className="mt-2 text-[10px] text-limestone-500">No audio is sent or played in Chat Test.</p>
      </form>
    </section>
  );
}

function TranscriptMessage({ message }: { message: ChatMessage }) {
  const isAssistant = message.role === "assistant";
  return (
    <li className={`max-w-[92%] rounded-xl p-3 text-sm leading-6 ${isAssistant ? "mr-auto border border-ink-700 bg-ink-900 text-limestone-200" : "ml-auto border border-copper-800/70 bg-copper-950/30 text-copper-100"}`}>
      <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-limestone-500">
        {isAssistant ? "Agent" : "You"} · turn {message.turn_sequence}
      </p>
      <p className="whitespace-pre-wrap break-words">{message.content}</p>
    </li>
  );
}

function TraceEntry({ entry }: { entry: ChatTraceEntry }) {
  return (
    <li className="rounded-md border border-ink-800 bg-ink-950 p-2 text-[10px] leading-4">
      <p className="font-mono text-copper-200">Turn {entry.turn_sequence} · {entry.event_type} · {entry.outcome}</p>
      <p className="mt-1 text-limestone-400">Step: {entry.step_id ?? "none"} · state v{entry.state_version}</p>
      {entry.diagnostics.length > 0 ? (
        <ul className="mt-1 space-y-1 text-rust-200">
          {entry.diagnostics.map((diagnostic, index) => (
            <li key={`${diagnostic.code}-${index}`}>{diagnostic.severity} {diagnostic.code} at {diagnostic.path || "/"}</li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

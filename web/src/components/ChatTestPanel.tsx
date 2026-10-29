"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
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
  pendingTurnSequence: number | null;
  messages: ChatMessage[];
  flowState: JsonObject;
  trace: ChatTraceEntry[];
  outcome: ChatOutcome;
}

interface PendingRecovery {
  expectedSequence: number;
  text: string;
}

interface StoredChatTest {
  version: 1;
  testId: string;
  snapshotHash: string;
  snapshotLabel: string;
  turnSequence: number;
  pendingRecovery: PendingRecovery | null;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The platform request failed.";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function chatStorageKey(agentId: string): string {
  return `platform-chat-test:${agentId}`;
}

function readStoredChatTest(agentId: string): StoredChatTest | null {
  const raw = window.sessionStorage.getItem(chatStorageKey(agentId));
  if (raw === null) return null;
  const parsed: unknown = JSON.parse(raw);
  if (!isRecord(parsed)) throw new Error("The saved Chat Test session is invalid.");
  const pending = parsed.pendingRecovery;
  const validPending = pending === null || (
    isRecord(pending) &&
    typeof pending.expectedSequence === "number" &&
    Number.isInteger(pending.expectedSequence) &&
    pending.expectedSequence > 0 &&
    typeof pending.text === "string" &&
    pending.text.trim().length > 0
  );
  if (
    parsed.version !== 1 ||
    typeof parsed.testId !== "string" ||
    typeof parsed.snapshotHash !== "string" ||
    typeof parsed.snapshotLabel !== "string" ||
    typeof parsed.turnSequence !== "number" ||
    !Number.isInteger(parsed.turnSequence) ||
    parsed.turnSequence < 0 ||
    !validPending
  ) {
    throw new Error("The saved Chat Test session is invalid. No new turn was sent.");
  }
  return parsed as unknown as StoredChatTest;
}

function persistChatTest(agentId: string, session: ChatSession, pendingRecovery: PendingRecovery | null): void {
  const saved: StoredChatTest = {
    version: 1,
    testId: session.testId,
    snapshotHash: session.snapshotHash,
    snapshotLabel: session.snapshotLabel,
    turnSequence: session.turnSequence,
    pendingRecovery,
  };
  window.sessionStorage.setItem(chatStorageKey(agentId), JSON.stringify(saved));
}

function sessionFromStoredChat(saved: StoredChatTest): ChatSession {
  return {
    testId: saved.testId,
    snapshotHash: saved.snapshotHash,
    snapshotLabel: saved.snapshotLabel,
    turnSequence: saved.turnSequence,
    pendingTurnSequence: null,
    messages: [],
    flowState: {},
    trace: [],
    outcome: "in_progress",
  };
}
function tryPersistChatTest(agentId: string, session: ChatSession, pendingRecovery: PendingRecovery | null): string | null {
  try {
    persistChatTest(agentId, session, pendingRecovery);
    return null;
  } catch (error: unknown) {
    return errorMessage(error);
  }
}

function pendingUserMessage(transcript: ChatTestView, expectedSequence: number): ChatMessage | undefined {
  return transcript.messages.find((message) =>
    message.role === "user" && message.turn_sequence === expectedSequence
  );
}

function isStaleSnapshotError(error: unknown): error is PlatformApiError {
  return error instanceof PlatformApiError &&
    error.status === 409 &&
    (error.message === "The saved draft or prompt policy changed before Chat Test started" ||
      error.message === "Chat Test snapshot does not match the pinned session");
}

function isChatRevisionConflict(error: unknown): error is PlatformApiError {
  return error instanceof PlatformApiError &&
    error.status === 409 &&
    error.code === "REVISION_CONFLICT" &&
    error.message !== "The previous Chat Test turn is still processing";
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
    pendingTurnSequence: null,
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
    pendingTurnSequence: transcript.pending_turn_sequence,
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
  const [restoreLoading, setRestoreLoading] = useState(true);
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

  const applyTranscript = useCallback((transcript: ChatTestView, snapshotLabel: string) => {
    const nextSession = fromTranscript(transcript, snapshotLabel);
    const persistenceError = tryPersistChatTest(agentId, nextSession, null);
    setSession(nextSession);
    setMessageText("");
    setPendingRecovery(null);
    setError(persistenceError
      ? `The transcript was loaded, but local recovery state could not be updated: ${persistenceError}`
      : null);
  }, [agentId]);

  useEffect(() => {
    let current = true;
    setRestoreLoading(true);
    setSession(null);
    setMessageText("");
    setPendingRecovery(null);
    setError(null);
    let saved: StoredChatTest | null;
    try {
      saved = readStoredChatTest(agentId);
    } catch (storageError: unknown) {
      setError(errorMessage(storageError));
      setRestoreLoading(false);
      return () => { current = false; };
    }
    if (!saved) {
      setRestoreLoading(false);
      return () => { current = false; };
    }
    const storedSession = sessionFromStoredChat(saved);
    setSession(storedSession);
    if (saved.pendingRecovery) {
      setPendingRecovery(saved.pendingRecovery);
      setMessageText(saved.pendingRecovery.text);
    }
    platformAgentApi.getChatTest(agentId, saved.testId).then((transcript) => {
      if (!current) return;
      if (transcript.snapshot_hash !== saved.snapshotHash) {
        setError("The saved Chat Test session no longer matches its pinned snapshot. No different snapshot was loaded.");
        return;
      }
      const transcriptSession = fromTranscript(transcript, saved.snapshotLabel);
      const localPending = saved.pendingRecovery;
      if (localPending && transcript.turn_sequence >= localPending.expectedSequence) {
        applyTranscript(transcript, saved.snapshotLabel);
        return;
      }
      if (localPending) {
        const serverPendingMessage = pendingUserMessage(transcript, localPending.expectedSequence);
        const serverHasDifferentPending = transcript.pending_turn_sequence !== null &&
          (transcript.pending_turn_sequence !== localPending.expectedSequence ||
            serverPendingMessage?.content !== localPending.text);
        setSession(transcriptSession);
        setPendingRecovery(localPending);
        setMessageText(localPending.text);
        const persistenceError = tryPersistChatTest(agentId, transcriptSession, localPending);
        setError(serverHasDifferentPending
          ? `The server transcript reports a different pending turn. Your exact saved message was preserved and no replacement was sent.${persistenceError ? ` Local recovery update failed: ${persistenceError}` : ""}`
          : transcript.pending_turn_sequence === localPending.expectedSequence
            ? `The previous turn is still processing. Retry the exact pending message.${persistenceError ? ` Local recovery update failed: ${persistenceError}` : ""}`
            : `The saved turn has not been committed yet. Retry the exact pending message.${persistenceError ? ` Local recovery update failed: ${persistenceError}` : ""}`);
        return;
      }
      if (transcript.pending_turn_sequence !== null) {
        const serverPendingMessage = pendingUserMessage(transcript, transcript.pending_turn_sequence);
        const recoveredPending = serverPendingMessage
          ? { expectedSequence: transcript.pending_turn_sequence, text: serverPendingMessage.content }
          : null;
        setSession(transcriptSession);
        if (recoveredPending) {
          setPendingRecovery(recoveredPending);
          setMessageText(recoveredPending.text);
          const persistenceError = tryPersistChatTest(agentId, transcriptSession, recoveredPending);
          setError(`A Chat Test turn is still processing. Retry the exact pending message.${persistenceError ? ` Local recovery update failed: ${persistenceError}` : ""}`);
        } else {
          setError("The server reports a pending turn but did not return its user message. A new turn is blocked because it cannot be retried safely.");
        }
        return;
      }
      applyTranscript(transcript, saved.snapshotLabel);
    }).catch((transcriptError: unknown) => {
      if (!current) return;
      setSession(storedSession);
      if (saved.pendingRecovery) {
        setPendingRecovery(saved.pendingRecovery);
        setMessageText(saved.pendingRecovery.text);
        setError(`Could not load the saved transcript. Your exact pending message is preserved: ${errorMessage(transcriptError)}`);
      } else if (transcriptError instanceof PlatformApiError && transcriptError.status === 404) {
        setError(null);
      } else {
        setError(`Could not load the saved transcript: ${errorMessage(transcriptError)}`);
      }
    }).finally(() => {
      if (current) setRestoreLoading(false);
    });
    return () => { current = false; };
  }, [agentId, applyTranscript]);

  const keepPendingTurn = (activeSession: ChatSession, pending: PendingRecovery, message: string) => {
    setSession(activeSession);
    setPendingRecovery(pending);
    setMessageText(pending.text);
    const persistenceError = tryPersistChatTest(agentId, activeSession, pending);
    setError(persistenceError ? `${message} Local recovery update failed: ${persistenceError}` : message);
  };

  const clearPendingTurn = (activeSession: ChatSession, pending: PendingRecovery, message: string) => {
    setSession(activeSession);
    setPendingRecovery(null);
    setMessageText(pending.text);
    const persistenceError = tryPersistChatTest(agentId, activeSession, null);
    setError(persistenceError ? `${message} Local recovery update failed: ${persistenceError}` : message);
  };

  const handleSend = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const text = messageText.trim();
    if (!text || busyRef.current || restoreLoading || pendingRecovery || (session && session.pendingTurnSequence !== null)) return;

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

    const pending = { expectedSequence: activeSession.turnSequence + 1, text };
    const persistenceError = tryPersistChatTest(agentId, activeSession, pending);
    if (persistenceError) {
      setError(`The pending turn could not be saved for safe recovery, so it was not sent: ${persistenceError}`);
      return;
    }
    setPendingRecovery(pending);
    setMessageText(text);
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const turn = await platformAgentApi.sendChatTurn(agentId, {
        snapshot_hash: activeSession.snapshotHash,
        test_id: activeSession.testId,
        expected_turn_sequence: pending.expectedSequence,
        text: pending.text,
      });
      const transcript = await platformAgentApi.getChatTest(agentId, turn.test_id);
      if (transcript.snapshot_hash !== activeSession.snapshotHash || transcript.turn_sequence < pending.expectedSequence) {
        throw new Error("The server has not returned the committed turn on the pinned snapshot yet.");
      }
      applyTranscript(transcript, activeSession.snapshotLabel);
    } catch (turnError: unknown) {
      let transcript: ChatTestView | null = null;
      let transcriptError: unknown = null;
      try {
        const loaded = await platformAgentApi.getChatTest(agentId, activeSession.testId);
        if (loaded.snapshot_hash === activeSession.snapshotHash) transcript = loaded;
      } catch (error: unknown) {
        transcriptError = error;
      }
      if (transcript && transcript.turn_sequence >= pending.expectedSequence) {
        applyTranscript(transcript, activeSession.snapshotLabel);
        return;
      }
      const reconciledSession = transcript
        ? fromTranscript(transcript, activeSession.snapshotLabel)
        : activeSession;
      if (isStaleSnapshotError(turnError)) {
        let refreshed = false;
        try {
          refreshed = await onRefreshDraftSnapshot();
        } catch {
          refreshed = false;
        }
        clearPendingTurn(
          { ...reconciledSession, pendingTurnSequence: null },
          pending,
          refreshed
            ? `The pinned snapshot was rejected: ${errorMessage(turnError)}. This conversation remains pinned to its original snapshot. Start a new test on the selected snapshot to continue.`
            : `The pinned snapshot was rejected: ${errorMessage(turnError)}. The latest draft snapshot could not be loaded.${transcriptError ? ` Transcript error: ${errorMessage(transcriptError)}` : ""}`
        );
      } else if (isChatRevisionConflict(turnError)) {
        clearPendingTurn(
          reconciledSession,
          pending,
          `The platform rejected this turn before saving it: ${errorMessage(turnError)}. Reset this test before sending again.`
        );
      } else {
        const pendingMessage = transcript ? pendingUserMessage(transcript, pending.expectedSequence) : undefined;
        const serverPendingSequence = transcript?.pending_turn_sequence ?? null;
        const mismatch = serverPendingSequence !== null &&
          (serverPendingSequence !== pending.expectedSequence || pendingMessage?.content !== pending.text);
        keepPendingTurn(
          reconciledSession,
          pending,
          mismatch
            ? `The server transcript reports a different pending turn. Your exact message was preserved and no replacement was sent. ${errorMessage(turnError)}`
            : `The turn may still be processing. Retry the exact pending message. ${errorMessage(turnError)}${transcriptError ? ` Transcript error: ${errorMessage(transcriptError)}` : ""}`
        );
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const handleRecover = async () => {
    if (!session || !pendingRecovery || !pendingRecovery.text || busyRef.current || restoreLoading) return;
    const activeSession = session;
    const pending = pendingRecovery;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    let observedTranscript: ChatTestView | null = null;
    try {
      try {
        observedTranscript = await platformAgentApi.getChatTest(agentId, activeSession.testId);
      } catch (error: unknown) {
        if (!(error instanceof PlatformApiError && error.status === 404)) throw error;
      }
      if (observedTranscript) {
        if (observedTranscript.snapshot_hash !== activeSession.snapshotHash) {
          throw new Error("The saved test ID now refers to a different pinned snapshot. No message was sent.");
        }
        if (observedTranscript.turn_sequence >= pending.expectedSequence) {
          applyTranscript(observedTranscript, activeSession.snapshotLabel);
          return;
        }
        const serverPendingMessage = pendingUserMessage(observedTranscript, pending.expectedSequence);
        if (observedTranscript.pending_turn_sequence !== null &&
          (observedTranscript.pending_turn_sequence !== pending.expectedSequence || serverPendingMessage?.content !== pending.text)) {
          keepPendingTurn(
            fromTranscript(observedTranscript, activeSession.snapshotLabel),
            pending,
            "The server transcript reports a different pending turn. Your exact message was preserved and no replacement was sent."
          );
          return;
        }
        const latestSession = fromTranscript(observedTranscript, activeSession.snapshotLabel);
        setSession(latestSession);
        const storageError = tryPersistChatTest(agentId, latestSession, pending);
        if (storageError) {
          setError(`The pending turn could not be saved for safe retry: ${storageError}`);
          return;
        }
      }
      await platformAgentApi.sendChatTurn(agentId, {
        snapshot_hash: activeSession.snapshotHash,
        test_id: activeSession.testId,
        expected_turn_sequence: pending.expectedSequence,
        text: pending.text,
      });
      const transcript = await platformAgentApi.getChatTest(agentId, activeSession.testId);
      if (transcript.snapshot_hash !== activeSession.snapshotHash || transcript.turn_sequence < pending.expectedSequence) {
        throw new Error("The server has not returned the committed turn on the pinned snapshot yet.");
      }
      applyTranscript(transcript, activeSession.snapshotLabel);
    } catch (recoveryError: unknown) {
      if (isStaleSnapshotError(recoveryError)) {
        let refreshed = false;
        try {
          refreshed = await onRefreshDraftSnapshot();
        } catch {
          refreshed = false;
        }
        const staleSession = observedTranscript?.snapshot_hash === activeSession.snapshotHash
          ? fromTranscript(observedTranscript, activeSession.snapshotLabel)
          : activeSession;
        clearPendingTurn(
          { ...staleSession, pendingTurnSequence: null },
          pending,
          refreshed
            ? `The pinned snapshot was rejected: ${errorMessage(recoveryError)}. This conversation remains pinned to its original snapshot. Start a new test on the selected snapshot to continue.`
            : `The pinned snapshot was rejected: ${errorMessage(recoveryError)}. The latest draft snapshot could not be loaded.`
        );
      } else {
        let transcript: ChatTestView | null = observedTranscript;
        try {
          const latest = await platformAgentApi.getChatTest(agentId, activeSession.testId);
          if (latest.snapshot_hash === activeSession.snapshotHash) transcript = latest;
        } catch {
          // Keep the original request and retry identity when the transcript cannot be read.
        }
        if (transcript && transcript.turn_sequence >= pending.expectedSequence) {
          applyTranscript(transcript, activeSession.snapshotLabel);
        } else if (transcript && transcript.pending_turn_sequence !== null) {
          const pendingMessage = pendingUserMessage(transcript, pending.expectedSequence);
          const mismatch = transcript.pending_turn_sequence !== pending.expectedSequence || pendingMessage?.content !== pending.text;
          keepPendingTurn(
            fromTranscript(transcript, activeSession.snapshotLabel),
            pending,
            mismatch
              ? `The server transcript reports a different pending turn. Your exact message was preserved and no replacement was sent. ${errorMessage(recoveryError)}`
              : `The previous turn is still processing. Retry the exact pending message. ${errorMessage(recoveryError)}`
          );
        } else if (isChatRevisionConflict(recoveryError)) {
          clearPendingTurn(
            transcript ? fromTranscript(transcript, activeSession.snapshotLabel) : activeSession,
            pending,
            `The platform rejected this turn before saving it: ${errorMessage(recoveryError)}. Reset this test before sending again.`
          );
        } else {
          keepPendingTurn(
            transcript ? fromTranscript(transcript, activeSession.snapshotLabel) : activeSession,
            pending,
            `Could not resume the exact pending turn. It remains available to retry: ${errorMessage(recoveryError)}`
          );
        }
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const handleReset = (snapshotHash: string, snapshotLabel: string) => {
    if (busyRef.current || pendingRecovery || (session && session.pendingTurnSequence !== null)) return;
    try {
      const nextSession = createSession(snapshotHash, snapshotLabel);
      const persistenceError = tryPersistChatTest(agentId, nextSession, null);
      setSession(nextSession);
      setMessageText("");
      setPendingRecovery(null);
      setError(persistenceError
        ? `The new test was not saved for recovery: ${persistenceError}`
        : null);
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
            disabled={busy || restoreLoading || Boolean(pendingRecovery)}
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
              disabled={busy || restoreLoading || Boolean(pendingRecovery) || session.pendingTurnSequence !== null}
            >
              <RotateCcw className="h-4 w-4" />Reset on pinned snapshot
            </button>
            {selectedSnapshotHash && !selectedIsCurrent ? (
              <button
                type="button"
                className="button-secondary"
                onClick={() => handleReset(selectedSnapshotHash, selectedSnapshotLabel)}
                disabled={busy || restoreLoading || Boolean(pendingRecovery) || session.pendingTurnSequence !== null}
              >
                Start on selected snapshot
              </button>
            ) : null}
          </div>
        ) : null}
        {restoreLoading ? <p role="status" className="text-xs text-limestone-400">Restoring saved Chat Test…</p> : null}
      </header>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4" aria-live="polite" aria-relevant="additions text">
        {!session || session.messages.length === 0 ? (
          <div className="rounded-xl border border-dashed border-ink-700 p-4 text-sm text-limestone-400">
            <p>No messages in this test yet.</p>
            <p className="mt-1 text-xs text-limestone-500">The first message starts at turn 1 and is stored against the pinned snapshot.</p>
          </div>
        ) : (
          <ol aria-label="Chat transcript" className="space-y-3">
            {session.messages.map((message) => (
              <TranscriptMessage
                key={message.id}
                message={message}
                pending={message.role === "user" && session.pendingTurnSequence === message.turn_sequence}
              />
            ))}
          </ol>
        )}

        {error ? <div role="alert" className="rounded-lg border border-rust-800 bg-rust-950/30 p-3 text-xs leading-5 text-rust-200">{error}</div> : null}
        {pendingRecovery ? (
          <button type="button" className="button-secondary" onClick={() => void handleRecover()} disabled={busy || restoreLoading || !pendingRecovery.text}>
            <RefreshCw className={`h-4 w-4 ${busy ? "animate-spin" : ""}`} />
            {busy ? "Retrying…" : "Retry pending turn"}
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
            disabled={busy || restoreLoading || Boolean(pendingRecovery) || Boolean(session && session.pendingTurnSequence !== null)}
          />
          <button type="submit" className="button-primary min-h-12 shrink-0 px-3" aria-label="Send message" disabled={busy || restoreLoading || Boolean(pendingRecovery) || Boolean(session && session.pendingTurnSequence !== null) || !messageText.trim() || (!session && !selectedSnapshotHash)}>
            {busy ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          </button>
        </div>
        <p className="mt-2 text-[10px] text-limestone-500">No audio is sent or played in Chat Test.</p>
      </form>
    </section>
  );
}

function TranscriptMessage({ message, pending }: { message: ChatMessage; pending: boolean }) {
  const isAssistant = message.role === "assistant";
  return (
    <li className={`max-w-[92%] rounded-xl p-3 text-sm leading-6 ${isAssistant ? "mr-auto border border-ink-700 bg-ink-900 text-limestone-200" : "ml-auto border border-copper-800/70 bg-copper-950/30 text-copper-100"}`}>
      <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-limestone-500">
        {isAssistant ? "Agent" : "You"} · turn {message.turn_sequence}{pending ? " · pending" : ""}
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
            <li key={`${diagnostic.code}-${index}`}>
              {diagnostic.severity} {diagnostic.code} at {diagnostic.path || "/"}
              {diagnostic.message ? `: ${diagnostic.message}` : ""}
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

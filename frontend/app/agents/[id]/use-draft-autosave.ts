"use client";

import { useEffect, useMemo, useRef } from "react";

import { ApiError, api, apiErrorText } from "@/lib/api";
import type { Agent } from "@/lib/types";

import { agentDraftFromAgent, draftChanges } from "./agent-sections";
import type { AgentDraft } from "./agent-sections";
import type { CallFlowSaveState } from "./call-flow-save";

const SAVE_IDLE_MS = 1200;
const SAVE_MAX_MS = 10000;
const SAVE_COALESCE_MS = 250;
const SAVE_RETRY_MS = [2000, 10000, 30000];
const SAVED_FADE_MS = 2500;

/** Only network failures, 429s and server errors are worth a timed auto-retry. */
function isRetryableSaveError(reason: unknown): boolean {
  return (
    reason instanceof ApiError &&
    (reason.status === 0 || reason.status === 429 || reason.status >= 500)
  );
}

function saveErrorMessage(reason: unknown): string {
  if (reason instanceof ApiError) return apiErrorText(reason, "Try again.");
  if (reason instanceof Error && reason.message) return reason.message;
  return "Try again.";
}

/** Normalized payload used for dirty checks so trimming cannot cause save loops. */
function payloadSignature(draft: AgentDraft): string {
  return JSON.stringify(draftChanges(draft));
}

/**
 * Auto-saves the agent draft with the same machinery as the visual flow
 * editor: 1.2 s idle debounce, 10 s max flush, single-flight saves with a
 * ~250 ms coalescing pass, no auto-retry for 4xx (except 429), and a
 * 2 s / 10 s / 30 s backoff for transient failures. The local draft is never
 * rewritten from a save response; only the server snapshot is updated.
 */
export function useDraftAutosave(options: {
  agentId: string;
  agent: Agent | null;
  draft: AgentDraft | null;
  onServerAgent: (agent: Agent) => void;
  onState: (state: CallFlowSaveState) => void;
}): { flush: () => void; dirty: boolean } {
  const { agent, draft } = options;

  // Latest options for timer and async continuations, so callback identity
  // churn can never retrigger a save and continuations never go stale.
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const dirty = useMemo(
    () => Boolean(agent && draft && payloadSignature(draft) !== payloadSignature(agentDraftFromAgent(agent))),
    [agent, draft],
  );

  const prevDraftRef = useRef<AgentDraft | null>(null);
  const dirtyRef = useRef(false);
  const editCounterRef = useRef(0);
  const savingRef = useRef(false);
  const idleTimerRef = useRef(0);
  const maxTimerRef = useRef(0);
  const retryTimerRef = useRef(0);
  const retryAttemptRef = useRef(0);
  const savedTimerRef = useRef(0);
  const mountedRef = useRef(true);
  const beforeUnloadRef = useRef<(event: BeforeUnloadEvent) => void>((event) => {
    if (!dirtyRef.current) return;
    event.preventDefault();
    event.returnValue = "";
  });

  function clearTimer(ref: { current: number }) {
    if (ref.current) {
      window.clearTimeout(ref.current);
      ref.current = 0;
    }
  }

  function cancelTimers() {
    clearTimer(idleTimerRef);
    clearTimer(maxTimerRef);
    clearTimer(retryTimerRef);
    clearTimer(savedTimerRef);
  }

  function guardUnload() {
    window.addEventListener("beforeunload", beforeUnloadRef.current);
  }

  function unguardUnload() {
    window.removeEventListener("beforeunload", beforeUnloadRef.current);
  }

  function scheduleIdleSave() {
    clearTimer(idleTimerRef);
    idleTimerRef.current = window.setTimeout(() => {
      idleTimerRef.current = 0;
      void runSave();
    }, SAVE_IDLE_MS);
    if (!maxTimerRef.current) {
      maxTimerRef.current = window.setTimeout(() => {
        maxTimerRef.current = 0;
        void runSave();
      }, SAVE_MAX_MS);
    }
  }

  function scheduleNextPass() {
    clearTimer(idleTimerRef);
    idleTimerRef.current = window.setTimeout(() => {
      idleTimerRef.current = 0;
      void runSave();
    }, SAVE_COALESCE_MS);
  }

  function showSaved() {
    clearTimer(savedTimerRef);
    optionsRef.current.onState({ status: "saved" });
    savedTimerRef.current = window.setTimeout(() => {
      savedTimerRef.current = 0;
      if (mountedRef.current && !dirtyRef.current && !savingRef.current) {
        optionsRef.current.onState({ status: "idle" });
      }
    }, SAVED_FADE_MS);
  }

  // Single-flight save: clears pending timers, then PATCHes the normalized
  // draft diff. Edits made mid-save are carried into the next pass, and
  // transient failures back off (2 s / 10 s / 30 s) while edits remain.
  async function runSave() {
    clearTimer(idleTimerRef);
    clearTimer(maxTimerRef);
    clearTimer(retryTimerRef);
    if (savingRef.current || !dirtyRef.current) return;
    clearTimer(savedTimerRef);
    const { agentId, agent: serverAgent, draft: currentDraft } = optionsRef.current;
    if (!serverAgent || !currentDraft) return;
    if (payloadSignature(currentDraft) === payloadSignature(agentDraftFromAgent(serverAgent))) {
      // The edit round-tripped back to the server value: nothing to send.
      dirtyRef.current = false;
      unguardUnload();
      if (mountedRef.current) optionsRef.current.onState({ status: "idle" });
      return;
    }
    const captured = editCounterRef.current;
    savingRef.current = true;
    optionsRef.current.onState({ status: "saving" });
    let autoRetryBlocked = false;
    try {
      const updated = await api<Agent>(`/agents/${agentId}`, {
        method: "PATCH",
        body: JSON.stringify(draftChanges(currentDraft)),
      });
      if (!mountedRef.current || optionsRef.current.agentId !== agentId) return;
      retryAttemptRef.current = 0;
      optionsRef.current.onServerAgent(updated);
      if (editCounterRef.current === captured) {
        dirtyRef.current = false;
        unguardUnload();
        showSaved();
      } else {
        scheduleNextPass();
      }
    } catch (reason) {
      if (!mountedRef.current || optionsRef.current.agentId !== agentId) return;
      autoRetryBlocked = !isRetryableSaveError(reason);
      optionsRef.current.onState({ status: "error", message: saveErrorMessage(reason) });
      if (!autoRetryBlocked && dirtyRef.current) {
        const attempt = retryAttemptRef.current;
        const delay = SAVE_RETRY_MS[Math.min(attempt, SAVE_RETRY_MS.length - 1)];
        retryAttemptRef.current = attempt + 1;
        retryTimerRef.current = window.setTimeout(() => {
          retryTimerRef.current = 0;
          void runSave();
        }, delay);
      }
    } finally {
      savingRef.current = false;
      if (
        mountedRef.current &&
        dirtyRef.current &&
        !autoRetryBlocked &&
        !idleTimerRef.current &&
        !maxTimerRef.current &&
        !retryTimerRef.current
      ) {
        scheduleIdleSave();
      }
    }
  }

  /** Saves the pending draft immediately; the header chip flushes through this. */
  function flush() {
    retryAttemptRef.current = 0;
    void runSave();
  }

  // Pending edits when the page unmounts (navigation before the debounce
  // fires): hand the draft to the API fire-and-forget. Refs only — no state
  // updates and no parent sync from this path; errors are swallowed.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cancelTimers();
      unguardUnload();
      if (!dirtyRef.current || savingRef.current) return;
      dirtyRef.current = false;
      const { agentId, agent: serverAgent, draft: currentDraft } = optionsRef.current;
      if (!currentDraft) return;
      if (serverAgent && payloadSignature(currentDraft) === payloadSignature(agentDraftFromAgent(serverAgent))) return;
      void api<Agent>(`/agents/${agentId}`, {
        method: "PATCH",
        body: JSON.stringify(draftChanges(currentDraft)),
      }).catch(() => {});
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A new draft object identity from patchDraft is a user edit: mark dirty
  // and schedule the debounced save. The page never rewrites the draft from a
  // save response, so identity changes are either edits or load resets.
  useEffect(() => {
    const previous = prevDraftRef.current;
    prevDraftRef.current = draft;
    if (previous === draft) return; // initial model (and StrictMode replay)
    if (!draft) {
      // The page is loading or switching agents: drop pending bookkeeping.
      dirtyRef.current = false;
      cancelTimers();
      unguardUnload();
      return;
    }
    if (!previous) return; // the freshly loaded draft is not an edit
    dirtyRef.current = true;
    editCounterRef.current += 1;
    guardUnload();
    if (!savingRef.current) scheduleIdleSave();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);

  return { flush, dirty };
}

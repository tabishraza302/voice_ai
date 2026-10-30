"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { RefreshCw } from "lucide-react";
import { AdminGate } from "@/components/AdminGate";
import { useAuth } from "@/lib/auth-state";
import { PlatformApiError, platformApi } from "@/lib/platform-api-client";
import { formatMinorCurrency, formatPlatformDate } from "@/lib/platform-format";
import type { AdminSessionDetail, AdminSessionItem, AdminSessionPage, SessionKind, TranscriptPageView } from "@/types/platform";

const TERMINABLE_STATES: Record<string, true> = {
  REGISTERED: true,
  CONNECTING: true,
  INITIALIZING: true,
  READY: true,
  ACTIVE: true,
  RECONNECTING: true,
};

function AdminSessionsContent() {
  const { executeWithStepUp } = useAuth();
  const [page, setPage] = useState<AdminSessionPage | null>(null);
  const [sessions, setSessions] = useState<AdminSessionItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AdminSessionDetail | null>(null);
  const [transcript, setTranscript] = useState<TranscriptPageView | null>(null);
  const [ownerId, setOwnerId] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [kindFilter, setKindFilter] = useState("");
  const [appliedFilters, setAppliedFilters] = useState({ ownerId: "", state: "", kind: "" });
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [accessReason, setAccessReason] = useState("");
  const [accessLoading, setAccessLoading] = useState(false);
  const [transcriptCursor, setTranscriptCursor] = useState<string | null>(null);
  const [correctionEventId, setCorrectionEventId] = useState("");
  const [replacementText, setReplacementText] = useState("");
  const [correctionReason, setCorrectionReason] = useState("");
  const [visibilityReason, setVisibilityReason] = useState("");
  const [terminationReason, setTerminationReason] = useState("");
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const loadSessions = useCallback(async (targetCursor: string | null = null, append = false) => {
    setLoading(!append);
    setLoadingMore(append);
    setError(null);
    try {
      const result = await platformApi.adminGetSessions({
        cursor: targetCursor,
        limit: 25,
        owner_user_id: appliedFilters.ownerId || undefined,
        state: appliedFilters.state || undefined,
        kind: (appliedFilters.kind as SessionKind) || undefined,
      });
      setPage(result);
      setSessions((current) => append ? [...current, ...result.items] : result.items);
      setCursor(result.next_cursor);
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not load admin session inventory.");
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [appliedFilters]);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  const loadDetail = useCallback(async (sessionId: string) => {
    setDetailLoading(true);
    setDetailError(null);
    try {
      const result = await platformApi.adminGetSession(sessionId);
      setDetail(result);
    } catch (err: unknown) {
      setDetailError(err instanceof PlatformApiError ? err.message : "Could not load this session.");
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    setTranscript(null);
    setTranscriptCursor(null);
    setSuccessMessage(null);
    setActionError(null);
    if (selectedId) void loadDetail(selectedId);
    else setDetail(null);
  }, [loadDetail, selectedId]);

  const applyFilters = (event: FormEvent) => {
    event.preventDefault();
    setAppliedFilters({
      ownerId: ownerId.trim(),
      state: stateFilter.trim(),
      kind: kindFilter,
    });
  };

  const requestTranscript = async (event: FormEvent) => {
    event.preventDefault();
    if (!selectedId || accessLoading) return;
    const reason = accessReason.trim();
    if (reason.length < 10 || reason.length > 500) {
      setActionError("Transcript access reason must contain 10–500 characters.");
      return;
    }
    setAccessLoading(true);
    setActionError(null);
    setSuccessMessage(null);
    try {
      const result = await executeWithStepUp(() => platformApi.adminAccessTranscript(
        selectedId,
        { reason, limit: 100 },
        crypto.randomUUID()
      ));
      setTranscript(result);
      setTranscriptCursor(result.next_cursor);
      setSuccessMessage("Transcript access recorded with your support reason.");
    } catch (err: unknown) {
      setActionError(err instanceof PlatformApiError ? err.message : "Could not open the transcript.");
    } finally {
      setAccessLoading(false);
    }
  };

  const loadMoreTranscript = async () => {
    if (!selectedId || !transcriptCursor || accessLoading) return;
    const reason = accessReason.trim();
    if (reason.length < 10) {
      setActionError("Enter a support reason before opening another transcript page.");
      return;
    }
    setAccessLoading(true);
    setActionError(null);
    try {
      const result = await executeWithStepUp(() => platformApi.adminAccessTranscript(
        selectedId,
        { reason, cursor: transcriptCursor, limit: 100 },
        crypto.randomUUID()
      ));
      setTranscript((current) => current ? {
        ...result,
        segments: [...current.segments, ...result.segments],
        gaps: [...current.gaps, ...result.gaps],
      } : result);
      setTranscriptCursor(result.next_cursor);
      setSuccessMessage("Transcript page access recorded.");
    } catch (err: unknown) {
      setActionError(err instanceof PlatformApiError ? err.message : "Could not load another transcript page.");
    } finally {
      setAccessLoading(false);
    }
  };

  const submitCorrection = async (event: FormEvent) => {
    event.preventDefault();
    if (!selectedId || !transcript || actionLoading) return;
    const reason = correctionReason.trim();
    if (!correctionEventId || !replacementText.trim() || reason.length < 10 || reason.length > 500) {
      setActionError("Choose a transcript event, provide replacement text, and enter a reason of 10–500 characters.");
      return;
    }
    setActionLoading(true);
    setActionError(null);
    setSuccessMessage(null);
    try {
      const result = await executeWithStepUp(() => platformApi.adminCreateTranscriptCorrection(
        selectedId,
        {
          event_id: correctionEventId,
          expected_revision: transcript.policy_revision,
          replacement_text: replacementText,
          reason,
        },
        crypto.randomUUID()
      ));
      setTranscript((current) => current ? {
        ...current,
        policy_revision: result.policy_revision,
        segments: current.segments.map((segment) => segment.event_id === result.event_id
          ? { ...segment, final_text: replacementText, corrected: true }
          : segment),
      } : current);
      setCorrectionReason("");
      setReplacementText("");
      setSuccessMessage("Transcript correction saved. The original event was retained.");
    } catch (err: unknown) {
      setActionError(err instanceof PlatformApiError ? err.message : "Could not save this transcript correction.");
    } finally {
      setActionLoading(false);
    }
  };

  const setTranscriptVisibility = async (visible: boolean) => {
    if (!selectedId || !transcript || actionLoading) return;
    const reason = visibilityReason.trim();
    if (reason.length < 10 || reason.length > 500) {
      setActionError("Visibility change reason must contain 10–500 characters.");
      return;
    }
    setActionLoading(true);
    setActionError(null);
    setSuccessMessage(null);
    try {
      const result = await executeWithStepUp(() => platformApi.adminSetTranscriptVisibility(
        selectedId,
        {
          visible_to_owner: visible,
          expected_revision: transcript.policy_revision,
          reason,
        },
        crypto.randomUUID()
      ));
      setTranscript((current) => current ? {
        ...current,
        policy_revision: result.policy_revision,
        visible_to_owner: result.visible_to_owner,
      } : current);
      setVisibilityReason("");
      setSuccessMessage(`Transcript visibility for the owner is now ${result.visible_to_owner ? "on" : "off"}.`);
    } catch (err: unknown) {
      setActionError(err instanceof PlatformApiError ? err.message : "Could not change transcript visibility.");
    } finally {
      setActionLoading(false);
    }
  };

  const terminateSession = async (event: FormEvent) => {
    event.preventDefault();
    if (!selectedId || !detail || actionLoading) return;
    const reason = terminationReason.trim();
    if (reason.length < 10 || reason.length > 500) {
      setActionError("Termination reason must contain 10–500 characters.");
      return;
    }
    setActionLoading(true);
    setActionError(null);
    setSuccessMessage(null);
    try {
      const result = await executeWithStepUp(() => platformApi.adminTerminateSession(
        selectedId,
        reason,
        crypto.randomUUID()
      ));
      setTerminationReason("");
      setSuccessMessage(result.state === "ENDING"
        ? "Stop requested. The session will finish after runner release acknowledgement."
        : `Session is already in state ${result.state}.`);
      await loadDetail(selectedId);
      await loadSessions();
    } catch (err: unknown) {
      setActionError(err instanceof PlatformApiError ? err.message : "Could not request session termination.");
    } finally {
      setActionLoading(false);
    }
  };

  const selectedSession = sessions.find((session) => session.session_id === selectedId) ?? null;

  return (
    <div className="space-y-8">
      <header className="border-b border-ink-800 pb-6">
        <p className="text-sm font-medium text-copper-300">Administration</p>
        <h1 className="mt-1 text-3xl font-semibold tracking-tight text-limestone-100">Sessions and transcripts</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">
          Inspect pinned call facts, open transcripts only with an audited support reason, and request emergency stops for live calls.
        </p>
      </header>

      {error ? <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{error}</div> : null}
      {actionError ? <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{actionError}</div> : null}
      {successMessage ? <div role="status" className="surface border-moss-800 bg-moss-950/30 p-4 text-sm text-moss-200">{successMessage}</div> : null}

      <form onSubmit={applyFilters} className="surface grid gap-3 p-4 sm:grid-cols-4">
        <label className="text-xs text-limestone-400">Owner user ID<input className="input mt-1 w-full" value={ownerId} onChange={(event) => setOwnerId(event.target.value)} placeholder="UUID" /></label>
        <label className="text-xs text-limestone-400">State<input className="input mt-1 w-full" value={stateFilter} onChange={(event) => setStateFilter(event.target.value)} placeholder="ACTIVE" /></label>
        <label className="text-xs text-limestone-400">Kind<select className="input mt-1 w-full" value={kindFilter} onChange={(event) => setKindFilter(event.target.value)}><option value="">All types</option><option value="voice">Voice</option><option value="chat">Chat</option><option value="voice_test">Voice test</option></select></label>
        <div className="flex items-end gap-2"><button className="button-primary" type="submit">Filter</button><button className="button-secondary" type="button" onClick={() => void loadSessions()} disabled={loading} aria-label="Refresh sessions"><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} /></button></div>
      </form>

      <div className="grid gap-6 xl:grid-cols-[minmax(19rem,0.8fr)_minmax(0,1.2fr)]">
        <section aria-labelledby="session-list-title" className="space-y-3">
          <h2 id="session-list-title" className="text-lg font-semibold text-limestone-100">Session inventory</h2>
          <div className="surface divide-y divide-ink-800">
            {loading && sessions.length === 0 ? <p role="status" className="p-4 text-sm text-limestone-400">Loading sessions…</p> : null}
            {sessions.map((session) => (
              <button key={session.session_id} type="button" onClick={() => setSelectedId(session.session_id)} aria-pressed={selectedId === session.session_id} className={`block w-full p-4 text-left text-sm hover:bg-ink-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-copper-400 ${selectedId === session.session_id ? "bg-ink-900" : ""}`}>
                <span className="flex items-start justify-between gap-3"><span className="min-w-0"><span className="block truncate font-medium text-limestone-100">{session.kind.replace("_", " ")} · {session.state}</span><span className="mt-1 block text-xs text-limestone-400">Owner {session.owner_user_id}</span></span><span className="rounded-full border border-ink-700 px-2 py-1 text-[11px] text-limestone-300">{session.runner_id ?? "No runner"}</span></span>
                <span className="mt-2 block text-xs text-limestone-500">{formatPlatformDate(session.created_at)} · {session.session_id}</span>
              </button>
            ))}
            {!loading && sessions.length === 0 ? <p className="p-4 text-sm text-limestone-400">No sessions match these filters.</p> : null}
          </div>
          {cursor ? <button className="button-secondary w-full" onClick={() => void loadSessions(cursor, true)} disabled={loadingMore}>{loadingMore ? "Loading…" : "Load more sessions"}</button> : null}
          {page && !page.next_cursor && sessions.length > 0 ? <p className="text-xs text-limestone-500">End of session results.</p> : null}
        </section>

        <section aria-labelledby="session-detail-title" className="min-w-0 space-y-4">
          <h2 id="session-detail-title" className="text-lg font-semibold text-limestone-100">Pinned session details</h2>
          {!selectedId ? <div className="surface p-5 text-sm text-limestone-400">Select a session to inspect its stored configuration and billing facts.</div> : null}
          {detailError ? <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{detailError}</div> : null}
          {detailLoading ? <div role="status" className="surface p-5 text-sm text-limestone-400">Loading pinned session data…</div> : null}
          {detail ? (
            <div className="space-y-4">
              <article className="surface p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div><h3 className="text-lg font-semibold text-limestone-100">{detail.kind.replace("_", " ")} · {detail.state}</h3><p className="mt-1 break-all font-mono text-xs text-limestone-500">{detail.session_id}</p></div>
                  <span className="rounded-full border border-ink-700 px-2 py-1 text-xs text-limestone-300">Version {detail.session_version}</span>
                </div>
                <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
                  <div><dt className="text-xs text-limestone-500">Owner</dt><dd className="mt-1 break-all text-limestone-200">{detail.owner_user_id}</dd></div>
                  <div><dt className="text-xs text-limestone-500">Runner</dt><dd className="mt-1 text-limestone-200">{detail.runner_id ?? "—"}</dd></div>
                  <div><dt className="text-xs text-limestone-500">Created</dt><dd className="mt-1 text-limestone-200">{formatPlatformDate(detail.created_at)}</dd></div>
                  <div><dt className="text-xs text-limestone-500">Ended</dt><dd className="mt-1 text-limestone-200">{formatPlatformDate(detail.ended_at)}</dd></div>
                  <div><dt className="text-xs text-limestone-500">Pinned rate</dt><dd className="mt-1 text-limestone-200">{detail.tariff_snapshot && typeof detail.tariff_snapshot.rate_minor_per_minute === "number" ? `${formatMinorCurrency(detail.tariff_snapshot.rate_minor_per_minute)} / minute` : "No tariff snapshot"}</dd></div>
                  <div><dt className="text-xs text-limestone-500">Transcript visibility</dt><dd className="mt-1 text-limestone-200">{detail.transcript_visible_to_owner ? "Visible to owner" : "Hidden from owner"} · policy {detail.transcript_policy_revision}</dd></div>
                </dl>
                {detail.billing ? <div className="mt-4 rounded-lg border border-ink-800 bg-ink-950/60 p-3 text-sm"><p className="font-medium text-limestone-200">{detail.billing.charge_status === "provisional" ? "Provisional usage" : "Final usage"}</p><p className="mt-1 text-limestone-400">{formatMinorCurrency(detail.billing.charged_minor)} charged · {formatMinorCurrency(detail.billing.reserved_minor)} reserved · {detail.billing.billed_ms} ms billed</p></div> : <p className="mt-4 text-sm text-limestone-500">No billing record is attached to this session.</p>}
                {detail.pinned_config_snapshot ? <details className="mt-4"><summary className="cursor-pointer text-sm text-copper-200">View exact pinned agent configuration</summary><pre className="mt-3 max-h-80 overflow-auto rounded-lg border border-ink-800 bg-ink-950 p-3 text-xs text-limestone-300">{JSON.stringify(detail.pinned_config_snapshot, null, 2)}</pre></details> : <p className="mt-4 text-sm text-limestone-500">No published agent snapshot is attached.</p>}
                {detail.tariff_snapshot ? <details className="mt-3"><summary className="cursor-pointer text-sm text-copper-200">View pinned tariff snapshot</summary><pre className="mt-3 max-h-56 overflow-auto rounded-lg border border-ink-800 bg-ink-950 p-3 text-xs text-limestone-300">{JSON.stringify(detail.tariff_snapshot, null, 2)}</pre></details> : null}
              </article>

              {selectedSession && TERMINABLE_STATES[selectedSession.state] ? (
                <form onSubmit={terminateSession} className="surface space-y-3 border-rust-900/70 p-4">
                  <div><h3 className="font-medium text-limestone-100">Emergency call stop</h3><p className="mt-1 text-xs text-limestone-400">Requests an audited stop; the runner release acknowledgement finishes termination.</p></div>
                  <label className="block text-xs text-limestone-400">Reason<textarea className="input mt-1 min-h-20 w-full" value={terminationReason} onChange={(event) => setTerminationReason(event.target.value)} maxLength={500} /></label>
                  <button className="button-danger" type="submit" disabled={actionLoading}>{actionLoading ? "Submitting…" : "Request emergency stop"}</button>
                </form>
              ) : null}

              <section aria-labelledby="transcript-access-title" className="surface space-y-4 p-4">
                <div><h3 id="transcript-access-title" className="font-medium text-limestone-100">Audited transcript access</h3><p className="mt-1 text-xs text-limestone-400">Enter the support reason before the transcript is returned. Each page access is recorded.</p></div>
                <form onSubmit={requestTranscript} className="flex flex-col gap-3 sm:flex-row sm:items-end">
                  <label className="flex-1 text-xs text-limestone-400">Support reason<textarea className="input mt-1 min-h-20 w-full" value={accessReason} onChange={(event) => setAccessReason(event.target.value)} maxLength={500} /></label>
                  <button className="button-secondary" type="submit" disabled={accessLoading}>{accessLoading ? "Opening…" : "Open transcript"}</button>
                </form>
                {transcript ? (
                  <div className="space-y-4 border-t border-ink-800 pt-4">
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-limestone-500"><span>Transcript revision {transcript.transcript_revision} · policy revision {transcript.policy_revision}</span><span>{transcript.visible_to_owner ? "Owner-visible" : "Hidden from owner"}</span></div>
                    {transcript.segments.length ? <ol className="max-h-[32rem] divide-y divide-ink-800 overflow-auto rounded-lg border border-ink-800">{transcript.segments.map((segment) => <li key={segment.event_id} className="p-3"><div className="flex flex-wrap justify-between gap-2 text-xs text-limestone-500"><span className="capitalize">{segment.speaker}{segment.corrected ? " · corrected" : ""}</span><span>{segment.start_ms}–{segment.end_ms} ms · {segment.event_id}</span></div><p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-limestone-200">{segment.final_text}</p></li>)}</ol> : <p className="text-sm text-limestone-400">No transcript segments are available for this session.</p>}
                    {transcript.has_gaps ? <p className="text-xs text-copper-200">Transcript reports missing events.</p> : null}
                    {transcriptCursor ? <button className="button-secondary" onClick={() => void loadMoreTranscript()} disabled={accessLoading}>Load next transcript page</button> : null}

                    {transcript.segments.length ? (
                      <form onSubmit={submitCorrection} className="space-y-3 border-t border-ink-800 pt-4">
                        <h4 className="text-sm font-medium text-limestone-100">Correct a transcript segment</h4>
                        <label className="block text-xs text-limestone-400">Transcript event<select className="input mt-1 w-full" value={correctionEventId} onChange={(event) => setCorrectionEventId(event.target.value)}><option value="">Choose an event</option>{transcript.segments.map((segment) => <option key={segment.event_id} value={segment.event_id}>{segment.speaker} · {segment.event_id} · {segment.final_text.slice(0, 80)}</option>)}</select></label>
                        <label className="block text-xs text-limestone-400">Corrected text<textarea className="input mt-1 min-h-24 w-full" value={replacementText} onChange={(event) => setReplacementText(event.target.value)} maxLength={16384} /></label>
                        <label className="block text-xs text-limestone-400">Correction reason<textarea className="input mt-1 min-h-20 w-full" value={correctionReason} onChange={(event) => setCorrectionReason(event.target.value)} maxLength={500} /></label>
                        <button className="button-secondary" type="submit" disabled={actionLoading}>{actionLoading ? "Saving…" : "Save correction"}</button>
                      </form>
                    ) : null}

                    <div className="space-y-3 border-t border-ink-800 pt-4">
                      <h4 className="text-sm font-medium text-limestone-100">Owner transcript visibility</h4>
                      <label className="block text-xs text-limestone-400">Reason<textarea className="input mt-1 min-h-20 w-full" value={visibilityReason} onChange={(event) => setVisibilityReason(event.target.value)} maxLength={500} /></label>
                      <div className="flex flex-wrap gap-2"><button className="button-secondary" type="button" onClick={() => void setTranscriptVisibility(true)} disabled={actionLoading || transcript.visible_to_owner}>Allow owner to see transcript</button><button className="button-danger" type="button" onClick={() => void setTranscriptVisibility(false)} disabled={actionLoading || !transcript.visible_to_owner}>Hide transcript from owner</button></div>
                    </div>
                  </div>
                ) : null}
              </section>
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}

export default function AdminSessionsPage() {
  return <AdminGate><AdminSessionsContent /></AdminGate>;
}

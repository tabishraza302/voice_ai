"use client";

import Link from "next/link";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ArrowRight, RefreshCw } from "lucide-react";
import { PlatformApiError, platformApi } from "@/lib/platform-api-client";
import { formatDurationMs, formatPlatformDate } from "@/lib/platform-format";
import type { ConfigSnapshotView, OwnerSessionsPage, SessionHistoryItem, TranscriptPageView } from "@/types/platform";

function sessionLabel(session: SessionHistoryItem): string {
  return session.kind.replace("_", " ");
}

export default function ConversationsPage() {
  return (
    <Suspense fallback={<div role="status" className="p-6 text-sm text-limestone-400">Loading call history…</div>}>
      <ConversationsPageContent />
    </Suspense>
  );
}

function ConversationsPageContent() {
  const searchParams = useSearchParams();
  const selectedSessionId = searchParams.get("session");
  const [page, setPage] = useState<OwnerSessionsPage | null>(null);
  const [sessions, setSessions] = useState<SessionHistoryItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [stateFilter, setStateFilter] = useState("");
  const [kindFilter, setKindFilter] = useState("");
  const [activeFilters, setActiveFilters] = useState({ state: "", kind: "" });
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [config, setConfig] = useState<ConfigSnapshotView | null>(null);
  const [transcript, setTranscript] = useState<TranscriptPageView | null>(null);
  const [transcriptCursor, setTranscriptCursor] = useState<string | null>(null);
  const [transcriptLoadingMore, setTranscriptLoadingMore] = useState(false);
  const sessionsRequestRef = useRef(0);

  const loadSessions = useCallback(async (targetCursor: string | null = null, append = false) => {
    const requestId = ++sessionsRequestRef.current;
    setLoading(!append);
    setLoadingMore(append);
    setError(null);
    if (!append) {
      setSessions([]);
      setPage(null);
      setCursor(null);
    }
    try {
      const result = await platformApi.getOwnerSessions({
        cursor: targetCursor,
        limit: 25,
        state: activeFilters.state || undefined,
        kind: activeFilters.kind
          ? (activeFilters.kind as "voice" | "chat" | "voice_test")
          : undefined,
      });
      if (requestId !== sessionsRequestRef.current) return;
      setPage(result);
      setSessions((current) => append ? [...current, ...result.items] : result.items);
      setCursor(result.next_cursor);
    } catch (err: unknown) {
      if (requestId === sessionsRequestRef.current) {
        setError(err instanceof PlatformApiError ? err.message : "Could not load call history.");
      }
    } finally {
      if (requestId === sessionsRequestRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, [activeFilters]);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    if (!selectedSessionId) {
      setConfig(null);
      setTranscript(null);
      setTranscriptCursor(null);
      setDetailError(null);
      return;
    }
    let current = true;
    setDetailLoading(true);
    setDetailError(null);
    setConfig(null);
    setTranscript(null);
    setTranscriptCursor(null);
    Promise.all([
      platformApi.getOwnerConfigSnapshot(selectedSessionId),
      platformApi.getOwnerTranscript(selectedSessionId, { limit: 100 }),
    ]).then(([configSnapshot, transcriptPage]) => {
      if (!current) return;
      setConfig(configSnapshot);
      setTranscript(transcriptPage);
      setTranscriptCursor(transcriptPage.next_cursor);
    }).catch((err: unknown) => {
      if (current) setDetailError(err instanceof PlatformApiError ? err.message : "Could not load this call.");
    }).finally(() => {
      if (current) setDetailLoading(false);
    });
    return () => { current = false; };
  }, [selectedSessionId]);

  const loadMoreTranscript = async () => {
    if (!selectedSessionId || !transcriptCursor || transcriptLoadingMore) return;
    setTranscriptLoadingMore(true);
    setDetailError(null);
    try {
      const nextPage = await platformApi.getOwnerTranscript(selectedSessionId, {
        cursor: transcriptCursor,
        limit: 100,
      });
      setTranscript((current) => current ? {
        ...nextPage,
        segments: [...current.segments, ...nextPage.segments],
        gaps: [...current.gaps, ...nextPage.gaps],
      } : nextPage);
      setTranscriptCursor(nextPage.next_cursor);
    } catch (err: unknown) {
      setDetailError(err instanceof PlatformApiError ? err.message : "Could not load more transcript events.");
    } finally {
      setTranscriptLoadingMore(false);
    }
  };

  const selectedSession = sessions.find((session) => session.session_id === selectedSessionId) ?? null;

  return (
    <div className="space-y-8">
      <header className="border-b border-ink-800 pb-6">
        <p className="text-sm font-medium text-copper-300">Your account</p>
        <h1 className="mt-1 text-3xl font-semibold tracking-tight text-limestone-100">Call history</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">
          Review your calls, the agent version and tariff pinned to each call, and any transcript available to you.
        </p>
      </header>

      {error ? <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{error}</div> : null}

      <div className="grid gap-6 xl:grid-cols-[minmax(18rem,0.8fr)_minmax(0,1.2fr)]">
        <section aria-labelledby="history-list-title" className="space-y-3">
          <div className="flex items-center justify-between gap-3">
            <h2 id="history-list-title" className="text-lg font-semibold text-limestone-100">Conversations</h2>
            <button className="button-secondary" onClick={() => void loadSessions()} disabled={loading} aria-label="Refresh call history">
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />Refresh
            </button>
          </div>
          <form
            className="surface flex flex-col gap-3 p-4 sm:flex-row"
            onSubmit={(event) => {
              event.preventDefault();
              setActiveFilters({ state: stateFilter.trim(), kind: kindFilter });
            }}
          >
            <label className="flex-1 text-xs text-limestone-400">
              State
              <input className="input mt-1 w-full" value={stateFilter} onChange={(event) => setStateFilter(event.target.value)} placeholder="e.g. ENDED" />
            </label>
            <label className="flex-1 text-xs text-limestone-400">
              Type
              <select className="input mt-1 w-full" value={kindFilter} onChange={(event) => setKindFilter(event.target.value)}>
                <option value="">All types</option>
                <option value="voice">Voice</option>
                <option value="chat">Chat</option>
                <option value="voice_test">Voice test</option>
              </select>
            </label>
            <button className="button-primary self-end" type="submit">Filter</button>
          </form>
          <div className="surface divide-y divide-ink-800">
            {loading && sessions.length === 0 ? <p role="status" className="p-4 text-sm text-limestone-400">Loading call history…</p> : null}
            {sessions.map((session) => {
              const selected = selectedSessionId === session.session_id;
              return (
                <Link
                  key={session.session_id}
                  href={`/conversations?session=${encodeURIComponent(session.session_id)}`}
                  aria-current={selected ? "page" : undefined}
                  className={`block p-4 text-sm transition-colors hover:bg-ink-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-copper-400 ${selected ? "bg-ink-900" : ""}`}
                >
                  <span className="flex items-start justify-between gap-3">
                    <span className="min-w-0">
                      <span className="block truncate font-medium capitalize text-limestone-100">{sessionLabel(session)}</span>
                      <span className="mt-1 block text-xs text-limestone-400">{session.state}</span>
                    </span>
                    <ArrowRight className="mt-1 h-4 w-4 shrink-0 text-limestone-500" />
                  </span>
                  <span className="mt-2 block text-xs text-limestone-500">{formatPlatformDate(session.created_at)}</span>
                  <span className="mt-1 block truncate font-mono text-[11px] text-limestone-600">{session.session_id}</span>
                </Link>
              );
            })}
            {!loading && !error && sessions.length === 0 ? <p className="p-4 text-sm text-limestone-400">No conversations match these filters.</p> : null}
            {error && sessions.length === 0 ? <p className="p-4 text-sm text-rust-200">Call history is unavailable until the request succeeds.</p> : null}
          </div>
          {cursor ? <button className="button-secondary w-full" onClick={() => void loadSessions(cursor, true)} disabled={loadingMore}>{loadingMore ? "Loading…" : "Load more calls"}</button> : null}
          {page?.next_cursor === null && sessions.length > 0 ? <p className="text-xs text-limestone-500">End of available call history.</p> : null}
        </section>

        <section aria-labelledby="conversation-detail-title" className="min-w-0 space-y-4">
          <div>
            <h2 id="conversation-detail-title" className="text-lg font-semibold text-limestone-100">Call details</h2>
            <p className="mt-1 text-sm text-limestone-400">Choose a conversation to see its stored snapshot and transcript.</p>
          </div>
          {!selectedSessionId ? (
            <div className="surface p-6 text-sm text-limestone-400">Select a call from your history.</div>
          ) : detailError ? (
            <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{detailError}</div>
          ) : detailLoading ? (
            <div role="status" className="surface p-6 text-sm text-limestone-400">Loading pinned call details…</div>
          ) : (
            <div className="space-y-4">
              {selectedSession ? (
                <article className="surface p-4">
                  <h3 className="font-medium capitalize text-limestone-100">{sessionLabel(selectedSession)} · {selectedSession.state}</h3>
                  <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2">
                    <div><dt className="text-xs text-limestone-500">Started</dt><dd className="mt-1 text-limestone-200">{formatPlatformDate(selectedSession.created_at)}</dd></div>
                    <div><dt className="text-xs text-limestone-500">Ended</dt><dd className="mt-1 text-limestone-200">{formatPlatformDate(selectedSession.ended_at)}</dd></div>
                    <div><dt className="text-xs text-limestone-500">Session version</dt><dd className="mt-1 text-limestone-200">{selectedSession.session_version}</dd></div>
                    <div><dt className="text-xs text-limestone-500">Failure code</dt><dd className="mt-1 text-limestone-200">{selectedSession.failure_code ?? "—"}</dd></div>
                  </dl>
                </article>
              ) : null}

              {config ? (
                <article className="surface p-4">
                  <h3 className="font-medium text-limestone-100">Pinned agent and tariff</h3>
                  <p className="mt-1 text-sm text-limestone-300">{config.agent_name} · version {config.agent_version}</p>
                  <p className="mt-1 break-all font-mono text-xs text-limestone-500">{config.agent_version_id}</p>
                  <div className="mt-3 grid gap-2 text-xs text-limestone-400 sm:grid-cols-2">
                    <p>Flow hash: <code className="break-all text-limestone-300">{config.flow_hash}</code></p>
                    <p>Artifact hash: <code className="break-all text-limestone-300">{config.artifact_hash}</code></p>
                    <p>Authoring hash: <code className="break-all text-limestone-300">{config.authoring_hash}</code></p>
                    <p>Published: {formatPlatformDate(config.published_at)}</p>
                  </div>
                  <details className="mt-4">
                    <summary className="cursor-pointer text-sm text-copper-200">View pinned configuration</summary>
                    <pre className="mt-3 max-h-96 overflow-auto rounded-lg border border-ink-800 bg-ink-950 p-3 text-xs text-limestone-300">{JSON.stringify({ immutable_config: config.immutable_config, tariff_snapshot: config.tariff_snapshot }, null, 2)}</pre>
                  </details>
                </article>
              ) : null}

              {transcript ? (
                <article className="surface">
                  <div className="border-b border-ink-800 p-4">
                    <h3 className="font-medium text-limestone-100">Transcript</h3>
                    <p className="mt-1 text-xs text-limestone-500">
                      Revision {transcript.transcript_revision} · policy revision {transcript.policy_revision}
                    </p>
                  </div>
                  {!transcript.visible_to_owner ? (
                    <p className="p-4 text-sm text-limestone-400">This transcript is currently hidden from the account owner.</p>
                  ) : transcript.segments.length ? (
                    <ol className="divide-y divide-ink-800">
                      {transcript.segments.map((segment) => (
                        <li key={segment.event_id} className="p-4">
                          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-limestone-500">
                            <span className="capitalize">{segment.speaker}{segment.corrected ? " · corrected" : ""}</span>
                            <span>{formatDurationMs(segment.start_ms)}–{formatDurationMs(segment.end_ms)}</span>
                          </div>
                          <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-limestone-200">{segment.final_text}</p>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <p className="p-4 text-sm text-limestone-400">No transcript segments are available for this call.</p>
                  )}
                  {transcript.has_gaps ? <p className="border-t border-ink-800 p-3 text-xs text-copper-200">Some transcript events are missing; the transcript reports gaps.</p> : null}
                  {transcriptCursor ? <button className="button-secondary m-4" onClick={() => void loadMoreTranscript()} disabled={transcriptLoadingMore}>{transcriptLoadingMore ? "Loading…" : "Load more transcript"}</button> : null}
                </article>
              ) : null}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

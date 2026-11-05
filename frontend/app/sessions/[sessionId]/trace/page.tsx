"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "@/lib/api";
import type { CallSession, CurrentUser, SessionTrace } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { OwnerShell } from "@/app/dashboard/owner-shell";

function formatTime(value: string | null): string {
  if (!value) return "Not recorded";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(new Date(value));
}

function formatDuration(value: number | null): string {
  if (value === null) return "Not measured";
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(2)} s`;
}

export default function SessionTracePage() {
  const params = useParams<{ sessionId: string }>();
  const router = useRouter();
  const sessionId = params.sessionId;
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [session, setSession] = useState<CallSession | null>(null);
  const [trace, setTrace] = useState<SessionTrace | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [traceError, setTraceError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [retrying, setRetrying] = useState(false);

  const loadTrace = useCallback(async () => {
    setRetrying(true);
    setTraceError(null);
    try {
      const result = await api<SessionTrace>(`/sessions/${sessionId}/trace`);
      setTrace(result);
    } catch (reason) {
      const error = reason instanceof ApiError ? reason : new ApiError(0, "Trace data could not be loaded.");
      setTrace(null);
      if (error.status === 401) router.replace("/login");
      else setTraceError(error);
    } finally {
      setRetrying(false);
    }
  }, [router, sessionId]);

  useEffect(() => {
    let current = true;
    async function load() {
      setLoading(true);
      setSessionError(null);
      setTraceError(null);
      setTrace(null);
      try {
        const currentUser = await api<CurrentUser>("/auth/me");
        if (!current) return;
        if (currentUser.role === "admin") {
          router.replace("/admin");
          return;
        }
        const call = await api<CallSession>(`/sessions/${sessionId}`);
        if (!current) return;
        setUser(currentUser);
        setSession(call);
        try {
          const result = await api<SessionTrace>(`/sessions/${sessionId}/trace`);
          if (current) setTrace(result);
        } catch (reason) {
          if (!current) return;
          const error = reason instanceof ApiError ? reason : new ApiError(0, "Trace data could not be loaded.");
          if (error.status === 401) router.replace("/login");
          else setTraceError(error);
        }
      } catch (reason) {
        if (!current) return;
        if (reason instanceof ApiError && reason.status === 401) {
          router.replace("/login");
          return;
        }
        const error = reason instanceof ApiError ? reason : new ApiError(0, "The session could not be loaded.");
        setSessionError(error.status === 404 ? "This session is unavailable." : error.message);
      } finally {
        if (current) setLoading(false);
      }
    }
    void load();
    return () => { current = false; };
  }, [router, sessionId]);

  return (
    <OwnerShell active="agents" email={user?.email}>
      <main className="mx-auto max-w-5xl px-4 py-8 sm:px-6 sm:py-10 lg:px-8">
        {session ? (
          <Link href={`/agents/${session.agent_id}?tab=history`} className="rounded-xl text-sm font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">
            Back to {session.call_type === "test" ? "test" : "call"} history
          </Link>
        ) : (
          <Link href="/dashboard" className="rounded-xl text-sm font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">
            Back to dashboard
          </Link>
        )}
        <div className="mt-7 space-y-2">
          <h1 className="text-3xl font-semibold tracking-tight">Trace details</h1>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">Only timing and error type fields for this session are shown.</p>
        </div>

        {loading ? (
          <Card className="mt-7" aria-busy="true">
            <CardContent className="py-8"><p role="status" className="text-sm text-muted-foreground">Loading session diagnostics...</p></CardContent>
          </Card>
        ) : sessionError ? (
          <Card className="mt-7">
            <CardContent className="space-y-3 py-6">
              <p role="alert" className="text-sm text-destructive">{sessionError}</p>
              <Link href="/dashboard" className="text-sm font-medium text-primary underline-offset-4 hover:underline">Return to dashboard</Link>
            </CardContent>
          </Card>
        ) : session ? (
          <>
            <Card className="mt-7">
              <CardHeader className="flex-row items-start justify-between gap-4">
                <div>
                  <CardTitle>Session</CardTitle>
                  <p className="mt-1 break-all text-sm text-muted-foreground">{session.id}</p>
                </div>
                <Badge variant={session.status === "completed" ? "success" : session.status === "failed" ? "danger" : "muted"}>
                  {session.status}
                </Badge>
              </CardHeader>
              <CardContent className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                <div><p className="text-xs font-medium text-muted-foreground">Call type</p><p className="mt-1 text-sm">{session.call_type}</p></div>
                <div><p className="text-xs font-medium text-muted-foreground">Started</p><p className="mt-1 text-sm">{formatTime(session.started_at)}</p></div>
                <div><p className="text-xs font-medium text-muted-foreground">Ended</p><p className="mt-1 text-sm">{formatTime(session.ended_at)}</p></div>
              </CardContent>
            </Card>

            {trace ? (
              <section className="mt-9" aria-labelledby="span-heading">
                <div className="flex flex-wrap items-end justify-between gap-3 border-b pb-4">
                  <div>
                    <h2 id="span-heading" className="text-xl font-semibold tracking-tight">Measured spans</h2>
                    <p className="mt-1 text-sm text-muted-foreground">{trace.spans.length} span{trace.spans.length === 1 ? "" : "s"} recorded</p>
                  </div>
                  <Button variant="outline" size="sm" onClick={() => void loadTrace()} disabled={retrying}>
                    {retrying ? "Refreshing..." : "Refresh trace"}
                  </Button>
                </div>
                {trace.spans.length === 0 ? (
                  <p className="py-8 text-sm text-muted-foreground">No trace spans are available for this session.</p>
                ) : (
                  <ul className="divide-y">
                    {trace.spans.map((span, index) => (
                      <li key={`${span.name}-${span.started_at ?? "none"}-${index}`} className="grid grid-cols-1 gap-3 py-5 md:grid-cols-[minmax(0,1fr)_11rem_9rem] md:items-start">
                        <div className="min-w-0">
                          <h3 className="break-words text-sm font-semibold">{span.name}</h3>
                          <p className="mt-1 text-sm text-muted-foreground">{span.status}</p>
                          {span.error_type ? <p className="mt-2 text-sm text-destructive">Error type: {span.error_type}</p> : null}
                        </div>
                        <div className="space-y-1 text-sm text-muted-foreground">
                          <p>Started {formatTime(span.started_at)}</p>
                          <p>Ended {formatTime(span.ended_at)}</p>
                        </div>
                        <p className="text-sm font-medium tabular-nums">{formatDuration(span.duration_ms)}</p>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            ) : (
              <Card className="mt-7">
                <CardContent className="space-y-4 py-6">
                  <div>
                    <h2 className="font-semibold">Trace data is unavailable</h2>
                    <p role="status" className="mt-1 text-sm leading-6 text-muted-foreground">
                      {traceError?.status === 503
                        ? "Phoenix is not available right now. Retry when the trace service is ready."
                        : traceError?.message ?? "The trace could not be loaded."}
                    </p>
                    {traceError && traceError.status > 0 ? <p className="mt-2 text-xs text-muted-foreground">HTTP {traceError.status}</p> : null}
                  </div>
                  <Button variant="outline" onClick={() => void loadTrace()} disabled={retrying}>
                    {retrying ? "Retrying..." : "Retry trace"}
                  </Button>
                </CardContent>
              </Card>
            )}
          </>
        ) : null}
      </main>
    </OwnerShell>
  );
}

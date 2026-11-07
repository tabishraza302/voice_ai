"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { CallPanel } from "@/components/call-panel";
import { ApiError, api, apiErrorText } from "@/lib/api";
import type { CallAnswer, CallMessage, CallSession, CallSessionGrant } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

export function AgentTesting({
  view,
  agentId,
  agentName,
  languages,
  defaultLanguage,
}: {
  view: "testing" | "history";
  agentId: string;
  agentName: string;
  languages: string[];
  defaultLanguage: string | null;
}) {
  const router = useRouter();
  const [language, setLanguage] = useState(defaultLanguage ?? languages[0] ?? "");
  const [sessions, setSessions] = useState<CallSession[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [latestSessionId, setLatestSessionId] = useState<string | null>(null);
  const [session, setSession] = useState<CallSession | null>(null);
  const [messages, setMessages] = useState<CallMessage[]>([]);
  const [answers, setAnswers] = useState<CallAnswer[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [callError, setCallError] = useState<string | null>(null);

  const loadSessions = useCallback(async () => {
    setLoadingHistory(true);
    setHistoryError(null);
    try {
      const result = await api<CallSession[]>(`/agents/${agentId}/sessions`);
      setSessions(result);
      setSelectedId((current) => current ?? result[0]?.id ?? null);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        router.replace("/login");
        return;
      }
      setHistoryError(apiErrorText(reason, "Could not load session history."));
    } finally {
      setLoadingHistory(false);
    }
  }, [agentId, router]);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    if (!selectedId) {
      setSession(null);
      setMessages([]);
      setAnswers([]);
      setDetailError(null);
      return;
    }
    const controller = new AbortController();
    let current = true;
    setLoadingDetail(true);
    setDetailError(null);
    Promise.all([
      api<CallSession>(`/sessions/${selectedId}`, { signal: controller.signal }),
      api<CallMessage[]>(`/sessions/${selectedId}/messages`, { signal: controller.signal }),
      api<CallAnswer[]>(`/sessions/${selectedId}/answers`, { signal: controller.signal }),
    ]).then(([call, callMessages, callAnswers]) => {
      if (!current) return;
      setSession(call);
      setMessages(callMessages);
      setAnswers(callAnswers);
    }).catch((reason: unknown) => {
      if (!current || controller.signal.aborted) return;
      if (reason instanceof ApiError && reason.status === 401) {
        router.replace("/login");
        return;
      }
      setSession(null);
      setMessages([]);
      setAnswers([]);
      setDetailError(apiErrorText(reason, "Could not load this session."));
    }).finally(() => {
      if (current) setLoadingDetail(false);
    });
    return () => {
      current = false;
      controller.abort();
    };
  }, [router, selectedId]);

  async function createCall(): Promise<CallSessionGrant> {
    setCallError(null);
    try {
      const grant = await api<CallSessionGrant>(`/agents/${agentId}/test-calls`, {
        method: "POST",
        body: JSON.stringify(language ? { language } : {}),
      });
      setLatestSessionId(grant.session_id);
      setSelectedId(grant.session_id);
      void loadSessions();
      return grant;
    } catch (reason) {
      setCallError(apiErrorText(reason, "Could not start a test call."));
      throw reason;
    }
  }

  function callEnded(sessionId: string) {
    setLatestSessionId(sessionId);
    setSelectedId(sessionId);
    void loadSessions();
  }

  return (
    <div className="space-y-8">
      {view === "testing" ? (
      <section className="space-y-4" aria-labelledby="test-call-heading">
        <div>
          <h3 id="test-call-heading" className="text-lg font-semibold">Test call</h3>
          <p className="mt-1 text-sm leading-6 text-muted-foreground">Start a real test session using the agent's saved provider configuration.</p>
        </div>
        {languages.length === 0 ? (
          <div className="rounded-xl border border-dashed p-4 text-sm leading-6 text-muted-foreground">
            Choose a supported language in the Languages tab before starting a test call.
          </div>
        ) : (
          <div className="max-w-sm space-y-1.5">
            <label htmlFor="test-language" className="text-sm font-medium">Call language</label>
            <Select id="test-language" value={language} onChange={(event) => setLanguage(event.currentTarget.value)}>
              {languages.map((item) => <option key={item} value={item}>{item === "en" ? "English" : item === "hi" ? "Hindi" : item}</option>)}
            </Select>
          </div>
        )}
        {callError ? <p role="alert" className="rounded-xl border border-destructive/30 bg-destructive/10 px-3.5 py-3 text-sm text-destructive">{callError}</p> : null}
        {latestSessionId ? (
          <div className="flex flex-wrap items-center gap-3 rounded-xl border bg-muted/40 p-3 text-sm">
            <span className="font-medium">Latest test session</span>
            <span className="break-all text-muted-foreground">{latestSessionId}</span>
            <Link href={`/sessions/${latestSessionId}/trace`} className="rounded-xl font-semibold text-primary underline-offset-4 hover:underline">View scoped trace</Link>
          </div>
        ) : null}
        {languages.length > 0 ? <CallPanel createCall={createCall} title={`Test ${agentName}`} onEnded={callEnded} /> : null}
      </section>
      ) : null}

      {view === "history" ? (
      <section className="space-y-4 border-t pt-7" aria-labelledby="history-heading">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h3 id="history-heading" className="text-lg font-semibold">Session history</h3>
            <p className="mt-1 text-sm text-muted-foreground">Test and public call activity for this agent.</p>
          </div>
          <Button variant="outline" size="sm" onClick={() => void loadSessions()} disabled={loadingHistory}>
            {loadingHistory ? "Refreshing..." : "Refresh history"}
          </Button>
        </div>
        {historyError ? <p role="alert" className="text-sm text-destructive">{historyError}</p> : null}
        {loadingHistory && sessions.length === 0 ? (
          <p role="status" className="rounded-xl border bg-card p-4 text-sm text-muted-foreground">Loading sessions...</p>
        ) : sessions.length === 0 ? (
          <p className="rounded-xl border border-dashed p-5 text-sm leading-6 text-muted-foreground">No sessions are recorded for this agent yet.</p>
        ) : (
          <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(14rem,0.7fr)_minmax(0,1.3fr)]">
            <ul className="space-y-2" aria-label="Agent sessions">
              {sessions.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    className={`w-full rounded-xl border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${selectedId === item.id ? "border-primary bg-primary/5" : "bg-card hover:bg-muted/60"}`}
                    aria-current={selectedId === item.id ? "true" : undefined}
                    onClick={() => setSelectedId(item.id)}
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className="text-sm font-semibold">{item.call_type === "chat_test" ? "Chat test" : item.call_type === "test" ? "Test call" : "Public call"}</span>
                      <Badge variant={item.status === "completed" ? "success" : item.status === "failed" ? "danger" : item.status === "active" ? "warning" : "muted"}>{item.status}</Badge>
                    </span>
                    <span className="mt-2 block text-xs text-muted-foreground">{formatDate(item.created_at)}</span>
                    <span className="mt-1 block break-all text-xs text-muted-foreground">{item.id}</span>
                  </button>
                </li>
              ))}
            </ul>

            <Card aria-busy={loadingDetail}>
              <CardHeader className="flex-row items-start justify-between gap-4">
                <div>
                  <CardTitle>Session details</CardTitle>
                  {session ? <p className="mt-1 break-all text-sm text-muted-foreground">{session.id}</p> : null}
                </div>
                {session ? <Badge variant={session.status === "completed" ? "success" : session.status === "failed" ? "danger" : session.status === "active" ? "warning" : "muted"}>{session.status}</Badge> : null}
              </CardHeader>
              <CardContent className="space-y-6">
                {loadingDetail ? <p role="status" className="text-sm text-muted-foreground">Loading session details...</p> : null}
                {detailError ? <p role="alert" className="text-sm text-destructive">{detailError}</p> : null}
                {session ? (
                  <>
                    <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
                      <div><dt className="text-xs text-muted-foreground">Created</dt><dd className="mt-1">{formatDate(session.created_at)}</dd></div>
                      <div><dt className="text-xs text-muted-foreground">Outcome</dt><dd className="mt-1">{session.outcome ?? "Not set"}</dd></div>
                      <div><dt className="text-xs text-muted-foreground">Started</dt><dd className="mt-1">{session.started_at ? formatDate(session.started_at) : "Not started"}</dd></div>
                      <div><dt className="text-xs text-muted-foreground">Ended</dt><dd className="mt-1">{session.ended_at ? formatDate(session.ended_at) : "Not ended"}</dd></div>
                    </dl>
                    <div className="flex flex-wrap gap-3 border-t pt-4">
                      <Link href={`/sessions/${session.id}/trace`} className="text-sm font-semibold text-primary underline-offset-4 hover:underline">View scoped trace details</Link>
                    </div>
                    <section aria-labelledby="transcript-heading">
                      <h4 id="transcript-heading" className="font-semibold">Transcript</h4>
                      {messages.length === 0 ? <p className="mt-2 text-sm text-muted-foreground">No transcript messages have been saved.</p> : (
                        <ol className="mt-3 max-h-96 space-y-3 overflow-y-auto rounded-xl border bg-muted/30 p-3" aria-label="Session transcript">
                          {messages.map((message) => (
                            <li key={message.id} className="rounded-xl bg-card p-3 text-sm">
                              <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{message.role === "assistant" ? "Agent" : "Caller"}</p>
                              <p className="whitespace-pre-wrap break-words leading-6">{message.content}</p>
                            </li>
                          ))}
                        </ol>
                      )}
                    </section>
                    <section aria-labelledby="answers-heading">
                      <h4 id="answers-heading" className="font-semibold">Collected answers</h4>
                      {answers.length === 0 ? <p className="mt-2 text-sm text-muted-foreground">No answers have been saved.</p> : (
                        <ul className="mt-3 divide-y rounded-xl border">
                          {answers.map((answer) => (
                            <li key={answer.id} className="space-y-2 p-3">
                              <div className="flex flex-wrap items-center justify-between gap-2">
                                <span className="text-sm font-medium">{answer.field_key}</span>
                                <Badge variant={answer.accepted ? "success" : "warning"}>{answer.accepted ? "Accepted" : "Not accepted"}</Badge>
                              </div>
                              <p className="break-words text-sm">{typeof answer.value === "string" ? answer.value : JSON.stringify(answer.value)}</p>
                              <p className="break-words text-xs text-muted-foreground">Validation: {JSON.stringify(answer.validation_result)}</p>
                            </li>
                          ))}
                        </ul>
                      )}
                    </section>
                  </>
                ) : !loadingDetail && !detailError ? <p className="text-sm text-muted-foreground">Select a session to view its transcript and answers.</p> : null}
              </CardContent>
            </Card>
          </div>
        )}
      </section>
      ) : null}
    </div>
  );
}

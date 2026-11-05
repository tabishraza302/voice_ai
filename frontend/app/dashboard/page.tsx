"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, api, apiErrorText } from "@/lib/api";
import type { Agent, CallSession, CurrentUser } from "@/lib/types";
import { Button, buttonClassName } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { OwnerShell } from "./owner-shell";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

interface AgentActivity {
  agent: Agent;
  sessions: CallSession[];
}


function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(value));
}

export default function DashboardPage() {
  const router = useRouter();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [activity, setActivity] = useState<AgentActivity[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [agentToDelete, setAgentToDelete] = useState<Agent | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function deleteAgent() {
    if (!agentToDelete) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await api<void>(`/agents/${agentToDelete.id}`, { method: "DELETE" });
      setAgentToDelete(null);
      setReload((val) => val + 1);
    } catch (reason) {
      setDeleteError(apiErrorText(reason, "Could not delete this agent. Active calls prevent deletion."));
    } finally {
      setDeleting(false);
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    let current = true;

    async function load() {
      setLoading(true);
      setError(null);
      try {
        const currentUser = await api<CurrentUser>("/auth/me", { signal: controller.signal });
        if (!current) return;
        if (currentUser.role === "admin") {
          router.replace("/admin");
          return;
        }
        const agents = await api<Agent[]>("/agents", { signal: controller.signal });
        const agentSessions = await Promise.all(
          agents.map(async (agent) => ({
            agent,
            sessions: await api<CallSession[]>(`/agents/${agent.id}/sessions`, { signal: controller.signal }),
          })),
        );
        if (current) {
          setUser(currentUser);
          setActivity(agentSessions);
        }
      } catch (reason) {
        if (!current || controller.signal.aborted) return;
        if (reason instanceof ApiError && reason.status === 401) {
          router.replace("/login");
          return;
        }
        setError(reason instanceof ApiError ? reason.message : "Could not load your workspace. Try again.");
      } finally {
        if (current) setLoading(false);
      }
    }

    void load();
    return () => {
      current = false;
      controller.abort();
    };
  }, [reload, router]);

  const filtered = useMemo(() => {
    const search = query.trim().toLocaleLowerCase();
    return activity.filter(({ agent }) => {
      const searchable = agent.name.toLocaleLowerCase();
      return !search || searchable.includes(search);
    });
  }, [activity, query]);

  const totalSessions = activity.reduce((total, item) => total + item.sessions.length, 0);

  return (
    <OwnerShell active="dashboard" email={user?.email}>
      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6 sm:py-10 lg:px-8">
        <div className="flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between">
          <div className="space-y-2">
            <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Your voice agents</h1>
            <p className="max-w-2xl text-sm leading-6 text-muted-foreground">
              Manage your agents, test a call, and review session activity.
            </p>
          </div>
          <Link href="/agents/new" className={buttonClassName("primary", "default", "whitespace-nowrap")}>
            Create agent
          </Link>
        </div>

        {loading ? (
          <section className="mt-8 space-y-5" aria-label="Loading workspace" aria-busy="true">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {["agents", "sessions"].map((item) => (
                <div key={item} className="h-24 animate-pulse rounded-2xl border bg-card" />
              ))}
            </div>
            <div className="h-64 animate-pulse rounded-2xl border bg-card" />
          </section>
        ) : error ? (
          <Card className="mt-8 p-5 sm:p-6">
            <div role="alert" className="space-y-3">
              <h2 className="font-semibold">Workspace could not load</h2>
              <p className="text-sm text-muted-foreground">{error}</p>
              <Button variant="outline" onClick={() => setReload((value) => value + 1)}>Try again</Button>
            </div>
          </Card>
        ) : user ? (
          <>
            <section className="mt-8 grid grid-cols-1 gap-px overflow-hidden rounded-2xl border bg-border sm:grid-cols-2" aria-label="Workspace counts">
              <div className="bg-card p-5 sm:p-6">
                <p className="text-sm text-muted-foreground">Agents</p>
                <p className="mt-2 text-3xl font-semibold tabular-nums">{activity.length}</p>
              </div>
              <div className="bg-card p-5 sm:p-6">
                <p className="text-sm text-muted-foreground">Sessions</p>
                <p className="mt-2 text-3xl font-semibold tabular-nums">{totalSessions}</p>
              </div>
            </section>

            <section id="agents" className="mt-10 scroll-mt-24">
              <div className="flex flex-col gap-4 border-b pb-5 sm:flex-row sm:items-end sm:justify-between">
                <div>
                  <h2 className="text-xl font-semibold tracking-tight">Agents</h2>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {filtered.length} of {activity.length} agents
                  </p>
                </div>
                <div className="w-full max-w-xs space-y-1.5">
                  <Label htmlFor="agent-search">Search agents</Label>
                  <Input id="agent-search" type="search" value={query} onChange={(event) => setQuery(event.currentTarget.value)} />
                </div>
              </div>

              {activity.length === 0 ? (
                <div className="py-14 text-center">
                  <h3 className="text-lg font-semibold">No agents yet</h3>
                  <p className="mx-auto mt-2 max-w-md text-sm leading-6 text-muted-foreground">
                    Create a draft to start configuring its voice, questions, and call flow.
                  </p>
                  <Link href="/agents/new" className={buttonClassName("primary", "default", "mt-5")}>
                    Create your first agent
                  </Link>
                </div>
              ) : filtered.length === 0 ? (
                <div className="py-14 text-center">
                  <h3 className="text-lg font-semibold">No matching agents</h3>
                  <p className="mt-2 text-sm text-muted-foreground">Try a different search term.</p>
                  <Button className="mt-4" variant="outline" onClick={() => setQuery("")}>
                    Clear search
                  </Button>
                </div>
              ) : (
                <ul className="mt-2 divide-y">
                  {filtered.map(({ agent, sessions }) => {
                    const latest = sessions[0]?.created_at ?? null;
                    const activeCalls = sessions.filter((session) => session.status === "created" || session.status === "active").length;
                    return (
                      <li key={agent.id}>
                        <article className="grid grid-cols-1 gap-3 py-5 sm:grid-cols-[minmax(0,1fr)_12rem_auto] sm:items-center sm:gap-6">
                          <div className="min-w-0">
                            <Link href={`/agents/${agent.id}`} className="rounded-sm text-base font-semibold tracking-tight underline-offset-4 hover:text-primary hover:underline">
                              {agent.name}
                            </Link>
                            {activeCalls > 0 ? <p className="mt-1 text-xs text-muted-foreground">{activeCalls} active call{activeCalls === 1 ? "" : "s"}</p> : null}
                          </div>
                          <div className="min-w-0 text-sm">
                            <p className="font-medium tabular-nums text-foreground">{sessions.length} session{sessions.length === 1 ? "" : "s"}</p>
                            <p className="mt-1 text-xs text-muted-foreground">{latest ? `Last activity ${formatDate(latest)}` : "No sessions yet"}</p>
                          </div>
                          <div className="flex flex-wrap items-center gap-2 sm:justify-self-end">
                            <Link href={`/agents/${agent.id}`} className={buttonClassName("outline", "sm")}>
                              Edit agent
                            </Link>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                              onClick={() => {
                                setDeleteError(null);
                                setAgentToDelete(agent);
                              }}
                            >
                              Delete agent
                            </Button>
                          </div>
                        </article>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          </>
        ) : null}
      </main>
            <ConfirmDialog
              open={agentToDelete !== null}
              title={agentToDelete ? `Delete "${agentToDelete.name}"?` : "Delete agent?"}
              description={
                deleteError
                  ? deleteError
                  : "Deleting permanently removes this agent and its saved sessions and answers. Active calls prevent deletion."
              }
              confirmLabel="Delete agent"
              pending={deleting}
              onConfirm={() => void deleteAgent()}
              onOpenChange={(open) => {
                if (!deleting && !open) {
                  setAgentToDelete(null);
                  setDeleteError(null);
                }
              }}
            />
    </OwnerShell>
  );
}

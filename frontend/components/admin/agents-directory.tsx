"use client";

import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import type { AdminAgent, AdminAgentStatus, AdminAgentSuspension } from "./admin-types";
import {
  AdminLoadError,
  AdminLoading,
  AdminNotice,
  errorDetails,
  formatCount,
  formatDateTime,
  type LoadState,
  type Notice,
} from "./admin-ui";

const statusVariants: Record<AdminAgentStatus, "muted" | "success" | "danger"> = {
  draft: "muted",
  published: "success",
  suspended: "danger",
};

export function AgentsDirectory() {
  const [load, setLoad] = useState<LoadState<AdminAgent[]>>({ kind: "loading" });
  const [retryKey, setRetryKey] = useState(0);
  const [suspendingId, setSuspendingId] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoad({ kind: "loading" });

    void api<AdminAgent[]>("/admin/agents", { signal: controller.signal })
      .then((agents) => setLoad({ kind: "ready", data: agents }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({ kind: "failed", message: errorDetails(error, "Couldn't load the agent directory.").message });
      });

    return () => controller.abort();
  }, [retryKey]);

  async function suspendAgent(agent: AdminAgent) {
    const confirmed = window.confirm(
      `Suspend ${agent.name} (${agent.slug})? New calls will be blocked. Calls already in progress will continue, and this action cannot be undone from the console.`,
    );
    if (!confirmed) return;

    setSuspendingId(agent.id);
    setNotice(null);
    try {
      const result = await api<AdminAgentSuspension>(`/admin/agents/${agent.id}/suspend`, {
        method: "POST",
      });
      if (result.id !== agent.id || result.status !== "suspended") {
        throw new Error("The suspension response did not match the selected agent.");
      }

      setLoad((current) =>
        current.kind === "ready"
          ? {
              kind: "ready",
              data: current.data.map((row) =>
                row.id === result.id ? { ...row, status: result.status, updated_at: result.updated_at } : row,
              ),
            }
          : current,
      );
      setNotice({
        kind: "success",
        message: `${agent.name} is suspended. New calls are blocked; calls already in progress will continue.`,
      });
    } catch (error: unknown) {
      setNotice({ kind: "error", message: errorDetails(error, "Couldn't suspend this agent.").message });
    } finally {
      setSuspendingId(null);
    }
  }

  if (load.kind === "loading") return <AdminLoading label="Loading agents…" />;
  if (load.kind === "failed") {
    return <AdminLoadError message={load.message} onRetry={() => setRetryKey((key) => key + 1)} />;
  }

  return (
    <div className="rounded-2xl border border-border bg-card px-5 sm:px-7">
      <div className="flex flex-col gap-3 border-b border-border py-4 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-sm font-medium">All agents</p>
        <p className="text-sm tabular-nums text-muted-foreground">
          {formatCount(load.data.length)} {load.data.length === 1 ? "agent" : "agents"}
        </p>
      </div>
      {notice && <div className="border-b border-border py-4"><AdminNotice notice={notice} /></div>}

      {load.data.length === 0 ? (
        <div className="py-10">
          <h3 className="font-medium">No agents</h3>
          <p className="mt-1 text-sm text-muted-foreground">Agents will appear here after an owner creates one.</p>
        </div>
      ) : (
        <ul aria-label="All agents" className="divide-y divide-border">
          {load.data.map((agent) => {
            const isSuspending = suspendingId === agent.id;
            const canSuspend = agent.status !== "suspended";

            return (
              <li
                key={agent.id}
                className="grid grid-cols-2 gap-x-5 gap-y-4 py-5 md:grid-cols-[minmax(0,1.5fr)_minmax(0,1.3fr)_minmax(0,.8fr)_minmax(0,1.2fr)_repeat(3,minmax(0,.55fr))_minmax(0,1fr)] md:items-center md:gap-x-4"
              >
                <div className="col-span-2 min-w-0 md:col-span-1">
                  <p className="text-xs font-medium text-muted-foreground">Agent</p>
                  <p className="mt-1 break-words font-medium">{agent.name}</p>
                  <p className="mt-1 break-all text-sm text-muted-foreground">{agent.slug}</p>
                </div>
                <div className="col-span-2 min-w-0 md:col-span-1">
                  <p className="text-xs font-medium text-muted-foreground">Owner</p>
                  <p className="mt-1 break-words text-sm">{agent.owner_email}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-muted-foreground">Status</p>
                  <Badge variant={statusVariants[agent.status]} className="mt-1 capitalize">
                    {agent.status}
                  </Badge>
                </div>
                <div>
                  <p className="text-xs font-medium text-muted-foreground">Updated</p>
                  <time className="mt-1 block text-sm" dateTime={agent.updated_at}>
                    {formatDateTime(agent.updated_at)}
                  </time>
                </div>
                <div>
                  <p className="text-xs font-medium text-muted-foreground">Steps</p>
                  <p className="mt-1 text-sm tabular-nums">{formatCount(agent.step_count)}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-muted-foreground">Calls</p>
                  <p className="mt-1 text-sm tabular-nums">{formatCount(agent.session_count)}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-muted-foreground">Active calls</p>
                  <p className="mt-1 text-sm tabular-nums">{formatCount(agent.active_session_count)}</p>
                </div>
                <div className="col-span-2 md:col-span-1">
                  <Button
                    variant={canSuspend ? "destructive" : "secondary"}
                    className="w-full"
                    onClick={() => void suspendAgent(agent)}
                    disabled={!canSuspend || suspendingId !== null}
                    aria-label={canSuspend ? `Suspend ${agent.name} (${agent.slug})` : `${agent.name} is suspended`}
                  >
                    {isSuspending ? "Suspending…" : canSuspend ? "Suspend agent" : "Suspended"}
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

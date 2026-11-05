"use client";

import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { Badge } from "../ui/badge";
import type { AdminUser } from "./admin-types";
import {
  AdminLoadError,
  AdminLoading,
  errorDetails,
  formatCount,
  formatDateTime,
  type LoadState,
} from "./admin-ui";

export function UsersDirectory() {
  const [load, setLoad] = useState<LoadState<AdminUser[]>>({ kind: "loading" });
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoad({ kind: "loading" });

    void api<AdminUser[]>("/admin/users", { signal: controller.signal })
      .then((users) => setLoad({ kind: "ready", data: users }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({ kind: "failed", message: errorDetails(error, "Couldn't load the user directory.").message });
      });

    return () => controller.abort();
  }, [retryKey]);

  if (load.kind === "loading") return <AdminLoading label="Loading users…" />;
  if (load.kind === "failed") {
    return <AdminLoadError message={load.message} onRetry={() => setRetryKey((key) => key + 1)} />;
  }

  return (
    <div className="rounded-2xl border border-border bg-card px-5 sm:px-7">
      <div className="flex items-baseline justify-between gap-4 border-b border-border py-4">
        <p className="text-sm font-medium">Registered accounts</p>
        <p className="text-sm tabular-nums text-muted-foreground">
          {formatCount(load.data.length)} {load.data.length === 1 ? "account" : "accounts"}
        </p>
      </div>

      {load.data.length === 0 ? (
        <div className="py-10">
          <h3 className="font-medium">No user accounts</h3>
          <p className="mt-1 text-sm text-muted-foreground">Accounts will appear here after people register.</p>
        </div>
      ) : (
        <ul aria-label="Registered users" className="divide-y divide-border">
          {load.data.map((user) => (
            <li
              key={user.id}
              className="grid grid-cols-2 gap-x-5 gap-y-4 py-5 md:grid-cols-[minmax(0,2fr)_repeat(4,minmax(0,1fr))] md:gap-x-4"
            >
              <div className="col-span-2 min-w-0 md:col-span-1">
                <p className="text-xs font-medium text-muted-foreground">Account</p>
                <p className="mt-1 break-words font-medium">{user.email}</p>
                <Badge className="mt-2 capitalize">{user.role}</Badge>
              </div>
              <div>
                <p className="text-xs font-medium text-muted-foreground">Joined</p>
                <time className="mt-1 block text-sm" dateTime={user.created_at}>
                  {formatDateTime(user.created_at)}
                </time>
              </div>
              <div>
                <p className="text-xs font-medium text-muted-foreground">Agents</p>
                <p className="mt-1 text-sm tabular-nums">{formatCount(user.agent_count)}</p>
              </div>
              <div>
                <p className="text-xs font-medium text-muted-foreground">Calls</p>
                <p className="mt-1 text-sm tabular-nums">{formatCount(user.session_count)}</p>
              </div>
              <div>
                <p className="text-xs font-medium text-muted-foreground">Active calls</p>
                <p className="mt-1 text-sm tabular-nums">{formatCount(user.active_session_count)}</p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

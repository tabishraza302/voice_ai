"use client";

import { UserRound } from "lucide-react";
import { PlatformAgentDirectory } from "@/components/PlatformAgentDirectory";
import { useAuth } from "@/lib/auth-state";

export function PlatformCustomerDashboard() {
  const { state } = useAuth();
  const user = state.status === "authenticated" ? state.user : null;

  return (
    <div className="space-y-8">
      <header className="flex flex-col gap-5 border-b border-ink-800 pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-sm font-medium text-copper-300">Customer workspace</p>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight text-limestone-100">
            {user ? `Welcome, ${user.display_name}` : "Your workspace"}
          </h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">
            Configure your agents, review their drafts, and test conversations before publishing.
          </p>
        </div>
        {user ? (
          <div className="flex items-center gap-3 rounded-xl border border-ink-800 bg-ink-950/60 px-4 py-3">
            <span className="flex h-9 w-9 items-center justify-center rounded-full bg-ink-800 text-copper-300">
              <UserRound className="h-4 w-4" aria-hidden="true" />
            </span>
            <span className="min-w-0">
              <span className="block text-xs text-limestone-500">Signed in as</span>
              <span className="block max-w-64 truncate text-sm font-medium text-limestone-200">{user.email}</span>
            </span>
          </div>
        ) : null}
      </header>

      <aside className="surface flex flex-col gap-2 border-copper-900/70 p-4 text-sm sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="font-medium text-limestone-100">Account activity</h2>
          <p className="mt-1 text-limestone-400">
            Usage, wallet balance, and call history are not available in this workspace yet.
          </p>
        </div>
      </aside>

      <PlatformAgentDirectory compact />
    </div>
  );
}

"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ArrowRight, RefreshCw, UserRound } from "lucide-react";
import { PlatformAgentDirectory } from "@/components/PlatformAgentDirectory";
import { useAuth } from "@/lib/auth-state";
import { PlatformApiError, platformApi } from "@/lib/platform-api-client";
import { formatMinorCurrency, formatPlatformDate } from "@/lib/platform-format";
import type { OwnerUsagePage, WalletView } from "@/types/platform";


export function PlatformCustomerDashboard() {
  const { state } = useAuth();
  const user = state.status === "authenticated" ? state.user : null;
  const [wallet, setWallet] = useState<WalletView | null>(null);
  const [usage, setUsage] = useState<OwnerUsagePage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [walletView, usagePage] = await Promise.all([
        platformApi.getWallet(),
        platformApi.getOwnerUsage({ limit: 5 }),
      ]);
      setWallet(walletView);
      setUsage(usagePage);
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not load account activity.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="space-y-8">
      <header className="flex flex-col gap-5 border-b border-ink-800 pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-sm font-medium text-copper-300">Customer workspace</p>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight text-limestone-100">
            {user ? `Welcome, ${user.display_name}` : "Your workspace"}
          </h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">
            Configure your agents, review their drafts, and track your account activity.
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

      <section aria-labelledby="account-activity-title" className="space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 id="account-activity-title" className="text-lg font-semibold text-limestone-100">
              Account activity
            </h2>
            <p className="mt-1 text-sm text-limestone-400">Balances and usage from your account records.</p>
          </div>
          <button className="button-secondary" onClick={() => void refresh()} disabled={loading}>
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>

        {error ? (
          <div role="alert" className="surface flex flex-wrap items-center justify-between gap-3 border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">
            <span>{error}</span>
            <button className="button-secondary" onClick={() => void refresh()}>Try again</button>
          </div>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-3">
          {[
            { label: "Available balance", value: wallet ? formatMinorCurrency(wallet.available_minor) : "—" },
            { label: "Reserved for calls", value: wallet ? formatMinorCurrency(wallet.reserved_minor) : "—" },
            { label: "Posted balance", value: wallet ? formatMinorCurrency(wallet.posted_minor) : "—" },
          ].map((item) => (
            <article key={item.label} className="surface p-4">
              <p className="text-xs font-medium uppercase tracking-wide text-limestone-500">{item.label}</p>
              <p className="mt-2 text-2xl font-semibold tabular-nums text-limestone-100">
                {loading && !wallet ? "Loading…" : item.value}
              </p>
              <p className="mt-1 text-xs text-limestone-500">INR · updated {wallet ? formatPlatformDate(wallet.updated_at) : "—"}</p>
            </article>
          ))}
        </div>

        <div className="surface divide-y divide-ink-800">
          <div className="flex flex-wrap items-center justify-between gap-3 p-4">
            <div>
              <h3 className="font-medium text-limestone-100">Recent call usage</h3>
              <p className="mt-1 text-xs text-limestone-500">Estimates and final charges are shown separately.</p>
            </div>
            <Link href="/billing" className="button-secondary">Billing details<ArrowRight className="h-4 w-4" /></Link>
          </div>
          {loading && !usage ? (
            <p role="status" className="p-4 text-sm text-limestone-400">Loading usage…</p>
          ) : usage?.items.length ? (
            usage.items.slice(0, 3).map(({ session, billing }) => (
              <Link
                key={session.session_id}
                href={`/conversations?session=${encodeURIComponent(session.session_id)}`}
                className="flex flex-wrap items-center justify-between gap-3 p-4 text-sm hover:bg-ink-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-copper-400"
              >
                <span>
                  <span className="block font-medium text-limestone-100">{session.kind.replace("_", " ")} · {session.state}</span>
                  <span className="mt-1 block text-xs text-limestone-500">{formatPlatformDate(session.created_at)}</span>
                </span>
                <span className="text-right">
                  <span className="block font-medium tabular-nums text-limestone-200">
                    {billing ? formatMinorCurrency(billing.charge_status === "provisional" ? (billing.authorized_estimate_minor ?? 0) : billing.charged_minor) : "No usage record"}
                  </span>
                  <span className="mt-1 block text-xs text-limestone-500">
                    {billing ? `${billing.charge_status === "provisional" ? "Authorized estimate" : "Final charge"} · ${billing.billed_ms} ms` : "—"}
                  </span>
                </span>
              </Link>
            ))
          ) : (
            <p className="p-4 text-sm text-limestone-400">No call usage has been recorded.</p>
          )}
        </div>

        <Link href="/conversations" className="button-secondary">View call history<ArrowRight className="h-4 w-4" /></Link>
      </section>

      <PlatformAgentDirectory compact />
    </div>
  );
}

"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ArrowRight, RefreshCw } from "lucide-react";
import { AdminGate } from "@/components/AdminGate";
import { PlatformApiError, platformApi } from "@/lib/platform-api-client";
import { formatMinorCurrency, formatPlatformDate } from "@/lib/platform-format";
import type { AdminOverview } from "@/types/platform";

function AdminOverviewContent() {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setOverview(await platformApi.adminGetOverview());
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not load platform overview.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const counts = overview ? [
    { label: "Users", value: overview.users_total, detail: `${overview.users_active} active` },
    { label: "Agents", value: overview.agents_total, detail: `${overview.owner_enabled_agents} enabled for owners` },
    { label: "Sessions", value: overview.sessions_total, detail: "All stored session records" },
    { label: "Wallet accounts", value: overview.wallet_accounts, detail: "Accounts with wallet rows" },
    { label: "Open call budgets", value: overview.call_budgets_open, detail: `${overview.call_budgets_closed} closed` },
  ] : [];

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-ink-800 pb-6">
        <div><p className="text-sm font-medium text-copper-300">Administration</p><h1 className="mt-1 text-3xl font-semibold tracking-tight text-limestone-100">Platform overview</h1><p className="mt-2 text-sm text-limestone-400">Live aggregates from platform records.</p></div>
        <button className="button-secondary" onClick={() => void refresh()} disabled={loading}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />Refresh</button>
      </header>
      {error ? <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{error}</div> : null}
      <section aria-label="Platform counts" className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {counts.map((item) => <article key={item.label} className="surface p-4"><p className="text-xs uppercase tracking-wide text-limestone-500">{item.label}</p><p className="mt-2 text-3xl font-semibold tabular-nums text-limestone-100">{loading && !overview ? "…" : item.value}</p><p className="mt-1 text-xs text-limestone-400">{item.detail}</p></article>)}
      </section>
      {overview ? <p className="text-xs text-limestone-500">Snapshot generated {formatPlatformDate(overview.generated_at)}.</p> : null}
      {overview ? <section aria-labelledby="wallet-totals-title" className="space-y-3"><div><h2 id="wallet-totals-title" className="text-lg font-semibold text-limestone-100">Wallet and charges</h2><p className="mt-1 text-sm text-limestone-400">Posted wallet values and budget charges in INR paise.</p></div><div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{[{label:"Posted",value:overview.wallet_posted_minor},{label:"Reserved",value:overview.wallet_reserved_minor},{label:"Available",value:overview.wallet_available_minor},{label:"Settled charges",value:overview.settled_charged_minor}].map((item)=><article key={item.label} className="surface p-4"><p className="text-xs uppercase tracking-wide text-limestone-500">{item.label}</p><p className="mt-2 text-xl font-semibold tabular-nums text-limestone-100">{formatMinorCurrency(item.value)}</p></article>)}</div><p className="text-xs text-limestone-500">Provisional charges: {formatMinorCurrency(overview.provisional_charged_minor)} · estimates have not been settled.</p></section> : null}
      <section aria-labelledby="admin-tools-title" className="space-y-3"><h2 id="admin-tools-title" className="text-lg font-semibold text-limestone-100">Operator tools</h2><div className="surface divide-y divide-ink-800">{[
        { href: "/admin/users", title: "User accounts", detail: "Review account state and verification" },
        { href: "/admin/wallet", title: "Wallet inspection", detail: "Search customer wallets and ledger entries" },
        { href: "/admin/agents", title: "Agent inventory", detail: "Search agents and inspect current published configuration" },
        { href: "/admin/sessions", title: "Sessions and transcripts", detail: "Inspect pinned facts, audited transcripts, and live-call stop controls" },
        { href: "/admin/tariffs", title: "Rates and call controls", detail: "Review tariff versions and set scoped admission pauses" },
        { href: "/admin/audit", title: "Audit log", detail: "Search recorded operator actions" },
      ].map((item) => <Link key={item.href} href={item.href} className="flex items-center justify-between gap-4 p-4 hover:bg-ink-900"><span><strong className="font-medium text-limestone-100">{item.title}</strong><span className="mt-1 block text-sm text-limestone-400">{item.detail}</span></span><ArrowRight className="h-4 w-4 text-limestone-500" /></Link>)}</div></section>
    </div>
  );
}

export default function AdminOverviewPage() {
  return <AdminGate><AdminOverviewContent /></AdminGate>;
}

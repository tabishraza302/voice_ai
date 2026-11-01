"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowRight, RefreshCw } from "lucide-react";
import { PlatformApiError, platformApi } from "@/lib/platform-api-client";
import { formatDurationMs, formatMinorCurrency, formatPlatformDate } from "@/lib/platform-format";
import type { OwnerUsageItem, WalletEntriesPage, WalletView } from "@/types/platform";

export default function BillingPage() {
  const [wallet, setWallet] = useState<WalletView | null>(null);
  const [entries, setEntries] = useState<WalletEntriesPage | null>(null);
  const [usage, setUsage] = useState<OwnerUsageItem[]>([]);
  const [entryCursor, setEntryCursor] = useState<string | null>(null);
  const [usageCursor, setUsageCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const [walletView, ledgerPage, usagePage] = await Promise.all([
        platformApi.getWallet(),
        platformApi.getWalletEntries({ limit: 20 }),
        platformApi.getOwnerUsage({ limit: 20 }),
      ]);
      if (requestId !== requestIdRef.current) return;
      setWallet(walletView);
      setEntries(ledgerPage);
      setUsage(usagePage.items);
      setEntryCursor(ledgerPage.next_cursor);
      setUsageCursor(usagePage.next_cursor);
    } catch (err: unknown) {
      if (requestId === requestIdRef.current) {
        setError(err instanceof PlatformApiError ? err.message : "Could not load billing records.");
      }
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const loadMore = async () => {
    if (loadingMore || (!entryCursor && !usageCursor)) return;
    setLoadingMore(true);
    setError(null);
    try {
      const [ledgerPage, usagePage] = await Promise.all([
        entryCursor ? platformApi.getWalletEntries({ cursor: entryCursor, limit: 20 }) : Promise.resolve(null),
        usageCursor ? platformApi.getOwnerUsage({ cursor: usageCursor, limit: 20 }) : Promise.resolve(null),
      ]);
      if (ledgerPage) {
        setEntries((current) => ({
          items: [...(current?.items ?? []), ...ledgerPage.items],
          next_cursor: ledgerPage.next_cursor,
        }));
        setEntryCursor(ledgerPage.next_cursor);
      }
      if (usagePage) {
        setUsage((current) => [...current, ...usagePage.items]);
        setUsageCursor(usagePage.next_cursor);
      }
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not load more billing records.");
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-ink-800 pb-6">
        <div>
          <p className="text-sm font-medium text-copper-300">Your account</p>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight text-limestone-100">Billing and usage</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">
            Current wallet balances, posted ledger entries, and per-call usage from your account records.
          </p>
        </div>
        <button className="button-secondary" onClick={() => void refresh()} disabled={loading}>
          <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />Refresh
        </button>
      </header>

      {error ? <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{error}</div> : null}

      <section aria-label="Wallet balances" className="grid gap-3 sm:grid-cols-3">
        {[
          { label: "Available", amount: wallet?.available_minor },
          { label: "Reserved", amount: wallet?.reserved_minor },
          { label: "Posted", amount: wallet?.posted_minor },
        ].map((balance) => (
          <article key={balance.label} className="surface p-5">
            <p className="text-xs font-medium uppercase tracking-wide text-limestone-500">{balance.label} balance</p>
            <p className="mt-2 text-2xl font-semibold tabular-nums text-limestone-100">
              {balance.amount === undefined ? (loading ? "Loading…" : "—") : formatMinorCurrency(balance.amount)}
            </p>
            <p className="mt-1 text-xs text-limestone-500">INR · {wallet ? `wallet version ${wallet.version}` : "wallet"}</p>
          </article>
        ))}
      </section>

      <section aria-labelledby="usage-title" className="space-y-3">
        <div>
          <h2 id="usage-title" className="text-lg font-semibold text-limestone-100">Call usage</h2>
          <p className="mt-1 text-sm text-limestone-400">An authorization estimate is not a settled charge.</p>
        </div>
        <div className="surface divide-y divide-ink-800">
          {loading && usage.length === 0 ? <p role="status" className="p-4 text-sm text-limestone-400">Loading usage…</p> : null}
          {usage.map(({ session, billing }) => {
            const amount = billing?.charge_status === "provisional"
              ? billing.authorized_estimate_minor
              : billing?.charged_minor ?? null;
            const amountLabel = billing?.charge_status === "provisional" ? "Authorized estimate" : "Final charge";
            return (
              <div key={session.session_id} className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <Link href={`/conversations?session=${encodeURIComponent(session.session_id)}`} className="font-medium text-limestone-100 hover:text-copper-200">
                    {session.kind.replaceAll("_", " ")} · {session.state}
                  </Link>
                  <p className="mt-1 text-xs text-limestone-500">{formatPlatformDate(session.created_at)} · {session.session_id}</p>
                  {billing ? <p className="mt-1 text-xs text-limestone-400">{formatDurationMs(billing.billed_ms)} billed · ₹{(billing.pinned_rate_minor_per_minute / 100).toFixed(2)}/min</p> : null}
                </div>
                <div className="sm:text-right">
                  <p className="font-medium tabular-nums text-limestone-100">{amount === null ? "—" : formatMinorCurrency(amount)}</p>
                  <p className="mt-1 text-xs text-limestone-500">{billing ? amountLabel : "No budget record"}</p>
                </div>
              </div>
            );
          })}
          {!loading && !error && usage.length === 0 ? <p className="p-4 text-sm text-limestone-400">No call usage has been recorded.</p> : null}
          {error && usage.length === 0 ? <p className="p-4 text-sm text-rust-200">Call usage is unavailable until the records load.</p> : null}
        </div>
      </section>

      <section aria-labelledby="ledger-title" className="space-y-3">
        <div>
          <h2 id="ledger-title" className="text-lg font-semibold text-limestone-100">Wallet ledger</h2>
          <p className="mt-1 text-sm text-limestone-400">Posted entries are shown as recorded; this page does not change your balance.</p>
        </div>
        <div className="surface divide-y divide-ink-800">
          {entries?.items.map((entry) => (
            <article key={entry.id} className="flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <p className="font-medium capitalize text-limestone-100">{entry.kind}</p>
                <p className="mt-1 text-sm text-limestone-300">{entry.reason}</p>
                <p className="mt-1 text-xs text-limestone-500">{formatPlatformDate(entry.created_at)} · {entry.operation_id}</p>
              </div>
              <p className="shrink-0 font-semibold tabular-nums text-limestone-100">{formatMinorCurrency(entry.amount_minor)}</p>
            </article>
          ))}
          {!loading && !error && (entries?.items.length ?? 0) === 0 ? <p className="p-4 text-sm text-limestone-400">No ledger entries have been recorded.</p> : null}
          {error && (entries?.items.length ?? 0) === 0 ? <p className="p-4 text-sm text-rust-200">Ledger entries are unavailable until the records load.</p> : null}
        </div>
      </section>

      {entryCursor || usageCursor ? (
        <button className="button-secondary" onClick={() => void loadMore()} disabled={loadingMore}>
          <RefreshCw className={`h-4 w-4 ${loadingMore ? "animate-spin" : ""}`} />Load more records
        </button>
      ) : null}
      <Link href="/conversations" className="button-secondary">Open call history<ArrowRight className="h-4 w-4" /></Link>
    </div>
  );
}

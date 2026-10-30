"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { RefreshCw, Search } from "lucide-react";
import { AdminGate } from "@/components/AdminGate";
import { PlatformApiError, platformApi } from "@/lib/platform-api-client";
import { formatMinorCurrency, formatPlatformDate } from "@/lib/platform-format";
import type { UserView } from "@/types/auth";
import type { WalletEntriesPage, WalletView } from "@/types/platform";

function AdminWalletContent() {
  const [users, setUsers] = useState<UserView[]>([]);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [selectedUser, setSelectedUser] = useState<UserView | null>(null);
  const [wallet, setWallet] = useState<WalletView | null>(null);
  const [entries, setEntries] = useState<WalletEntriesPage | null>(null);
  const [entryCursor, setEntryCursor] = useState<string | null>(null);
  const [loadingUsers, setLoadingUsers] = useState(false);
  const [loadingWallet, setLoadingWallet] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const searchUsers = useCallback(async () => {
    setLoadingUsers(true);
    setError(null);
    try {
      const page = await platformApi.adminGetUsers({ limit: 25, q: appliedQuery || undefined });
      setUsers(page.items);
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not search for accounts.");
    } finally {
      setLoadingUsers(false);
    }
  }, [appliedQuery]);

  useEffect(() => {
    void searchUsers();
  }, [searchUsers]);

  const loadWallet = useCallback(async (user: UserView) => {
    setSelectedUser(user);
    setWallet(null);
    setEntries(null);
    setEntryCursor(null);
    setLoadingWallet(true);
    setError(null);
    try {
      const [walletView, ledgerPage] = await Promise.all([
        platformApi.adminGetWallet(user.id),
        platformApi.adminGetWalletEntries(user.id, { limit: 25 }),
      ]);
      setWallet(walletView);
      setEntries(ledgerPage);
      setEntryCursor(ledgerPage.next_cursor);
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not load this wallet.");
    } finally {
      setLoadingWallet(false);
    }
  }, []);

  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    setAppliedQuery(query.trim());
  };

  const loadMore = async () => {
    if (!selectedUser || !entryCursor || loadingMore) return;
    setLoadingMore(true);
    setError(null);
    try {
      const page = await platformApi.adminGetWalletEntries(selectedUser.id, { cursor: entryCursor, limit: 25 });
      setEntries((current) => ({ items: [...(current?.items ?? []), ...page.items], next_cursor: page.next_cursor }));
      setEntryCursor(page.next_cursor);
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not load more wallet entries.");
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <div className="space-y-8">
      <header className="border-b border-ink-800 pb-6">
        <p className="text-sm font-medium text-copper-300">Administration</p>
        <h1 className="mt-1 text-3xl font-semibold tracking-tight text-limestone-100">Wallet inspection</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">Search a customer account and inspect the current wallet balances and immutable ledger entries.</p>
      </header>
      {error ? <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{error}</div> : null}
      <div className="grid gap-6 xl:grid-cols-[minmax(18rem,0.8fr)_minmax(0,1.2fr)]">
        <section aria-labelledby="wallet-account-title" className="space-y-3">
          <h2 id="wallet-account-title" className="text-lg font-semibold text-limestone-100">Find account</h2>
          <form onSubmit={submitSearch} className="surface flex gap-2 p-3">
            <label className="relative min-w-0 flex-1">
              <span className="sr-only">Search accounts by name or email</span>
              <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-limestone-500" />
              <input className="input w-full pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Name or email" />
            </label>
            <button className="button-primary" type="submit" disabled={loadingUsers}>Search</button>
          </form>
          <div className="surface divide-y divide-ink-800">
            {loadingUsers && users.length === 0 ? <p role="status" className="p-4 text-sm text-limestone-400">Searching accounts…</p> : null}
            {users.map((user) => <button key={user.id} type="button" onClick={() => void loadWallet(user)} aria-pressed={selectedUser?.id === user.id} className={`block w-full p-4 text-left hover:bg-ink-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-copper-400 ${selectedUser?.id === user.id ? "bg-ink-900" : ""}`}><span className="block font-medium text-limestone-100">{user.display_name}</span><span className="mt-1 block text-sm text-limestone-400">{user.email}</span><span className="mt-1 block font-mono text-[11px] text-limestone-600">{user.id}</span></button>)}
            {!loadingUsers && users.length === 0 ? <p className="p-4 text-sm text-limestone-400">No accounts match this search.</p> : null}
          </div>
        </section>

        <section aria-labelledby="wallet-detail-title" className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 id="wallet-detail-title" className="text-lg font-semibold text-limestone-100">Wallet and ledger</h2><p className="mt-1 text-sm text-limestone-400">{selectedUser ? `${selectedUser.display_name} · ${selectedUser.email}` : "Select an account."}</p></div>{selectedUser ? <button className="button-secondary" onClick={() => void loadWallet(selectedUser)} disabled={loadingWallet} aria-label="Refresh selected wallet"><RefreshCw className={`h-4 w-4 ${loadingWallet ? "animate-spin" : ""}`} /></button> : null}</div>
          {loadingWallet ? <div role="status" className="surface p-4 text-sm text-limestone-400">Loading wallet…</div> : null}
          {wallet ? <div className="grid gap-3 sm:grid-cols-3">{[{ name: "Available", amount: wallet.available_minor }, { name: "Reserved", amount: wallet.reserved_minor }, { name: "Posted", amount: wallet.posted_minor }].map((item) => <article key={item.name} className="surface p-4"><p className="text-xs uppercase tracking-wide text-limestone-500">{item.name}</p><p className="mt-2 text-xl font-semibold tabular-nums text-limestone-100">{formatMinorCurrency(item.amount)}</p><p className="mt-1 text-xs text-limestone-500">INR · wallet v{wallet.version}</p></article>)}</div> : null}
          <div className="surface divide-y divide-ink-800">
            <div className="p-4"><h3 className="font-medium text-limestone-100">Ledger entries</h3><p className="mt-1 text-xs text-limestone-500">Read-only support inspection.</p></div>
            {entries?.items.map((entry) => <article key={entry.id} className="flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between"><div className="min-w-0"><p className="font-medium capitalize text-limestone-100">{entry.kind}</p><p className="mt-1 text-sm text-limestone-300">{entry.reason}</p><p className="mt-1 break-all text-xs text-limestone-500">{formatPlatformDate(entry.created_at)} · {entry.operation_id}</p></div><p className="shrink-0 font-semibold tabular-nums text-limestone-100">{formatMinorCurrency(entry.amount_minor)}</p></article>)}
            {!loadingWallet && selectedUser && (entries?.items.length ?? 0) === 0 ? <p className="p-4 text-sm text-limestone-400">No wallet ledger entries have been recorded.</p> : null}
          </div>
          {entryCursor ? <button className="button-secondary" onClick={() => void loadMore()} disabled={loadingMore}>{loadingMore ? "Loading…" : "Load more entries"}</button> : null}
        </section>
      </div>
    </div>
  );
}

export default function AdminWalletPage() {
  return <AdminGate><AdminWalletContent /></AdminGate>;
}

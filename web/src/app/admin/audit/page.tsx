"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { RefreshCw, Search } from "lucide-react";
import { PlatformApiError, platformApi } from "@/lib/platform-api-client";
import { formatPlatformDate } from "@/lib/platform-format";
import type { AdminAuditItem, AdminAuditPage } from "@/types/platform";

function AdminAuditContent() {
  const [page, setPage] = useState<AdminAuditPage | null>(null);
  const [items, setItems] = useState<AdminAuditItem[]>([]);
  const [filters, setFilters] = useState({ action: "", targetType: "", targetId: "", actorId: "" });
  const [appliedFilters, setAppliedFilters] = useState(filters);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadAudit = useCallback(async (targetCursor: string | null = null, append = false) => {
    setLoading(!append);
    setLoadingMore(append);
    setError(null);
    try {
      const result = await platformApi.adminGetAuditLogs({
        cursor: targetCursor,
        limit: 50,
        action: appliedFilters.action || undefined,
        target_type: appliedFilters.targetType || undefined,
        target_id: appliedFilters.targetId || undefined,
        actor_id: appliedFilters.actorId || undefined,
      });
      setPage(result);
      setItems((current) => append ? [...current, ...result.items] : result.items);
      setCursor(result.next_cursor);
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not load the audit log.");
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [appliedFilters]);

  useEffect(() => {
    void loadAudit();
  }, [loadAudit]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setAppliedFilters({
      action: filters.action.trim(),
      targetType: filters.targetType.trim(),
      targetId: filters.targetId.trim(),
      actorId: filters.actorId.trim(),
    });
  };

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-ink-800 pb-6">
        <div><p className="text-sm font-medium text-copper-300">Administration</p><h1 className="mt-1 text-3xl font-semibold tracking-tight text-limestone-100">Audit log</h1><p className="mt-2 max-w-2xl text-sm text-limestone-400">Search immutable records of operator and support actions.</p></div>
        <button className="button-secondary" onClick={() => void loadAudit()} disabled={loading}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />Refresh</button>
      </header>
      {error ? <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{error}</div> : null}
      <form onSubmit={submit} className="surface grid gap-3 p-4 sm:grid-cols-2 xl:grid-cols-5">
        <label className="text-xs text-limestone-400">Action<input className="input mt-1 w-full" value={filters.action} onChange={(event) => setFilters((current) => ({ ...current, action: event.target.value }))} placeholder="admin.tariff.version.create" /></label>
        <label className="text-xs text-limestone-400">Target type<input className="input mt-1 w-full" value={filters.targetType} onChange={(event) => setFilters((current) => ({ ...current, targetType: event.target.value }))} placeholder="session" /></label>
        <label className="text-xs text-limestone-400">Target ID<input className="input mt-1 w-full" value={filters.targetId} onChange={(event) => setFilters((current) => ({ ...current, targetId: event.target.value }))} placeholder="UUID" /></label>
        <label className="text-xs text-limestone-400">Actor ID<input className="input mt-1 w-full" value={filters.actorId} onChange={(event) => setFilters((current) => ({ ...current, actorId: event.target.value }))} placeholder="UUID" /></label>
        <button className="button-primary self-end" type="submit"><Search className="h-4 w-4" />Search</button>
      </form>
      <section aria-labelledby="audit-results-title" className="space-y-3">
        <h2 id="audit-results-title" className="text-lg font-semibold text-limestone-100">Recorded actions</h2>
        <div className="surface divide-y divide-ink-800">
          {loading && items.length === 0 ? <p role="status" className="p-4 text-sm text-limestone-400">Loading audit records…</p> : null}
          {items.map((item) => <article key={item.id} className="grid gap-3 p-4 text-sm xl:grid-cols-[1.1fr_1fr_1fr_1fr]">
            <div><p className="font-medium text-limestone-100">{item.action}</p><p className="mt-1 text-xs text-limestone-500">{formatPlatformDate(item.created_at)} · {item.result}</p></div>
            <div><p className="text-xs text-limestone-500">Actor</p><p className="mt-1 break-all text-limestone-300">{item.actor_kind} · {item.actor_id ?? "—"}</p></div>
            <div><p className="text-xs text-limestone-500">Target</p><p className="mt-1 break-all text-limestone-300">{item.target_type} · {item.target_id ?? "—"}</p>{item.model_change ? <div className="mt-1 break-all text-xs text-limestone-300"><p>{item.model_change.provider_id} · {item.model_change.model_id}</p><p>Before: {item.model_change.before ? `enabled ${item.model_change.before.enabled ? "yes" : "no"}, qualified ${item.model_change.before.qualified ? "yes" : "no"}` : "not listed"}</p><p>After: {item.model_change.after ? `enabled ${item.model_change.after.enabled ? "yes" : "no"}, qualified ${item.model_change.after.qualified ? "yes" : "no"}` : "removed"}</p></div> : null}{item.session_id ? <p className="mt-1 break-all text-xs text-limestone-500">Session {item.session_id}</p> : null}</div>
            <div><p className="text-xs text-limestone-500">Reason and request</p><p className="mt-1 text-limestone-300">{item.reason ?? "—"}</p><p className="mt-1 break-all text-xs text-limestone-500">Request {item.request_id ?? "—"}{item.source_ip ? ` · ${item.source_ip}` : ""}</p></div>
          </article>)}
          {!loading && items.length === 0 ? <p className="p-4 text-sm text-limestone-400">No audit entries match these filters.</p> : null}
        </div>
        {cursor ? <button className="button-secondary" onClick={() => void loadAudit(cursor, true)} disabled={loadingMore}>{loadingMore ? "Loading…" : "Load more audit records"}</button> : null}
        {page && !page.next_cursor && items.length > 0 ? <p className="text-xs text-limestone-500">End of audit results.</p> : null}
      </section>
    </div>
  );
}

export default function AdminAuditPage() {
  return <AdminAuditContent />;
}

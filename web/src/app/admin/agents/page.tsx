"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { RefreshCw, Search } from "lucide-react";
import { AdminGate } from "@/components/AdminGate";
import { PlatformApiError, platformApi } from "@/lib/platform-api-client";
import { formatPlatformDate } from "@/lib/platform-format";
import type { AdminAgentDetail, AdminAgentItem, AdminAgentPage } from "@/types/platform";

function AdminAgentsContent() {
  const [page, setPage] = useState<AdminAgentPage | null>(null);
  const [agents, setAgents] = useState<AdminAgentItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AdminAgentDetail | null>(null);
  const [query, setQuery] = useState("");
  const [ownerId, setOwnerId] = useState("");
  const [appliedQuery, setAppliedQuery] = useState({ query: "", ownerId: "" });
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const loadAgents = useCallback(async (targetCursor: string | null = null, append = false) => {
    setLoading(!append);
    setLoadingMore(append);
    setError(null);
    try {
      const result = await platformApi.adminGetAgents({
        cursor: targetCursor,
        limit: 25,
        q: appliedQuery.query || undefined,
        owner_user_id: appliedQuery.ownerId || undefined,
      });
      setPage(result);
      setAgents((current) => append ? [...current, ...result.items] : result.items);
      setCursor(result.next_cursor);
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not load the agent inventory.");
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, [appliedQuery]);

  useEffect(() => {
    void loadAgents();
  }, [loadAgents]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    let current = true;
    setDetailLoading(true);
    setDetailError(null);
    setDetail(null);
    platformApi.adminGetAgent(selectedId).then((result) => {
      if (current) setDetail(result);
    }).catch((err: unknown) => {
      if (current) setDetailError(err instanceof PlatformApiError ? err.message : "Could not load this agent.");
    }).finally(() => {
      if (current) setDetailLoading(false);
    });
    return () => { current = false; };
  }, [selectedId]);

  const applySearch = (event: FormEvent) => {
    event.preventDefault();
    setAppliedQuery({ query: query.trim(), ownerId: ownerId.trim() });
  };

  return (
    <div className="space-y-8">
      <header className="border-b border-ink-800 pb-6">
        <p className="text-sm font-medium text-copper-300">Administration</p>
        <h1 className="mt-1 text-3xl font-semibold tracking-tight text-limestone-100">Agent inventory</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">
          Search platform agents and inspect the immutable published version currently selected for new calls.
        </p>
      </header>

      {error ? <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{error}</div> : null}
      <form onSubmit={applySearch} className="surface grid gap-3 p-4 sm:grid-cols-[1fr_1fr_auto]">
        <label className="text-xs text-limestone-400">
          Name or description
          <span className="relative mt-1 block">
            <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-limestone-500" />
            <input className="input w-full pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search agents" />
          </span>
        </label>
        <label className="text-xs text-limestone-400">
          Owner user ID
          <input className="input mt-1 w-full" value={ownerId} onChange={(event) => setOwnerId(event.target.value)} placeholder="UUID" />
        </label>
        <div className="flex items-end gap-2">
          <button className="button-primary" type="submit">Search</button>
          <button className="button-secondary" type="button" onClick={() => void loadAgents()} disabled={loading} aria-label="Refresh agent inventory">
            <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </form>

      <div className="grid gap-6 xl:grid-cols-[minmax(18rem,0.85fr)_minmax(0,1.15fr)]">
        <section aria-labelledby="agent-list-title" className="space-y-3">
          <h2 id="agent-list-title" className="text-lg font-semibold text-limestone-100">Agents <span className="text-sm font-normal text-limestone-500">{agents.length}</span></h2>
          <div className="surface divide-y divide-ink-800">
            {loading && agents.length === 0 ? <p role="status" className="p-4 text-sm text-limestone-400">Loading inventory…</p> : null}
            {agents.map((agent) => (
              <button
                key={agent.id}
                type="button"
                onClick={() => setSelectedId(agent.id)}
                aria-pressed={selectedId === agent.id}
                className={`block w-full p-4 text-left text-sm hover:bg-ink-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-copper-400 ${selectedId === agent.id ? "bg-ink-900" : ""}`}
              >
                <span className="flex items-start justify-between gap-3">
                  <span className="min-w-0">
                    <span className="block truncate font-medium text-limestone-100">{agent.name}</span>
                    <span className="mt-1 block line-clamp-2 text-xs text-limestone-400">{agent.description || "No description"}</span>
                  </span>
                  <span className={`shrink-0 rounded-full border px-2 py-1 text-[11px] ${agent.enabled ? "border-moss-800 text-moss-200" : "border-ink-700 text-limestone-400"}`}>
                    {agent.enabled ? "Enabled" : "Disabled"}
                  </span>
                </span>
                <span className="mt-3 block text-xs text-limestone-500">
                  Published version {agent.current_published_version ?? "none"} · updated {formatPlatformDate(agent.updated_at)}
                </span>
              </button>
            ))}
            {!loading && agents.length === 0 ? <p className="p-4 text-sm text-limestone-400">No agents match this search.</p> : null}
          </div>
          {cursor ? <button className="button-secondary w-full" onClick={() => void loadAgents(cursor, true)} disabled={loadingMore}>{loadingMore ? "Loading…" : "Load more agents"}</button> : null}
          {page && !page.next_cursor && agents.length > 0 ? <p className="text-xs text-limestone-500">End of inventory results.</p> : null}
        </section>

        <section aria-labelledby="agent-detail-title" className="space-y-3">
          <h2 id="agent-detail-title" className="text-lg font-semibold text-limestone-100">Pinned published configuration</h2>
          {!selectedId ? <div className="surface p-5 text-sm text-limestone-400">Select an agent to inspect its current immutable configuration.</div> : null}
          {detailError ? <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{detailError}</div> : null}
          {detailLoading ? <div role="status" className="surface p-5 text-sm text-limestone-400">Loading configuration…</div> : null}
          {detail ? (
            <article className="surface space-y-4 p-5">
              <div>
                <h3 className="text-lg font-semibold text-limestone-100">{detail.name}</h3>
                <p className="mt-1 text-sm text-limestone-400">Owned by {detail.owner_display_name} · {detail.owner_email}</p>
                <p className="mt-1 break-all font-mono text-xs text-limestone-500">Owner ID: {detail.owner_user_id}</p>
              </div>
              <dl className="grid gap-3 text-sm sm:grid-cols-2">
                <div><dt className="text-xs text-limestone-500">Current version</dt><dd className="mt-1 text-limestone-200">{detail.current_published_version ?? "No published version"}</dd></div>
                <div><dt className="text-xs text-limestone-500">Agent state</dt><dd className="mt-1 text-limestone-200">{detail.enabled ? "Enabled" : "Disabled"}</dd></div>
                <div><dt className="text-xs text-limestone-500">Flow hash</dt><dd className="mt-1 break-all font-mono text-xs text-limestone-300">{detail.flow_hash ?? "—"}</dd></div>
                <div><dt className="text-xs text-limestone-500">Artifact hash</dt><dd className="mt-1 break-all font-mono text-xs text-limestone-300">{detail.artifact_hash ?? "—"}</dd></div>
                <div className="sm:col-span-2"><dt className="text-xs text-limestone-500">Authoring hash</dt><dd className="mt-1 break-all font-mono text-xs text-limestone-300">{detail.authoring_hash ?? "—"}</dd></div>
              </dl>
              {detail.immutable_config ? (
                <details>
                  <summary className="cursor-pointer text-sm text-copper-200">View immutable configuration</summary>
                  <pre className="mt-3 max-h-[32rem] overflow-auto rounded-lg border border-ink-800 bg-ink-950 p-3 text-xs text-limestone-300">{JSON.stringify(detail.immutable_config, null, 2)}</pre>
                </details>
              ) : <p className="text-sm text-limestone-500">No published configuration is attached.</p>}
            </article>
          ) : null}
        </section>
      </div>
    </div>
  );
}

export default function AdminAgentsPage() {
  return <AdminGate><AdminAgentsContent /></AdminGate>;
}

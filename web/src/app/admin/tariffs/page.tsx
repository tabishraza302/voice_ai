"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { RefreshCw } from "lucide-react";
import { AdminGate } from "@/components/AdminGate";
import { useAuth } from "@/lib/auth-state";
import { PlatformApiError, platformApi } from "@/lib/platform-api-client";
import { formatMinorCurrency, formatPlatformDate } from "@/lib/platform-format";
import type { AdminAdmissionPauseItem, AdminTariffPage } from "@/types/platform";

function AdminTariffsContent() {
  const { executeWithStepUp } = useAuth();
  const [tariffs, setTariffs] = useState<AdminTariffPage | null>(null);
  const [pauses, setPauses] = useState<AdminAdmissionPauseItem[]>([]);
  const [pauseCursor, setPauseCursor] = useState<string | null>(null);
  const [globalPause, setGlobalPause] = useState<AdminAdmissionPauseItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMoreTariffs, setLoadingMoreTariffs] = useState(false);
  const [loadingMorePauses, setLoadingMorePauses] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [rate, setRate] = useState("");
  const [tariffReason, setTariffReason] = useState("");
  const [scope, setScope] = useState<"global" | "user" | "agent">("global");
  const [targetId, setTargetId] = useState("");
  const [paused, setPaused] = useState(true);
  const [pauseReason, setPauseReason] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [tariffPage, pausePage, globalPage] = await Promise.all([
        platformApi.adminGetTariffs({ limit: 50 }),
        platformApi.adminGetAdmissionPauses({ limit: 100 }),
        platformApi.adminGetAdmissionPauses({ scope: "global", limit: 1 }),
      ]);
      setTariffs(tariffPage);
      setPauses(pausePage.items);
      setPauseCursor(pausePage.next_cursor);
      setGlobalPause(globalPage.items[0] ?? null);
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not load tariff and admission policy records.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  const loadMoreTariffs = async () => {
    if (!tariffs?.next_cursor || loadingMoreTariffs) return;
    setLoadingMoreTariffs(true);
    setError(null);
    try {
      const nextPage = await platformApi.adminGetTariffs({
        cursor: tariffs.next_cursor,
        limit: 50,
      });
      setTariffs((current) => current
        ? {
            ...nextPage,
            current: current.current,
            items: [...current.items, ...nextPage.items],
          }
        : nextPage);
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not load more tariff history.");
    } finally {
      setLoadingMoreTariffs(false);
    }
  };

  const loadMorePauses = async () => {
    if (!pauseCursor || loadingMorePauses) return;
    setLoadingMorePauses(true);
    setError(null);
    try {
      const nextPage = await platformApi.adminGetAdmissionPauses({
        cursor: pauseCursor,
        limit: 100,
      });
      setPauses((current) => [...current, ...nextPage.items]);
      setPauseCursor(nextPage.next_cursor);
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not load more admission policies.");
    } finally {
      setLoadingMorePauses(false);
    }
  };

  const createTariff = async (event: FormEvent) => {
    event.preventDefault();
    if (!tariffs || actionLoading) return;
    const rateMinor = Number(rate);
    const reason = tariffReason.trim();
    if (!Number.isSafeInteger(rateMinor) || rateMinor < 0) {
      setError("Rate must be a nonnegative whole number of paise per minute.");
      return;
    }
    if (reason.length < 10 || reason.length > 500) {
      setError("Tariff change reason must contain 10–500 characters.");
      return;
    }
    setActionLoading(true);
    setError(null);
    setMessage(null);
    try {
      const version = await executeWithStepUp(() => platformApi.adminCreateTariffVersion(
        {
          expected_revision: tariffs.current.revision,
          rate_minor_per_minute: rateMinor,
          reason,
        },
        crypto.randomUUID()
      ));
      setMessage(`Tariff ${version.tariff_version} is active for new sessions. Existing calls keep their pinned rate.`);
      setRate("");
      setTariffReason("");
      await refresh();
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not create a tariff version.");
    } finally {
      setActionLoading(false);
    }
  };

  const setAdmissionPolicy = async (event: FormEvent) => {
    event.preventDefault();
    if (actionLoading) return;
    const target = targetId.trim();
    const reason = pauseReason.trim();
    if (scope !== "global" && !target) {
      setError("Enter the exact user or agent ID for this policy.");
      return;
    }
    if (reason.length < 10 || reason.length > 500) {
      setError("Admission policy reason must contain 10–500 characters.");
      return;
    }
    setActionLoading(true);
    setError(null);
    setMessage(null);
    try {
      const ownerUserId = scope === "user" ? target : undefined;
      const agentId = scope === "agent" ? target : undefined;
      const currentPage = await platformApi.adminGetAdmissionPauses({
        scope,
        owner_user_id: ownerUserId,
        agent_id: agentId,
        limit: 1,
      });
      const current = currentPage.items[0];
      const updated = await executeWithStepUp(() => platformApi.adminSetAdmissionPause(
        {
          scope,
          owner_user_id: ownerUserId ?? null,
          agent_id: agentId ?? null,
          paused,
          expected_revision: current?.revision ?? 0,
          reason,
        },
        crypto.randomUUID()
      ));
      setMessage(`${scope === "global" ? "Global" : scope === "user" ? "User" : "Agent"} admission is ${updated.paused ? "paused" : "open"}.`);
      setPauseReason("");
      await refresh();
    } catch (err: unknown) {
      setError(err instanceof PlatformApiError ? err.message : "Could not update admission policy.");
    } finally {
      setActionLoading(false);
    }
  };

  const activeGlobalPause = globalPause;

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-ink-800 pb-6">
        <div>
          <p className="text-sm font-medium text-copper-300">Administration</p>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight text-limestone-100">Rates and call controls</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">Tariff changes apply only to new calls. Admission pauses are persisted, scoped, audited, and checked by owner and guest admission.</p>
        </div>
        <button className="button-secondary" onClick={() => void refresh()} disabled={loading}><RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />Refresh</button>
      </header>

      {error ? <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">{error}</div> : null}
      {message ? <div role="status" className="surface border-moss-800 bg-moss-950/30 p-4 text-sm text-moss-200">{message}</div> : null}

      <section aria-labelledby="active-tariff-title" className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(20rem,0.8fr)]">
        <article className="surface p-5">
          <h2 id="active-tariff-title" className="text-lg font-semibold text-limestone-100">Active tariff</h2>
          {loading && !tariffs ? <p role="status" className="mt-4 text-sm text-limestone-400">Loading active tariff…</p> : tariffs ? (
            <>
              <p className="mt-4 text-3xl font-semibold tabular-nums text-limestone-100">{formatMinorCurrency(tariffs.current.rate_minor_per_minute)}<span className="ml-2 text-sm font-normal text-limestone-400">per minute</span></p>
              <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
                <div><dt className="text-xs text-limestone-500">Tariff version</dt><dd className="mt-1 text-limestone-200">{tariffs.current.tariff_version}</dd></div>
                <div><dt className="text-xs text-limestone-500">Revision</dt><dd className="mt-1 text-limestone-200">{tariffs.current.revision}</dd></div>
                <div><dt className="text-xs text-limestone-500">Billing slice</dt><dd className="mt-1 text-limestone-200">{tariffs.current.billing_slice_ms} ms</dd></div>
                <div><dt className="text-xs text-limestone-500">Permit lifetime</dt><dd className="mt-1 text-limestone-200">{tariffs.current.permit_ttl_ms} ms</dd></div>
              </dl>
              <p className="mt-3 text-xs text-limestone-500">Created {formatPlatformDate(tariffs.current.created_at)}. Existing session snapshots do not change when this pointer moves.</p>
            </>
          ) : <p className="mt-4 text-sm text-limestone-400">No active tariff is available.</p>}
        </article>

        <form onSubmit={createTariff} className="surface space-y-3 p-5">
          <div><h2 className="text-lg font-semibold text-limestone-100">Publish a new rate</h2><p className="mt-1 text-sm text-limestone-400">Creates an immutable version and switches the active pointer.</p></div>
          <label className="block text-xs text-limestone-400">Rate (whole paise per minute)<input className="input mt-1 w-full" type="number" min="0" step="1" value={rate} onChange={(event) => setRate(event.target.value)} /></label>
          <p className="text-xs text-limestone-500">{rate && Number.isSafeInteger(Number(rate)) ? `${formatMinorCurrency(Number(rate))} per minute` : ""}</p>
          <label className="block text-xs text-limestone-400">Reason<textarea className="input mt-1 min-h-20 w-full" value={tariffReason} onChange={(event) => setTariffReason(event.target.value)} maxLength={500} /></label>
          <button className="button-primary" type="submit" disabled={!tariffs || actionLoading}>{actionLoading ? "Saving…" : "Activate new tariff"}</button>
        </form>
      </section>

      <section aria-labelledby="tariff-history-title" className="space-y-3">
        <div><h2 id="tariff-history-title" className="text-lg font-semibold text-limestone-100">Immutable rate history</h2><p className="mt-1 text-sm text-limestone-400">Every prior version remains available for calls that pinned it.</p></div>
        <div className="surface overflow-x-auto"><table className="w-full min-w-[44rem] text-left text-sm"><thead className="border-b border-ink-800 text-xs uppercase tracking-wide text-limestone-500"><tr><th className="p-3">Version</th><th className="p-3">Rate / minute</th><th className="p-3">Slice</th><th className="p-3">Permit</th><th className="p-3">State</th><th className="p-3">Created</th></tr></thead><tbody className="divide-y divide-ink-800">{tariffs?.items.map((item) => <tr key={item.id} className="text-limestone-300"><td className="p-3 font-medium text-limestone-100">{item.tariff_version}</td><td className="p-3 tabular-nums">{formatMinorCurrency(item.rate_minor_per_minute)}</td><td className="p-3">{item.billing_slice_ms} ms</td><td className="p-3">{item.permit_ttl_ms} ms</td><td className="p-3">{item.is_current ? "Active" : "Historical"}</td><td className="p-3">{formatPlatformDate(item.created_at)}</td></tr>)}</tbody></table></div>
        {tariffs?.next_cursor ? (
          <button className="button-secondary" onClick={() => void loadMoreTariffs()} disabled={loadingMoreTariffs}>
            {loadingMoreTariffs ? "Loading…" : "Load more tariff versions"}
          </button>
        ) : null}
      </section>

      <section aria-labelledby="admission-title" className="space-y-4">
        <div><h2 id="admission-title" className="text-lg font-semibold text-limestone-100">Admission pause controls</h2><p className="mt-1 text-sm text-limestone-400">Pausing blocks new admissions in the selected scope; it does not terminate active sessions.</p></div>
        {activeGlobalPause?.paused ? <p role="status" className="surface border-copper-900 bg-copper-950/30 p-4 text-sm text-copper-200">Global admission pause is active.</p> : null}
        <div className="grid gap-6 xl:grid-cols-[minmax(20rem,0.8fr)_minmax(0,1.2fr)]">
          <form onSubmit={setAdmissionPolicy} className="surface space-y-3 p-5">
            <h3 className="font-medium text-limestone-100">Set a pause policy</h3>
            <label className="block text-xs text-limestone-400">Scope<select className="input mt-1 w-full" value={scope} onChange={(event) => setScope(event.target.value as "global" | "user" | "agent")}><option value="global">All new calls</option><option value="user">One owner</option><option value="agent">One agent</option></select></label>
            {scope !== "global" ? <label className="block text-xs text-limestone-400">{scope === "user" ? "Owner user ID" : "Agent ID"}<input className="input mt-1 w-full" value={targetId} onChange={(event) => setTargetId(event.target.value)} placeholder="UUID" /></label> : null}
            <label className="flex items-center gap-2 text-sm text-limestone-300"><input type="checkbox" checked={paused} onChange={(event) => setPaused(event.target.checked)} />Pause new admissions in this scope</label>
            <label className="block text-xs text-limestone-400">Reason<textarea className="input mt-1 min-h-20 w-full" value={pauseReason} onChange={(event) => setPauseReason(event.target.value)} maxLength={500} /></label>
            <button className="button-primary" type="submit" disabled={actionLoading}>{actionLoading ? "Saving…" : "Save admission policy"}</button>
          </form>
          <div className="surface divide-y divide-ink-800">
            <div className="p-4"><h3 className="font-medium text-limestone-100">Saved pause policies</h3><p className="mt-1 text-xs text-limestone-500">A policy remains in the audit history when admission is reopened.</p></div>
            {pauses.map((policy) => <article key={policy.id} className="p-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><p className="font-medium capitalize text-limestone-100">{policy.scope} · {policy.paused ? "Paused" : "Open"}</p><p className="mt-1 break-all text-xs text-limestone-400">{policy.owner_user_id ?? policy.agent_id ?? "All owners and agents"}</p></div><span className="text-xs text-limestone-500">Revision {policy.revision}</span></div><p className="mt-2 text-sm text-limestone-300">{policy.reason}</p><p className="mt-1 text-xs text-limestone-500">Updated {formatPlatformDate(policy.updated_at)} · admin {policy.updated_by_user_id}</p></article>)}
            {!loading && pauses.length === 0 ? <p className="p-4 text-sm text-limestone-400">No pause policy has been saved.</p> : null}
            {pauseCursor ? (
              <button className="button-secondary m-4" onClick={() => void loadMorePauses()} disabled={loadingMorePauses}>
                {loadingMorePauses ? "Loading…" : "Load more pause policies"}
              </button>
            ) : null}
          </div>
        </div>
      </section>
    </div>
  );
}

export default function AdminTariffsPage() {
  return <AdminGate><AdminTariffsContent /></AdminGate>;
}

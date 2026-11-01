"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Copy,
  Plus,
  RefreshCw,
  SlidersHorizontal,
  Trash2,
} from "lucide-react";
import {
  platformAgentApi,
  type AgentView,
} from "@/lib/platform-agent-client";
import {
  getVisibleAgents,
  type AgentListQuery,
} from "@/lib/platform-agent-model";

function runtimeSummary(agent: AgentView) {
  const settings = agent.runtime_settings;
  if (!settings) return null;
  const textOnly = !settings.tts_voice_en && !settings.tts_voice_hi;
  return {
    llmModel: settings.llm_model,
    sttProvider: settings.stt_provider,
    voices: textOnly
      ? "Text only"
      : `${settings.tts_voice_en || "Not set"} / ${settings.tts_voice_hi || "Not set"}`,
  };
}

export function CreateAgentMenu() {
  return (
    <details className="group relative">
      <summary className="button-primary list-none cursor-pointer select-none [&::-webkit-details-marker]:hidden">
        <Plus className="h-4 w-4" />
        Create agent
        <span aria-hidden="true" className="ml-1 text-xs">▾</span>
      </summary>
      <div className="absolute right-0 z-30 mt-2 w-60 rounded-xl border border-ink-700 bg-ink-950 p-2 shadow-lg shadow-black/30">
        <Link href="/agents/new?mode=generate" className="button-secondary w-full justify-start">
          Generate from prompt
        </Link>
        <Link href="/agents/new?mode=manual" className="button-secondary mt-1 w-full justify-start">
          Create manually
        </Link>
      </div>
    </details>
  );
}

interface PlatformAgentDirectoryProps {
  compact?: boolean;
}

interface PendingDuplicate {
  idempotencyKey: string;
  request: { name: string; source_revision_or_version: "draft" };
}

function getPendingDuplicate(agent: AgentView): PendingDuplicate {
  const storageKey = `platform-agent-duplicate:${agent.id}`;
  const saved = window.sessionStorage.getItem(storageKey);
  if (saved !== null) {
    const parsed: unknown = JSON.parse(saved);
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      !("idempotencyKey" in parsed) ||
      typeof parsed.idempotencyKey !== "string" ||
      !("request" in parsed) ||
      typeof parsed.request !== "object" ||
      parsed.request === null ||
      !("name" in parsed.request) ||
      typeof parsed.request.name !== "string" ||
      !("source_revision_or_version" in parsed.request) ||
      parsed.request.source_revision_or_version !== "draft"
    ) {
      throw new Error("The saved duplicate retry is invalid. Clear this browser session before trying again.");
    }
    return {
      idempotencyKey: parsed.idempotencyKey,
      request: {
        name: parsed.request.name,
        source_revision_or_version: "draft",
      },
    };
  }
  if (!globalThis.crypto?.randomUUID) {
    throw new Error("Secure idempotency keys are unavailable in this browser.");
  }
  const operation: PendingDuplicate = {
    idempotencyKey: globalThis.crypto.randomUUID(),
    request: {
      name: `Copy of ${agent.name}`,
      source_revision_or_version: "draft",
    },
  };
  window.sessionStorage.setItem(storageKey, JSON.stringify(operation));
  return operation;
}

function clearPendingDuplicate(agentId: string): void {
  try {
    window.sessionStorage.removeItem(`platform-agent-duplicate:${agentId}`);
  } catch {
    // The operation has succeeded; a retained key is safe to replay.
  }
}
const PAGE_SIZE = 12;

function displayError(error: unknown): string {
  return error instanceof Error ? error.message : "The platform request failed.";
}

function displayDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Date unavailable"
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
}

function defaultQuery(compact: boolean): AgentListQuery {
  return {
    query: "",
    enabled: "all",
    publication: "all",
    sort: "updated",
    page: 1,
    pageSize: compact ? 5 : PAGE_SIZE,
  };
}

export function PlatformAgentDirectory({ compact = false }: PlatformAgentDirectoryProps) {
  const [agents, setAgents] = useState<AgentView[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [query, setQuery] = useState<AgentListQuery>(() => defaultQuery(compact));
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setLoadError(null);
    platformAgentApi.listAllAgents().then((result) => {
      if (current) setAgents(result);
    }).catch((error: unknown) => {
      if (current) setLoadError(displayError(error));
    }).finally(() => {
      if (current) setLoading(false);
    });
    return () => { current = false; };
  }, [reloadKey]);

  const visible = useMemo(
    () => getVisibleAgents(agents, query),
    [agents, query]
  );

  const updateQuery = (updates: Partial<AgentListQuery>) => {
    setQuery((current) => ({ ...current, ...updates, page: 1 }));
  };

  const retryLoad = () => setReloadKey((current) => current + 1);

  const handleEnabledChange = async (agent: AgentView) => {
    const action = agent.enabled ? "disable" : "enable";
    setActionLoading(`${action}-${agent.id}`);
    setActionError(null);
    try {
      const updated = await platformAgentApi.setEnabled(agent.id, !agent.enabled);
      setAgents((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (error: unknown) {
      setActionError(displayError(error));
    } finally {
      setActionLoading(null);
    }
  };

  const handleDuplicate = async (agent: AgentView) => {
    setActionLoading(`duplicate-${agent.id}`);
    setActionError(null);
    try {
      const operation = getPendingDuplicate(agent);
      const duplicate = await platformAgentApi.duplicateAgent(agent.id, operation.request, operation.idempotencyKey);
      setAgents((current) => current.some((item) => item.id === duplicate.id) ? current : [...current, duplicate]);
      clearPendingDuplicate(agent.id);
    } catch (error: unknown) {
      setActionError(displayError(error));
    } finally {
      setActionLoading(null);
    }
  };

  const handleDelete = async (agent: AgentView) => {
    const confirmation = window.prompt(`Type “${agent.name}” to permanently delete this agent.`);
    if (confirmation !== agent.name) return;
    setActionLoading(`delete-${agent.id}`);
    setActionError(null);
    try {
      await platformAgentApi.deleteAgent(agent.id);
      setAgents((current) => current.filter((item) => item.id !== agent.id));
    } catch (error: unknown) {
      setActionError(displayError(error));
    } finally {
      setActionLoading(null);
    }
  };

  const heading = compact ? "Your agents" : "Agents";
  const firstFive = compact ? visible.items : [];

  return (
    <section aria-labelledby="platform-agents-title" className="space-y-4">
      <header className="flex flex-col gap-4 border-b border-ink-800 pb-5 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 id="platform-agents-title" className={compact ? "text-xl font-semibold text-limestone-100" : "text-3xl font-semibold tracking-tight text-limestone-100"}>
            {heading}
          </h2>
          <p className="mt-1 text-sm text-limestone-400">
            {compact
              ? "Most recently updated agents."
              : "Search, configure, test, and manage your agents."}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {compact ? (
            <Link href="/agents" className="button-secondary">
              View all agents <ArrowRight className="h-4 w-4" />
            </Link>
          ) : null}
          <CreateAgentMenu />
        </div>
      </header>

      {loadError ? (
        <div role="alert" className="surface flex flex-wrap items-center justify-between gap-3 p-4 text-sm text-rust-200">
          <span>Could not load your agents: {loadError}</span>
          <button className="button-secondary" onClick={retryLoad}>Retry</button>
        </div>
      ) : null}
      {actionError ? (
        <div role="alert" className="surface flex flex-wrap items-center justify-between gap-3 p-4 text-sm text-rust-200">
          <span>{actionError}</span>
          <button className="button-secondary" onClick={() => setActionError(null)}>Dismiss</button>
        </div>
      ) : null}

      {!compact ? (
        <div className="grid gap-3 sm:grid-cols-[minmax(16rem,1fr)_12rem_12rem_12rem]">
          <div>
            <label className="field-label" htmlFor="agent-search">Search agents</label>
            <input
              id="agent-search"
              className="field-control"
              type="search"
              value={query.query}
              onChange={(event) => updateQuery({ query: event.currentTarget.value })}
              placeholder="Name or purpose"
            />
          </div>
          <div>
            <label className="field-label" htmlFor="agent-enabled-filter">Admission status</label>
            <select
              id="agent-enabled-filter"
              className="field-control"
              value={query.enabled}
              onChange={(event) => updateQuery({ enabled: event.currentTarget.value as AgentListQuery["enabled"] })}
            >
              <option value="all">All agents</option>
              <option value="enabled">Enabled</option>
              <option value="disabled">Disabled</option>
            </select>
          </div>
          <div>
            <label className="field-label" htmlFor="agent-publication-filter">Publication</label>
            <select
              id="agent-publication-filter"
              className="field-control"
              value={query.publication}
              onChange={(event) => updateQuery({ publication: event.currentTarget.value as AgentListQuery["publication"] })}
            >
              <option value="all">Any publication</option>
              <option value="published">Published</option>
              <option value="unpublished">Not published</option>
            </select>
          </div>
          <div>
            <label className="field-label" htmlFor="agent-sort">Sort by</label>
            <select
              id="agent-sort"
              className="field-control"
              value={query.sort}
              onChange={(event) => updateQuery({ sort: event.currentTarget.value as AgentListQuery["sort"] })}
            >
              <option value="updated">Recently updated</option>
              <option value="name">Name A–Z</option>
            </select>
          </div>
        </div>
      ) : null}

      {loading ? (
        <div role="status" className="surface flex items-center gap-3 p-5 text-sm text-limestone-400">
          <RefreshCw className="h-4 w-4 animate-spin text-copper-400" />Loading agents…
        </div>
      ) : !loadError && agents.length === 0 ? (
        <div className="surface flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="font-medium text-limestone-100">No agents yet</h3>
            <p className="mt-1 text-sm text-limestone-400">Create a draft, configure its flow, then test it in this workspace.</p>
          </div>
          {!compact ? <CreateAgentMenu /> : null}
        </div>
      ) : !loadError && visible.totalItems === 0 ? (
        <div className="surface flex flex-col gap-3 p-5 text-sm text-limestone-300">
          <h3 className="font-medium text-limestone-100">No agents match those filters</h3>
          <button
            type="button"
            className="button-secondary self-start"
            onClick={() => setQuery(defaultQuery(false))}
          >
            Clear search and filters
          </button>
        </div>
      ) : !loadError ? (
        <div className="surface divide-y divide-ink-800">
          {firstFive.length > 0 ? firstFive.map((agent) => (
            <AgentListRow
              key={agent.id}
              agent={agent}
              summary={runtimeSummary(agent)}
              compact
            />
          )) : visible.items.map((agent) => (
            <AgentListRow
              key={agent.id}
              agent={agent}
              summary={runtimeSummary(agent)}
              actionLoading={actionLoading}
              onEnabledChange={() => void handleEnabledChange(agent)}
              onDuplicate={() => void handleDuplicate(agent)}
              onDelete={() => void handleDelete(agent)}
            />
          ))}
        </div>
      ) : null}

      {!compact && !loading && visible.totalItems > 0 ? (
        <nav aria-label="Agent pages" className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-limestone-400">
            {visible.totalItems} {visible.totalItems === 1 ? "agent" : "agents"}
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="button-secondary"
              aria-label="Previous agent page"
              disabled={visible.page <= 1}
              onClick={() => setQuery((current) => ({ ...current, page: visible.page - 1 }))}
            >
              <ArrowLeft className="h-4 w-4" />Previous
            </button>
            <span className="px-2 text-sm tabular-nums text-limestone-300" aria-live="polite">
              Page {visible.page} of {visible.pageCount}
            </span>
            <button
              type="button"
              className="button-secondary"
              aria-label="Next agent page"
              disabled={visible.page >= visible.pageCount}
              onClick={() => setQuery((current) => ({ ...current, page: visible.page + 1 }))}
            >
              Next<ArrowRight className="h-4 w-4" />
            </button>
          </div>
        </nav>
      ) : null}
    </section>
  );
}

interface AgentListRowProps {
  agent: AgentView;
  summary?: { llmModel: string; sttProvider: string; voices: string } | null;
  compact?: boolean;
  actionLoading?: string | null;
  onEnabledChange?: () => void;
  onDuplicate?: () => void;
  onDelete?: () => void;
}

function settingLabel(value: string | null | undefined): string {
  return value ?? "Platform default";
}

function AgentListRow({
  agent,
  summary,
  compact = false,
  actionLoading,
  onEnabledChange,
  onDuplicate,
  onDelete,
}: AgentListRowProps) {
  return (
    <article className="grid gap-4 p-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:px-5">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="truncate text-base font-semibold text-limestone-100">{agent.name}</h3>
          <span className={`rounded-md border px-2 py-1 text-xs font-medium ${agent.enabled ? "border-moss-700 bg-moss-950 text-moss-200" : "border-ink-700 text-limestone-400"}`}>
            {agent.enabled ? "Enabled" : "Disabled"}
          </span>
          <span className="rounded-md border border-ink-700 px-2 py-1 text-xs text-limestone-300">
            Draft r{agent.draft_revision}
          </span>
          {agent.published_version_id ? (
            <span className="rounded-md border border-copper-700 bg-copper-950/40 px-2 py-1 text-xs text-copper-200">Published</span>
          ) : (
            <span className="rounded-md border border-ink-700 px-2 py-1 text-xs text-limestone-400">Not published</span>
          )}
        </div>
        <p className="mt-1 max-w-3xl text-sm text-limestone-400">{agent.description || "No purpose provided."}</p>
        <dl className="mt-3 grid gap-x-5 gap-y-1 text-xs sm:grid-cols-3">
          <div>
            <dt className="text-limestone-500">Runtime model</dt>
            <dd className="mt-0.5 break-all text-limestone-200">
              {summary ? settingLabel(summary.llmModel) : "Settings unavailable"}
            </dd>
          </div>
          <div>
            <dt className="text-limestone-500">Speech recognition</dt>
            <dd className="mt-0.5 break-all text-limestone-200">
              {summary ? settingLabel(summary.sttProvider) : "Settings unavailable"}
            </dd>
          </div>
          <div>
            <dt className="text-limestone-500">English / Hindi voices</dt>
            <dd className="mt-0.5 break-all text-limestone-200">
              {summary ? summary.voices : "Settings unavailable"}
            </dd>
          </div>
        </dl>
        <p className="mt-2 text-xs text-limestone-500">
          Updated <time dateTime={agent.updated_at}>{displayDate(agent.updated_at)}</time>
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {compact ? (
          <>
            <Link href={`/agents/${agent.id}`} className="button-secondary">
              <SlidersHorizontal className="h-4 w-4" />Configure
            </Link>
            <Link href={`/agents/${agent.id}?tab=test`} className="button-primary">
              Test
            </Link>
          </>
        ) : (
          <>
            <button
              type="button"
              className="button-secondary"
              onClick={onEnabledChange}
              disabled={actionLoading !== null}
            >
              <Check className="h-4 w-4" />
              {actionLoading === `enable-${agent.id}` ? "Enabling…" : actionLoading === `disable-${agent.id}` ? "Disabling…" : agent.enabled ? "Disable" : "Enable"}
            </button>
            <Link href={`/agents/${agent.id}`} className="button-secondary">
              <SlidersHorizontal className="h-4 w-4" />Configure
            </Link>
            <Link href={`/agents/${agent.id}?tab=test`} className="button-secondary">Test</Link>
            <button
              type="button"
              className="button-secondary px-3"
              aria-label={`Duplicate ${agent.name}`}
              onClick={onDuplicate}
              disabled={actionLoading !== null}
            >
              <Copy className="h-4 w-4" />
              {actionLoading === `duplicate-${agent.id}` ? <span className="sr-only">Duplicating…</span> : null}
            </button>
            <button
              type="button"
              className="button-secondary px-3 text-rust-300"
              aria-label={`Delete ${agent.name}`}
              onClick={onDelete}
              disabled={actionLoading !== null}
            >
              <Trash2 className="h-4 w-4" />
              {actionLoading === `delete-${agent.id}` ? <span className="sr-only">Deleting…</span> : null}
            </button>
          </>
        )}
      </div>
    </article>
  );
}

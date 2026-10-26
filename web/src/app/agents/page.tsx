"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AlertCircle, Bot, Check, Copy, Plus, RefreshCw, SlidersHorizontal, Trash2 } from "lucide-react";
import { api } from "@/lib/api-client";
import type { Agent } from "@/types/agent";

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const requestIdRef = useRef(0);
  const mountedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const loadAgents = async () => {
    const requestId = ++requestIdRef.current;
    const isStale = () => requestId !== requestIdRef.current || !mountedRef.current;
    setLoading(true);
    setError(null);
    try {
      const fetched = await api.getAgents();
      if (isStale()) return;
      setAgents(fetched);
    } catch (err: unknown) {
      if (isStale()) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!isStale()) setLoading(false);
    }
  };

  useEffect(() => { void loadAgents(); }, []);

  const handleActivate = async (id: string) => {
    setActionLoading(`activate-${id}`);
    setError(null);
    try {
      const updated = await api.activateAgent(id);
      setAgents((prev) => prev.map((agent) => ({ ...agent, is_active: agent.id === updated.id })));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not select this agent.");
    } finally {
      setActionLoading(null);
    }
  };

  const handleClone = async (agent: Agent) => {
    setActionLoading(`clone-${agent.id}`);
    setError(null);
    try {
      const cloned = await api.cloneAgent(agent.id);
      setAgents((prev) => [...prev, cloned]);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not duplicate this agent.");
    } finally {
      setActionLoading(null);
    }
  };

  const handleDelete = async (agent: Agent) => {
    if (!window.confirm(`Delete “${agent.name}”? This cannot be undone.`)) return;
    setActionLoading(`delete-${agent.id}`);
    setError(null);
    try {
      await api.deleteAgent(agent.id);
      setAgents((prev) => prev.filter((item) => item.id !== agent.id));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not delete this agent.");
    } finally {
      setActionLoading(null);
    }
  };

  return (
    <div className="space-y-7">
      <header className="flex flex-col gap-5 border-b border-ink-800 pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-limestone-100">Agents</h1>
          <p className="mt-2 text-sm text-limestone-400">Select the agent used for new sessions or update its configuration.</p>
        </div>
        <Link href="/agents/new" className="button-primary self-start"><Plus className="h-4 w-4" />New agent</Link>
      </header>

      {error && <div role="alert" className="surface flex flex-wrap items-center justify-between gap-3 border-rust-800 p-4 text-sm text-rust-200"><span className="flex items-center gap-2"><AlertCircle className="h-4 w-4 shrink-0" />{error}</span><button className="button-secondary" onClick={() => void loadAgents()}>Retry</button></div>}

      {loading ? (
        <div className="surface flex items-center gap-3 p-5 text-sm text-limestone-400" role="status"><RefreshCw className="h-4 w-4 animate-spin text-copper-400" />Loading agents…</div>
      ) : agents.length === 0 ? (
        <div className="surface flex flex-col items-start gap-4 p-6 sm:flex-row sm:items-center sm:justify-between">
          <div><h2 className="text-lg font-medium text-limestone-100">No agents yet</h2><p className="mt-1 text-sm text-limestone-400">Create an agent to configure its prompts, model, and bilingual voices.</p></div>
          <Link href="/agents/new" className="button-primary"><Plus className="h-4 w-4" />Create agent</Link>
        </div>
      ) : (
        <section aria-label="Agent configurations" className="surface divide-y divide-ink-800">
          {agents.map((agent) => (
            <article key={agent.id} className="grid gap-4 p-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:px-5">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="truncate text-base font-semibold text-limestone-100">{agent.name}</h2>
                  {agent.is_active ? <span className="rounded-md border border-moss-700 bg-moss-950 px-2 py-1 text-xs font-medium text-moss-200">Selected</span> : <span className="rounded-md border border-ink-700 px-2 py-1 text-xs text-limestone-400">Standby</span>}
                  {agent.is_preset && <span className="rounded-md border border-ink-700 px-2 py-1 text-xs text-limestone-400">Preset</span>}
                </div>
                <p className="mt-1 line-clamp-1 text-sm text-limestone-400">{agent.description || "No description provided."}</p>
                <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs">
                  <div className="flex gap-1.5"><dt className="text-limestone-500">Model</dt><dd className="text-limestone-200">{agent.llm_model}</dd></div>
                  <div className="flex gap-1.5"><dt className="text-limestone-500">Voices</dt><dd className="text-limestone-200">{agent.tts_voice_en} / {agent.tts_voice_hi}</dd></div>
                  <div className="flex gap-1.5"><dt className="text-limestone-500">Speed</dt><dd className="text-limestone-200">{agent.tts_speed}×</dd></div>
                </dl>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {!agent.is_active && <button onClick={() => void handleActivate(agent.id)} disabled={actionLoading === `activate-${agent.id}`} className="button-primary"><Check className="h-4 w-4" />{actionLoading === `activate-${agent.id}` ? "Selecting…" : "Select"}</button>}
                <Link href={`/agents/${agent.id}`} className="button-secondary"><SlidersHorizontal className="h-4 w-4" />Edit</Link>
                <button aria-label={`Duplicate ${agent.name}`} title="Duplicate agent" onClick={() => void handleClone(agent)} disabled={actionLoading === `clone-${agent.id}`} className="button-secondary px-3"><Copy className="h-4 w-4" />{actionLoading === `clone-${agent.id}` ? <span className="sr-only">Duplicating…</span> : null}</button>
                {!agent.is_preset && <button aria-label={`Delete ${agent.name}`} title="Delete agent" onClick={() => void handleDelete(agent)} disabled={actionLoading === `delete-${agent.id}`} className="button-secondary px-3 text-rust-300"><Trash2 className="h-4 w-4" />{actionLoading === `delete-${agent.id}` ? <span className="sr-only">Deleting…</span> : null}</button>}
              </div>
            </article>
          ))}
        </section>
      )}
    </div>
  );
}

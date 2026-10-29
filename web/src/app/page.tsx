"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowRight, Bot, Mic, RefreshCw, ScrollText, SlidersHorizontal } from "lucide-react";
import { api } from "@/lib/api-client";
import type { Agent } from "@/types/agent";
import { useAuth } from "@/lib/auth-state";
import { PlatformCustomerDashboard } from "@/components/PlatformCustomerDashboard";

export default function OverviewPage() {
  const { isLocalMode } = useAuth();
  return isLocalMode ? <LocalOverviewPage /> : <PlatformCustomerDashboard />;
}

function LocalOverviewPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    api.getAgents().then((result) => {
      if (current) setAgents(result);
    }).catch((err: unknown) => {
      if (current) setError(err instanceof Error ? err.message : String(err));
    }).finally(() => {
      if (current) setLoading(false);
    });
    return () => { current = false; };
  }, []);

  const selectedAgent = agents.find((agent) => agent.is_active) || null;

  return (
    <div className="space-y-8">
      <header className="flex flex-col gap-5 border-b border-ink-800 pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-limestone-100">Overview</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">
            Manage local voice agents, their shared prompt, and test sessions.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link href="/agents/new" className="button-primary"><Bot className="h-4 w-4" />New agent</Link>
          <Link href="/playground" className="button-secondary"><Mic className="h-4 w-4" />Voice test</Link>
        </div>
      </header>

      {error && (
        <div role="alert" className="surface flex items-center justify-between gap-4 border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">
          <span>Could not load agents: {error}</span>
          <button className="button-secondary" onClick={() => window.location.reload()}>Retry</button>
        </div>
      )}

      <section aria-labelledby="selected-agent-title">
        <h2 id="selected-agent-title" className="mb-3 text-lg font-semibold text-limestone-100">Selected for new sessions</h2>
        {loading ? (
          <div className="surface flex items-center gap-3 p-5 text-sm text-limestone-400" role="status">
            <RefreshCw className="h-4 w-4 animate-spin text-copper-400" />Loading agent configuration…
          </div>
        ) : selectedAgent ? (
          <div className="surface divide-y divide-ink-800">
            <div className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="truncate text-xl font-semibold text-limestone-100">{selectedAgent.name}</h3>
                  <span className="rounded-md border border-moss-700 bg-moss-950 px-2 py-1 text-xs font-medium text-moss-200">Selected</span>
                  {selectedAgent.is_preset && <span className="rounded-md border border-ink-700 px-2 py-1 text-xs text-limestone-300">Preset</span>}
                </div>
                <p className="mt-1 max-w-2xl text-sm text-limestone-400">{selectedAgent.description || "No description provided."}</p>
              </div>
              <div className="flex shrink-0 gap-2">
                <Link href={`/agents/${selectedAgent.id}`} className="button-secondary"><SlidersHorizontal className="h-4 w-4" />Edit</Link>
                <Link href="/playground" className="button-primary"><Mic className="h-4 w-4" />Test voice</Link>
              </div>
            </div>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-4 p-5 text-sm sm:grid-cols-4">
              <div><dt className="text-limestone-500">Model</dt><dd className="mt-1 break-all font-medium text-limestone-200">{selectedAgent.llm_model}</dd></div>
              <div><dt className="text-limestone-500">Speech recognition</dt><dd className="mt-1 break-all font-medium text-limestone-200">{selectedAgent.stt_provider}</dd></div>
              <div><dt className="text-limestone-500">English voice</dt><dd className="mt-1 font-medium text-limestone-200">{selectedAgent.tts_voice_en}</dd></div>
              <div><dt className="text-limestone-500">Hindi voice</dt><dd className="mt-1 font-medium text-limestone-200">{selectedAgent.tts_voice_hi}</dd></div>
            </dl>
          </div>
        ) : (
          <div className="surface flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h3 className="font-medium text-limestone-100">No agent is selected</h3>
              <p className="mt-1 text-sm text-limestone-400">Select an agent from the studio before starting a voice session.</p>
            </div>
            <Link href="/agents" className="button-secondary">Open agent studio<ArrowRight className="h-4 w-4" /></Link>
          </div>
        )}
      </section>

      <section aria-labelledby="workspace-title" className="space-y-3">
        <h2 id="workspace-title" className="text-lg font-semibold text-limestone-100">Workspace</h2>
        <div className="surface divide-y divide-ink-800">
          <Link href="/agents" className="flex items-center justify-between gap-4 p-4 text-sm hover:bg-ink-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-copper-400">
            <span className="flex items-center gap-3"><Bot className="h-4 w-4 text-copper-300" /><span><strong className="font-medium text-limestone-100">Agent studio</strong><span className="mt-0.5 block text-limestone-400">Create, activate, and configure agents</span></span></span>
            <span className="text-limestone-500">{loading ? "—" : agents.length} agents</span>
          </Link>
          <Link href="/base-prompt" className="flex items-center justify-between gap-4 p-4 text-sm hover:bg-ink-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-copper-400">
            <span className="flex items-center gap-3"><ScrollText className="h-4 w-4 text-copper-300" /><span><strong className="font-medium text-limestone-100">Base prompt</strong><span className="mt-0.5 block text-limestone-400">Edit the shared system instructions</span></span></span>
            <ArrowRight className="h-4 w-4 text-limestone-500" />
          </Link>
          <Link href="/playground" className="flex items-center justify-between gap-4 p-4 text-sm hover:bg-ink-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-copper-400">
            <span className="flex items-center gap-3"><Mic className="h-4 w-4 text-copper-300" /><span><strong className="font-medium text-limestone-100">Voice test</strong><span className="mt-0.5 block text-limestone-400">Connect to the local pipeline and talk with the selected agent</span></span></span>
            <ArrowRight className="h-4 w-4 text-limestone-500" />
          </Link>
        </div>
      </section>
    </div>
  );
}

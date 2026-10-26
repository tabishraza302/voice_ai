"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import {
  Mic,
  Bot,
  RefreshCw,
  CheckCircle2,
  AlertCircle,
} from "lucide-react";
import { api } from "@/lib/api-client";
import { VoiceTestModal } from "@/components/VoiceTestModal";
import type { Agent } from "@/types/agent";

export default function PlaygroundPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string>("");
  const [activeAgent, setActiveAgent] = useState<Agent | null>(null);
  const [loading, setLoading] = useState(true);
  const [activating, setActivating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [voiceModalOpen, setVoiceModalOpen] = useState(false);

  useEffect(() => {
    async function loadAgents() {
      setLoading(true);
      setError(null);
      try {
        const list = await api.getAgents();
        setAgents(list);
        const active = list.find((a) => a.is_active) || null;
        if (active) {
          setActiveAgent(active);
          setSelectedAgentId(active.id);
        } else if (list[0]) {
          setSelectedAgentId(list[0].id);
        }
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : String(err);
        setError(
          message ||
            "Unable to connect to backend on port 7860. Verify the bot is running with ./scripts/voice-ai.sh start"
        );
      } finally {
        setLoading(false);
      }
    }
    loadAgents();
  }, []);

  const handleActivate = async () => {
    if (!selectedAgentId) return;
    setActivating(true);
    setError(null);
    try {
      const updated = await api.activateAgent(selectedAgentId);
      setActiveAgent(updated);
      setAgents((prev) =>
        prev.map((a) => ({
          ...a,
          is_active: a.id === updated.id,
        }))
      );
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to activate agent";
      setError(message);
    } finally {
      setActivating(false);
    }
  };

  const selectedAgent = agents.find((a) => a.id === selectedAgentId) || null;

  return (
    <div className="space-y-7">
      <header className="border-b border-ink-800 pb-6">
        <h1 className="text-3xl font-semibold tracking-tight text-limestone-100">Call test</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">
          Select an agent and test its saved configuration through a browser voice session.
        </p>
      </header>

      {error && <div role="alert" className="surface flex items-center gap-3 border-rust-800 bg-rust-950/40 p-4 text-sm text-rust-200"><AlertCircle className="h-4 w-4 shrink-0" />{error}</div>}

      {loading ? (
        <div className="surface flex items-center gap-3 p-5 text-sm text-limestone-400" role="status"><RefreshCw className="h-4 w-4 animate-spin text-copper-400" />Loading agents…</div>
      ) : agents.length === 0 ? (
        <div className="surface flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
          <div><h2 className="font-medium text-limestone-100">No agents available</h2><p className="mt-1 text-sm text-limestone-400">Create an agent before starting a call test.</p></div>
          <Link href="/agents/new" className="button-primary"><Bot className="h-4 w-4" />Create agent</Link>
        </div>
      ) : selectedAgent ? (
        <section className="surface overflow-hidden" aria-labelledby="session-setup-title">
          <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_240px]">
            <div className="space-y-5 p-5 sm:p-6">
              <div>
                <h2 id="session-setup-title" className="text-lg font-semibold text-limestone-100">Session setup</h2>
                <p className="mt-1 text-sm text-limestone-400">Choose the profile to send with this session.</p>
              </div>
              <div>
                <label htmlFor="agent-selection" className="field-label">Agent</label>
                <select id="agent-selection" value={selectedAgentId} onChange={(event) => setSelectedAgentId(event.target.value)} disabled={activating} className="field-control">
                  {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}{agent.is_active ? " · Selected" : ""}</option>)}
                </select>
              </div>
              <div className="border-t border-ink-800 pt-4">
                <h3 className="font-medium text-limestone-100">{selectedAgent.name}</h3>
                <p className="mt-1 text-sm leading-6 text-limestone-400">{selectedAgent.description || "No description provided."}</p>
                <dl className="mt-4 grid grid-cols-2 gap-x-5 gap-y-3 text-sm sm:grid-cols-4">
                  <div><dt className="text-limestone-500">Model</dt><dd className="mt-1 break-all text-limestone-200">{selectedAgent.llm_model}</dd></div>
                  <div><dt className="text-limestone-500">Speech recognition</dt><dd className="mt-1 break-all text-limestone-200">{selectedAgent.stt_provider}</dd></div>
                  <div><dt className="text-limestone-500">English voice</dt><dd className="mt-1 text-limestone-200">{selectedAgent.tts_voice_en}</dd></div>
                  <div><dt className="text-limestone-500">Hindi voice</dt><dd className="mt-1 text-limestone-200">{selectedAgent.tts_voice_hi}</dd></div>
                </dl>
              </div>
            </div>
            <aside className="flex flex-col justify-between gap-4 border-t border-ink-800 bg-ink-950/50 p-5 lg:border-l lg:border-t-0 sm:p-6">
              <div>
                <p className="text-sm font-medium text-limestone-100">New-session default</p>
                <p className="mt-1 text-sm text-limestone-400">{activeAgent ? `Currently ${activeAgent.name}` : "No default agent selected"}</p>
              </div>
              <div className="grid gap-2">
                <button type="button" onClick={() => void handleActivate()} disabled={activating || selectedAgentId === activeAgent?.id} className="button-secondary w-full">
                  {activating ? <RefreshCw className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                  {selectedAgentId === activeAgent?.id ? "Selected" : "Set as default"}
                </button>
                <button type="button" onClick={() => setVoiceModalOpen(true)} className="button-primary w-full"><Mic className="h-4 w-4" />Start voice session</button>
              </div>
              <p className="text-xs leading-5 text-limestone-500">Starting a test requests microphone access. Unsaved editor changes are not used.</p>
            </aside>
          </div>
        </section>
      ) : null}

      {selectedAgent && <VoiceTestModal
        agent={selectedAgent}
        isOpen={voiceModalOpen}
        onClose={() => setVoiceModalOpen(false)}
        onActivated={(activatedAgent) => {
          setActiveAgent(activatedAgent);
          setAgents((current) => current.map((item) => ({ ...item, is_active: item.id === activatedAgent.id })));
        }}
      />}
    </div>
  );
}

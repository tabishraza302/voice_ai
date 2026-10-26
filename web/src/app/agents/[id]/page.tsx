"use client";

import { use, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ArrowLeft, AlertCircle, RefreshCw } from "lucide-react";
import { AgentForm } from "@/components/AgentForm";
import { api } from "@/lib/api-client";
import { describeAgentLoadError, type AgentLoadFailure } from "@/lib/agent-errors";
import type { Agent } from "@/types/agent";

interface PageProps {
  params: Promise<{ id: string }>;
}

export default function EditAgentPage({ params }: PageProps) {
  const resolvedParams = use(params);
  const [agent, setAgent] = useState<Agent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<AgentLoadFailure | null>(null);
  const requestIdRef = useRef(0);
  const mountedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    const requestId = ++requestIdRef.current;
    const isStale = () => requestId !== requestIdRef.current || !mountedRef.current;

    async function loadAgent() {
      setLoading(true);
      setError(null);
      try {
        const fetched = await api.getAgent(resolvedParams.id);
        if (isStale()) return;
        setAgent(fetched);
      } catch (err: unknown) {
        if (isStale()) return;
        setError(describeAgentLoadError(err));
      } finally {
        if (!isStale()) setLoading(false);
      }
    }
    void loadAgent();
  }, [resolvedParams.id]);

  if (loading) {
    return (
      <div className="flex items-center justify-center p-12 text-limestone-400 text-sm">
        <RefreshCw className="w-4 h-4 animate-spin text-copper-400 mr-2" />
        Loading agent configuration...
      </div>
    );
  }

  if (error || !agent) {
    const heading = error?.notFound ? "Agent Not Found" : "Couldn't Load Agent";
    const detail = error
      ? error.notFound
        ? error.detail
        : `Failed to load agent: ${error.detail}`
      : "The requested agent could not be found in the database.";
    return (
      <div className="max-w-xl mx-auto mt-12 p-8 rounded-xl bg-ink-900/60 border border-ink-800 text-center space-y-4">
        <AlertCircle className="w-10 h-10 text-rust-400 mx-auto" />
        <h2 className="text-lg font-bold text-white">{heading}</h2>
        <p className="text-xs text-limestone-400">{detail}</p>
        <Link
          href="/agents"
          className="inline-flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-semibold bg-ink-800 hover:bg-ink-700 text-limestone-200 border border-ink-700"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          Back to Agents
        </Link>
      </div>
    );
  }

  return (
    <div className="max-w-7xl mx-auto animate-in fade-in duration-300">
      <AgentForm initialAgent={agent} isNew={false} />
    </div>
  );
}

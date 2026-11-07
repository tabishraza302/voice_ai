"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ApiError, api, apiErrorText } from "@/lib/api";
import type { Agent, AgentStep, CurrentUser, ProviderCatalog } from "@/lib/types";
import { Button, buttonClassName } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { TabPanel, TabsBar } from "@/components/ui/tabs";
import { cn } from "@/components/ui/utils";
import { OwnerShell } from "@/app/dashboard/owner-shell";
import { AgentDraft, AgentInstructions, AgentPipelineToolbar, agentDraftFromAgent } from "./agent-sections";
import { AgentTesting } from "./agent-testing";
import { agentStepsToCallFlow, type CallFlowStep } from "./call-flow-adapter";
import { CallFlowEditor } from "./call-flow-editor";
import type { CallFlowSaveState } from "./call-flow-save";
import { CompiledPromptView } from "./compiled-prompt-view";
import { useDraftAutosave } from "./use-draft-autosave";
import { ChatPanel } from "@/components/chat-panel";
type EditorTab = "workflow" | "instructions" | "testing" | "history";

const editorTabs: Array<{ value: EditorTab; label: string }> = [
  { value: "workflow", label: "Workflow" },
  { value: "instructions", label: "Instructions" },
  { value: "testing", label: "Testing" },
  { value: "history", label: "History" },
];

function targetForField(field: string): EditorTab {
  if (field.includes("instruction")) return "instructions";
  return "workflow";
}

export default function AgentEditorPage() {
  const params = useParams<{ id: string }>();
  const agentId = params.id;
  const router = useRouter();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [agent, setAgent] = useState<Agent | null>(null);
  const [draft, setDraft] = useState<AgentDraft | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [steps, setSteps] = useState<AgentStep[]>([]);
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<EditorTab>("workflow");
  const [reload, setReload] = useState(0);
  const [draftSaveState, setDraftSaveState] = useState<CallFlowSaveState>({ status: "idle" });
  const [flowSaveState, setFlowSaveState] = useState<CallFlowSaveState>({ status: "idle" });
  const [workflowView, setWorkflowView] = useState<"flow" | "prompt">("flow");
  const [liveFlowSteps, setLiveFlowSteps] = useState<CallFlowStep[]>([]);
  const [flushToken, setFlushToken] = useState(0);
  const { flush: flushDraft } = useDraftAutosave({
    agentId,
    agent,
    draft,
    onServerAgent: setAgent,
    onState: setDraftSaveState,
  });

  const loadCatalog = useCallback(async (signal?: AbortSignal) => {
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const result = await api<ProviderCatalog>("/provider-catalog", { signal });
      if (signal?.aborted) return;
      setCatalog(result);
    } catch (reason) {
      if (signal?.aborted) return;
      if (reason instanceof ApiError && reason.status === 401) {
        router.replace("/login");
        return;
      }
      setCatalog(null);
      setCatalogError(apiErrorText(reason, "Verified provider choices could not be loaded."));
    } finally {
      if (!signal?.aborted) setCatalogLoading(false);
    }
  }, [router]);

  useEffect(() => {
    const controller = new AbortController();
    let current = true;

    async function load() {
      setLoading(true);
      setPageError(null);
      setAgent(null);
      setDraft(null);
      setSteps([]);
      setCatalog(null);
      setCatalogError(null);
      try {
        const currentUser = await api<CurrentUser>("/auth/me", { signal: controller.signal });
        if (!current) return;
        if (currentUser.role === "admin") {
          router.replace("/admin");
          return;
        }
        const [agentResult, stepResult] = await Promise.all([
          api<Agent>(`/agents/${agentId}`, { signal: controller.signal }),
          api<AgentStep[]>(`/agents/${agentId}/steps`, { signal: controller.signal }),
        ]);
        if (!current) return;
        setUser(currentUser);
        setAgent(agentResult);
        setDraft(agentDraftFromAgent(agentResult));
        setSteps(stepResult);
        void loadCatalog(controller.signal);
      } catch (reason) {
        if (!current || controller.signal.aborted) return;
        if (reason instanceof ApiError && reason.status === 401) {
          router.replace("/login");
          return;
        }
        setPageError(apiErrorText(reason, "This agent could not be loaded."));
      } finally {
        if (current) setLoading(false);
      }
    }

    void load();
    return () => {
      current = false;
      controller.abort();
    };
  }, [agentId, loadCatalog, reload, router]);

  function patchDraft(patch: Partial<AgentDraft>) {
    setDraft((current) => (current ? { ...current, ...patch } : current));
  }



  return (
    <OwnerShell active="agents" email={user?.email}>
      <main className={cn("w-full px-6 py-8 sm:px-8 sm:py-10 lg:px-12 transition-[padding] duration-300", chatOpen && "pr-[380px] sm:pr-[380px] lg:pr-[380px]")}>
        {loading ? (
          <div className="space-y-5" aria-busy="true" aria-label="Loading agent editor">
            <div className="h-12 w-1/2 animate-pulse rounded-xl bg-muted" />
            <div className="h-14 animate-pulse rounded-xl border bg-card" />
            <div className="h-80 animate-pulse rounded-2xl border bg-card" />
          </div>
        ) : pageError ? (
          <Card className="space-y-4 p-5 sm:p-6">
            <div role="alert">
              <h1 className="text-xl font-semibold">Agent unavailable</h1>
              <p className="mt-2 text-sm text-muted-foreground">{pageError}</p>
            </div>
            <div className="flex flex-wrap gap-3">
              <Button variant="outline" onClick={() => setReload((value) => value + 1)}>Try again</Button>
              <Link href="/dashboard" className={buttonClassName("outline")}>Back to dashboard</Link>
            </div>
          </Card>
        ) : agent && draft ? (
          <>
            <div className="mt-6 flex flex-col gap-4 border-b pb-6 sm:flex-row sm:items-end sm:justify-between">
              <div className="min-w-0">
                <nav aria-label="Breadcrumb">
                  <ol className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                    <li><Link href="/dashboard#agents" className="underline-offset-4 hover:text-foreground hover:underline">Agents</Link></li>
                    <li aria-hidden="true">/</li>
                    <li aria-current="page" className="max-w-full break-words font-medium text-foreground">{draft.name || agent.name}</li>
                  </ol>
                </nav>
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <input
                    type="text"
                    aria-label="Agent name"
                    maxLength={120}
                    value={draft.name}
                    onChange={(event) => patchDraft({ name: event.currentTarget.value })}
                    className="w-full max-w-xl rounded-lg bg-transparent py-1 text-2xl font-semibold tracking-tight text-foreground transition-colors hover:bg-muted/40 focus:bg-background focus:px-2.5 focus:outline-none focus:ring-2 focus:ring-ring sm:text-3xl"
                    placeholder="Agent name"
                  />
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  onClick={() => {
                    const isBusyOrError =
                      draftSaveState.status === "saving" ||
                      flowSaveState.status === "saving" ||
                      draftSaveState.status === "error" ||
                      flowSaveState.status === "error";
                    if (isBusyOrError) {
                      flushDraft();
                      setFlushToken((token) => token + 1);
                    } else {
                      setChatOpen((prev) => !prev);
                    }
                  }}
                  className={cn(
                    "flex items-center gap-2 rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors",
                    draftSaveState.status === "error" || flowSaveState.status === "error"
                      ? "border-destructive/40 bg-destructive/10 text-destructive hover:bg-destructive/15"
                      : "border-border/60 bg-muted/30 text-muted-foreground hover:bg-muted/50 hover:text-foreground"
                  )}
                  role="status"
                  aria-label={
                    draftSaveState.status === "error" || flowSaveState.status === "error"
                      ? `Error: ${draftSaveState.status === "error" ? draftSaveState.message : flowSaveState.message}. Click to retry.`
                      : draftSaveState.status === "saving" || flowSaveState.status === "saving"
                      ? "Saving changes…"
                      : draftSaveState.status === "saved" || flowSaveState.status === "saved"
                      ? "All changes saved"
                      : "Test chat"
                  }
                >
                  {draftSaveState.status === "saving" || flowSaveState.status === "saving" ? (
                    <>
                      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500" />
                      <span>Saving…</span>
                    </>
                  ) : draftSaveState.status === "error" || flowSaveState.status === "error" ? (
                    <>
                      <span className="h-1.5 w-1.5 rounded-full bg-destructive" />
                      <span>{draftSaveState.status === "error" ? draftSaveState.message : flowSaveState.message || "Couldn't save"}</span>
                      <span className="ml-1 font-semibold underline underline-offset-2">Retry</span>
                    </>
                  ) : draftSaveState.status === "saved" || flowSaveState.status === "saved" ? (
                    <>
                      <span className="h-1.5 w-1.5 rounded-full bg-primary" />
                      <span className="text-primary font-medium">Saved</span>
                    </>
                  ) : (
                    <>
                      <svg className="h-3.5 w-3.5 text-muted-foreground" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                      </svg>
                      <span>Test chat</span>
                    </>
                  )}
                </button>
              </div>
            </div>

            <div className="mt-7">
              <TabsBar id="agent-editor" label="Agent settings" items={editorTabs} value={activeTab} onValueChange={(value) => {
                const tab = editorTabs.find((item) => item.value === value);
                if (tab) setActiveTab(tab.value);
              }} />
              <TabPanel id="agent-editor" value={activeTab} labelledBy={`tab-agent-editor-${activeTab}`} className="pt-6">
                {activeTab === "workflow" ? (
                  <div className="space-y-6">
                    <AgentPipelineToolbar
                      draft={draft}
                      catalog={catalog}
                      catalogError={catalogError}
                      catalogLoading={catalogLoading}
                      onPatch={patchDraft}
                      onRetryCatalog={() => void loadCatalog()}
                    />
                    <div
                      role="group"
                      aria-label="Workflow view"
                      className="inline-flex items-center gap-1 rounded-xl border bg-muted/40 p-1"
                    >
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-pressed={workflowView === "flow"}
                        onClick={() => setWorkflowView("flow")}
                        className={cn(
                          "min-h-8 rounded-lg px-3 text-xs font-medium",
                          workflowView === "flow"
                            ? "bg-background text-foreground shadow-sm"
                            : "text-muted-foreground hover:bg-transparent hover:text-foreground",
                        )}
                      >
                        Visual Flow
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-pressed={workflowView === "prompt"}
                        onClick={() => setWorkflowView("prompt")}
                        className={cn(
                          "min-h-8 rounded-lg px-3 text-xs font-medium",
                          workflowView === "prompt"
                            ? "bg-background text-foreground shadow-sm"
                            : "text-muted-foreground hover:bg-transparent hover:text-foreground",
                        )}
                      >
                        Compiled Prompt
                      </Button>
                    </div>
                    <div className={cn(workflowView !== "flow" && "hidden")}>
                      <CallFlowEditor
                        agentId={agentId}
                        agentName={draft.name || agent.name}
                        steps={steps}
                        onStepsChange={setSteps}
                        onFlowStepsChange={setLiveFlowSteps}
                        onSaveStateChange={setFlowSaveState}
                        flushToken={flushToken}
                      />
                    </div>
                    {workflowView === "prompt" ? (
                      <CompiledPromptView
                        draft={draft}
                        flowSteps={
                          liveFlowSteps.length > 0 ? liveFlowSteps : agentStepsToCallFlow(steps).flowSteps
                        }
                      />
                    ) : null}
                  </div>
                ) : null}
                {activeTab === "instructions" ? <AgentInstructions draft={draft} onPatch={patchDraft} /> : null}
                {activeTab === "testing" || activeTab === "history" ? (
                  <AgentTesting view={activeTab} agentId={agentId} agentName={agent.name} languages={draft.languages} defaultLanguage={draft.default_language} />
                ) : null}
              </TabPanel>
            </div>
          </>
        ) : null}
      </main>
      {agent && draft ? (
        <ChatPanel
          agentId={agentId}
          agentName={draft.name || agent.name}
          languages={draft.languages}
          defaultLanguage={draft.default_language}
          open={chatOpen}
          onClose={() => setChatOpen(false)}
        />
      ) : null}
    </OwnerShell>
  );
}

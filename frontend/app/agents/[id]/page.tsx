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
import { AgentDraft, AgentInstructions, AgentPipelineToolbar, agentDraftFromAgent, draftChanges } from "./agent-sections";
import { AgentTesting } from "./agent-testing";
import { QuestionsEditor } from "./questions-editor";

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
  const [steps, setSteps] = useState<AgentStep[]>([]);
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [activeTab, setActiveTab] = useState<EditorTab>("workflow");
  const [reload, setReload] = useState(0);

  const dirty = useMemo(() => {
    if (!agent || !draft) return false;
    return JSON.stringify(agentDraftFromAgent(agent)) !== JSON.stringify(draft);
  }, [agent, draft]);

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
    setDraft((current) => current ? { ...current, ...patch } : current);
    setSaveError(null);
    setSaveMessage(null);
  }

  async function saveAgent() {
    if (!draft) return;
    setSaving(true);
    setSaveError(null);
    setSaveMessage(null);
    try {
      const updated = await api<Agent>(`/agents/${agentId}`, {
        method: "PATCH",
        body: JSON.stringify(draftChanges(draft)),
      });
      setAgent(updated);
      setDraft(agentDraftFromAgent(updated));
      setSaveMessage("Changes saved.");
    } catch (reason) {
      setSaveError(apiErrorText(reason, "Could not save this agent."));
    } finally {
      setSaving(false);
    }
  }



  return (
    <OwnerShell active="agents" email={user?.email}>
      <main className="mx-auto max-w-7xl px-4 py-8 sm:px-6 sm:py-10 lg:px-8">
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
                {dirty ? <span className="text-sm text-muted-foreground">Unsaved changes</span> : saveMessage ? <span role="status" className="text-sm text-primary">{saveMessage}</span> : null}
                <Button onClick={() => void saveAgent()} disabled={!dirty || saving}>
                  {saving ? "Saving..." : "Save changes"}
                </Button>
              </div>
            </div>
            {saveError ? <p role="alert" className="mt-4 rounded-xl border border-destructive/30 bg-destructive/10 px-3.5 py-3 text-sm text-destructive">{saveError}</p> : null}

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
                    <QuestionsEditor
                      agentId={agentId}
                      languages={draft.languages}
                      steps={steps}
                      published={false}
                      onStepsChange={setSteps}
                    />
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
    </OwnerShell>
  );
}

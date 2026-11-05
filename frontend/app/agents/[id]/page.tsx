"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ApiError, api, apiFieldErrors } from "@/lib/api";
import type { Agent, AgentStep, CurrentUser, ProviderCatalog } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { TabPanel, TabsBar } from "@/components/ui/tabs";
import { OwnerShell } from "@/app/dashboard/owner-shell";
import { AgentDraft, AgentInstructions, AgentLanguages, AgentOverview, AgentVoice, agentDraftFromAgent, draftChanges } from "./agent-sections";
import { AgentTesting } from "./agent-testing";
import { QuestionsEditor } from "./questions-editor";

type EditorTab = "overview" | "voice" | "languages" | "instructions" | "questions" | "testing" | "history" | "publishing";

const editorTabs: Array<{ value: EditorTab; label: string }> = [
  { value: "overview", label: "Overview" },
  { value: "voice", label: "AI and voice" },
  { value: "languages", label: "Languages" },
  { value: "instructions", label: "Instructions" },
  { value: "questions", label: "Questions and logic" },
  { value: "testing", label: "Testing" },
  { value: "history", label: "History" },
  { value: "publishing", label: "Publishing" },
];

function statusVariant(status: Agent["status"]): "muted" | "success" | "warning" {
  if (status === "published") return "success";
  if (status === "suspended") return "warning";
  return "muted";
}

function tabForField(field: string): EditorTab {
  if (field.startsWith("steps.")) return "questions";
  if (field.includes("language")) return "languages";
  if (field.includes("provider") || field.includes("model") || field.includes("voice") || field.startsWith("agent.stt") || field.startsWith("agent.llm") || field.startsWith("agent.tts")) return "voice";
  if (field.includes("instruction")) return "instructions";
  if (field.includes("slug") || field.includes("name") || field.includes("description")) return "overview";
  return "publishing";
}

function errorText(reason: unknown, fallback: string): string {
  if (!(reason instanceof ApiError)) return fallback;
  return reason.status > 0 ? `${reason.message} (HTTP ${reason.status})` : reason.message;
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
  const [activeTab, setActiveTab] = useState<EditorTab>("overview");
  const [reload, setReload] = useState(0);
  const [publishErrors, setPublishErrors] = useState<Record<string, string[]>>({});
  const [publishMessage, setPublishMessage] = useState<string | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

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
      setCatalogError(errorText(reason, "Verified provider choices could not be loaded."));
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
        setPageError(errorText(reason, "This agent could not be loaded."));
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
      setSaveError(errorText(reason, "Could not save this agent."));
    } finally {
      setSaving(false);
    }
  }

  async function changePublishState(action: "publish" | "unpublish") {
    setPublishing(true);
    setPublishErrors({});
    setPublishMessage(null);
    try {
      const updated = await api<Agent>(`/agents/${agentId}/${action}`, { method: "POST" });
      setAgent(updated);
      setDraft(agentDraftFromAgent(updated));
      setPublishMessage(action === "publish" ? "Agent published." : "Agent unpublished.");
    } catch (reason) {
      if (reason instanceof ApiError) {
        setPublishErrors(apiFieldErrors(reason));
        setPublishMessage(reason.status > 0 ? `${reason.message} (HTTP ${reason.status})` : reason.message);
      } else {
        setPublishMessage("Could not update publishing status. Try again.");
      }
    } finally {
      setPublishing(false);
    }
  }

  async function deleteAgent() {
    setDeleting(true);
    setDeleteError(null);
    try {
      await api<void>(`/agents/${agentId}`, { method: "DELETE" });
      router.replace("/dashboard");
    } catch (reason) {
      setDeleteError(errorText(reason, "Could not delete this agent."));
      setDeleting(false);
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
              <Link href="/dashboard" className="inline-flex min-h-11 items-center justify-center rounded-xl border px-4 py-2.5 text-sm font-medium hover:bg-muted">Back to dashboard</Link>
            </div>
          </Card>
        ) : agent && draft ? (
          <>
            <Link href="/dashboard" className="rounded-xl text-sm font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">Back to dashboard</Link>
            <div className="mt-6 flex flex-col gap-4 border-b pb-6 sm:flex-row sm:items-end sm:justify-between">
              <div className="min-w-0">
                <p className="text-sm font-medium text-primary">Agent editor</p>
                <div className="mt-1 flex flex-wrap items-center gap-3">
                  <h1 className="max-w-full break-words text-3xl font-semibold tracking-tight">{agent.name}</h1>
                  <Badge variant={statusVariant(agent.status)}>{agent.status}</Badge>
                </div>
                <p className="mt-2 break-all text-sm text-muted-foreground">/a/{agent.slug}</p>
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
                {activeTab === "overview" ? (
                  <div className="space-y-8">
                    <AgentOverview draft={draft} onPatch={patchDraft} />
                    <Card className="border-destructive/25 p-5 sm:p-6">
                      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                        <div>
                          <h3 className="font-semibold text-destructive">Delete agent</h3>
                          <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">Deleting permanently removes this agent and its saved sessions and answers. Active calls prevent deletion.</p>
                        </div>
                        <Button variant="destructive" onClick={() => setDeleteOpen(true)} disabled={deleting}>Delete agent</Button>
                      </div>
                      {deleteError ? <p role="alert" className="mt-4 text-sm text-destructive">{deleteError}</p> : null}
                    </Card>
                  </div>
                ) : null}
                {activeTab === "voice" ? <AgentVoice draft={draft} catalog={catalog} catalogError={catalogError} onPatch={patchDraft} onRetryCatalog={() => void loadCatalog()} /> : null}
                {activeTab === "languages" ? <AgentLanguages draft={draft} catalog={catalog} catalogError={catalogError} onPatch={patchDraft} /> : null}
                {activeTab === "instructions" ? <AgentInstructions draft={draft} onPatch={patchDraft} /> : null}
                {activeTab === "questions" ? <QuestionsEditor agentId={agentId} languages={draft.languages} steps={steps} published={agent.status === "published"} onStepsChange={setSteps} /> : null}
                {activeTab === "testing" || activeTab === "history" ? (
                  <AgentTesting view={activeTab} agentId={agentId} agentName={agent.name} languages={draft.languages} defaultLanguage={draft.default_language} />
                ) : null}
                {activeTab === "publishing" ? (
                  <div className="max-w-3xl space-y-6">
                    <div>
                      <h2 className="text-lg font-semibold">Publishing</h2>
                      <p className="mt-1 text-sm leading-6 text-muted-foreground">Drafts can be saved while incomplete. Publishing checks every agent and enabled question against the server's rules.</p>
                    </div>
                    <Card className="space-y-4 p-5 sm:p-6">
                      <div className="flex flex-wrap items-center gap-3">
                        <span className="text-sm font-medium">Current status</span>
                        <Badge variant={statusVariant(agent.status)}>{agent.status}</Badge>
                      </div>
                      {agent.status === "suspended" ? (
                        <p className="text-sm leading-6 text-muted-foreground">This agent is suspended by an administrator. Owners cannot publish or unsuspend it.</p>
                      ) : agent.status === "published" ? (
                        <div className="space-y-3">
                          <p className="text-sm leading-6 text-muted-foreground">The public call page is available at <span className="font-medium text-foreground">/a/{agent.slug}</span>.</p>
                          <Button variant="outline" onClick={() => void changePublishState("unpublish")} disabled={publishing || dirty || saving}>
                            {publishing ? "Updating..." : "Unpublish agent"}
                          </Button>
                        </div>
                      ) : (
                        <div className="space-y-3">
                          {dirty ? <p className="text-sm text-muted-foreground">Save your changes before publishing.</p> : null}
                          <Button onClick={() => void changePublishState("publish")} disabled={publishing || dirty || saving}>
                            {publishing ? "Checking and publishing..." : "Publish agent"}
                          </Button>
                        </div>
                      )}
                      {publishMessage ? <p role="status" className={publishErrors && Object.keys(publishErrors).length > 0 ? "text-sm text-destructive" : "text-sm text-muted-foreground"}>{publishMessage}</p> : null}
                    </Card>
                    {Object.keys(publishErrors).length > 0 ? (
                      <section className="space-y-3" aria-labelledby="publish-errors-heading">
                        <h3 id="publish-errors-heading" className="font-semibold">Fix these fields to publish</h3>
                        <ul className="space-y-2">
                          {Object.entries(publishErrors).map(([field, messages]) => (
                            <li key={field} className="rounded-xl border border-destructive/20 bg-destructive/5 p-3">
                              <button type="button" className="text-left text-sm font-semibold text-destructive underline-offset-4 hover:underline" onClick={() => setActiveTab(tabForField(field))}>
                                {field}
                              </button>
                              <ul className="mt-1 list-inside list-disc text-sm leading-6 text-muted-foreground">
                                {messages.map((message, index) => <li key={`${field}-${index}`}>{message}</li>)}
                              </ul>
                            </li>
                          ))}
                        </ul>
                      </section>
                    ) : null}
                    {catalogLoading ? <p role="status" className="text-sm text-muted-foreground">Loading verified provider choices...</p> : null}
                    {catalogError ? <p className="text-sm text-muted-foreground">Provider catalog unavailable: {catalogError}</p> : null}
                  </div>
                ) : null}
              </TabPanel>
            </div>
            <ConfirmDialog
              open={deleteOpen}
              title="Delete this agent and its call history?"
              description="This permanently deletes the agent, its sessions, and saved answers. Deletion is blocked while a call is active. This cannot be undone."
              confirmLabel="Delete agent and history"
              pending={deleting}
              onConfirm={() => void deleteAgent()}
              onOpenChange={setDeleteOpen}
            />
          </>
        ) : null}
      </main>
    </OwnerShell>
  );
}

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, Check, RefreshCw, MessageSquare, Activity, X } from "lucide-react";
import { ChatTestPanel } from "@/components/ChatTestPanel";
import { FlowQAPanel } from "@/components/FlowQAPanel";
import { AgentBuilderTab } from "@/components/AgentBuilderTab";
import { AgentRuntimeTab } from "@/components/AgentRuntimeTab";
import { AgentPublishTab } from "@/components/AgentPublishTab";
import {
  type RuntimeSettingKey,
  type WorkspaceTab,
} from "@/lib/agent-workspace-constants";
import {
  platformAgentApi,
  type AgentDiagnostic,
  type AgentUpdateRequest,
  type AgentView,
  type DraftValidation,
  type DraftView,
  type JsonObject,
  type VersionView,
} from "@/lib/platform-agent-client";
import { PlatformApiError } from "@/lib/platform-api-client";
import {
  configWithFlow,
  getFlowFromConfig,
  type FlowDocument,
} from "@/lib/platform-agent-model";

interface PlatformAgentWorkspaceProps {
  agentId: string;
}

interface IdempotencyRecord {
  fingerprint: string;
  key: string;
}

type MobileTab = "editor" | "test";
type SaveStatus = "saved" | "unsaved" | "saving" | "error";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The platform request failed.";
}

function idempotencyKeyFor(ref: { current: IdempotencyRecord | null }, fingerprint: string): string {
  if (ref.current?.fingerprint === fingerprint) return ref.current.key;
  if (!globalThis.crypto?.randomUUID) {
    throw new Error("This browser cannot create an idempotency key for the requested action.");
  }
  const key = globalThis.crypto.randomUUID();
  ref.current = { fingerprint, key };
  return key;
}


export function PlatformAgentWorkspace({ agentId }: PlatformAgentWorkspaceProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [mobileTab, setMobileTab] = useState<MobileTab>(searchParams.get("tab") === "test" ? "test" : "editor");
  const initialSection = searchParams.get("section");
  const [workspaceTab, setWorkspaceTabState] = useState<WorkspaceTab>(
    initialSection === "runtime" || initialSection === "publish" ? initialSection : "builder"
  );
  const [activeDrawer, setActiveDrawer] = useState<"none" | "chat" | "qa">("none");
  const [agent, setAgent] = useState<AgentView | null>(null);
  const [draft, setDraft] = useState<DraftView | null>(null);
  const [config, setConfig] = useState<JsonObject | null>(null);
  const [versions, setVersions] = useState<VersionView[]>([]);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [versionsLoading, setVersionsLoading] = useState(true);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [flowJsonDirty, setFlowJsonDirty] = useState(false);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("saved");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveConflict, setSaveConflict] = useState(false);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [identityError, setIdentityError] = useState<string | null>(null);
  const [identitySaving, setIdentitySaving] = useState(false);
  const [identityName, setIdentityName] = useState("");
  const [identityDescription, setIdentityDescription] = useState("");
  const [identityPersona, setIdentityPersona] = useState("");
  const [enabledSaving, setEnabledSaving] = useState(false);
  const [validation, setValidation] = useState<DraftValidation | null>(null);
  const [validationConfigKey, setValidationConfigKey] = useState<string | null>(null);
  const [validationRevision, setValidationRevision] = useState<number | null>(null);
  const [validationLoading, setValidationLoading] = useState(false);
  const [changelog, setChangelog] = useState("");
  const [acknowledgedWarnings, setAcknowledgedWarnings] = useState<string[]>([]);
  const [publishLoading, setPublishLoading] = useState(false);
  const [restoringVersionId, setRestoringVersionId] = useState<string | null>(null);
  const [textOnly, setTextOnly] = useState(false);
  const [snapshotRefreshNeeded, setSnapshotRefreshNeeded] = useState(false);
  const [snapshotRefreshLoading, setSnapshotRefreshLoading] = useState(false);
  const draftRef = useRef<DraftView | null>(null);
  const configRef = useRef<JsonObject | null>(null);
  const dirtyRef = useRef(false);
  const conflictRef = useRef(false);
  const discardingRef = useRef(false);
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const saveTimerRef = useRef<number | null>(null);
  const pendingSaveCountRef = useRef(0);
  const publishKeyRef = useRef<IdempotencyRecord | null>(null);
  const versionsRequestRef = useRef(0);
  const activeWorkspaceUrlRef = useRef<string | null>(null);
  const activeWorkspaceHistoryStateRef = useRef<unknown>(null);

  const refreshVersions = useCallback(async () => {
    const requestId = ++versionsRequestRef.current;
    setVersionsLoading(true);
    setVersionsError(null);
    try {
      const loadedVersions = await platformAgentApi.listAllVersions(agentId);
      if (requestId === versionsRequestRef.current) setVersions(loadedVersions);
    } catch (error: unknown) {
      if (requestId === versionsRequestRef.current) setVersionsError(errorMessage(error));
    } finally {
      if (requestId === versionsRequestRef.current) setVersionsLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    setMobileTab(searchParams.get("tab") === "test" ? "test" : "editor");
  }, [searchParams]);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setLoadError(null);
    void refreshVersions();
    const load = async () => {
      try {
        const [loadedAgent, loadedDraft] = await Promise.all([
          platformAgentApi.getAgent(agentId),
          platformAgentApi.getDraft(agentId),
        ]);
        if (!current) return;
        draftRef.current = loadedDraft;
        configRef.current = loadedDraft.config;
        dirtyRef.current = false;
        conflictRef.current = false;
        setAgent(loadedAgent);
        setDraft(loadedDraft);
        setConfig(loadedDraft.config);
        setIdentityName(loadedAgent.name);
        setIdentityDescription(loadedAgent.description);
        setIdentityPersona(loadedAgent.persona_prompt);
        setTextOnly(loadedDraft.config.tts_voice_en === "" && loadedDraft.config.tts_voice_hi === "");
        setDirty(false);
        setSaveStatus("saved");
        setSnapshotRefreshNeeded(false);
      } catch (error: unknown) {
        if (current) setLoadError(errorMessage(error));
      } finally {
        if (current) setLoading(false);
      }
    };
    void load();
    return () => {
      current = false;
      versionsRequestRef.current += 1;
    };
  }, [agentId, refreshKey, refreshVersions]);

  const configKey = useMemo(() => config ? JSON.stringify(config) : "", [config]);
  const currentValidation = Boolean(
    validation &&
    draft &&
    validationConfigKey === configKey &&
    validationRevision === draft.draft_revision
  );
  const visibleDiagnostics: AgentDiagnostic[] = currentValidation && validation
    ? validation.diagnostics
    : !dirty && draft
      ? [...draft.validation.errors, ...draft.validation.warnings]
      : [];
  const warningCodes = currentValidation && validation
    ? Array.from(new Set(validation.warnings.map((warning) => warning.code)))
    : [];
  const allWarningsAcknowledged = warningCodes.every((code) => acknowledgedWarnings.includes(code));
  const identityDirty = Boolean(agent && (
    identityName !== agent.name ||
    identityDescription !== agent.description ||
    identityPersona !== agent.persona_prompt
  ));
  const workspacePath = `/agents/${agentId}`;
  const hasUnsavedWorkspaceChanges = identityDirty ||
    identitySaving ||
    flowJsonDirty ||
    dirty ||
    dirtyRef.current ||
    pendingSaveCountRef.current > 0 ||
    saveStatus === "saving" ||
    saveStatus === "error" ||
    saveError !== null ||
    publishLoading ||
    restoringVersionId !== null;
  useEffect(() => {
    if (window.location.pathname === workspacePath) {
      activeWorkspaceUrlRef.current = window.location.href;
      activeWorkspaceHistoryStateRef.current = window.history.state;
    }
  }, [searchParams, workspacePath]);

  useEffect(() => {
    if (!hasUnsavedWorkspaceChanges) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [hasUnsavedWorkspaceChanges]);

  useEffect(() => {
    if (!hasUnsavedWorkspaceChanges) return;
    const warnBeforeInternalNavigation = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (!(event.target instanceof Element)) return;
      const link = event.target.closest<HTMLAnchorElement>("a[href]");
      if (!link || link.target && link.target !== "_self" || link.hasAttribute("download")) return;
      const destination = new URL(link.href, window.location.href);
      if (destination.origin !== window.location.origin || destination.pathname === workspacePath) return;
      if (window.confirm("You have unsaved agent changes. Leave without saving?")) return;
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    const warnBeforeHistoryNavigation = (event: PopStateEvent) => {
      if (window.location.pathname === workspacePath || window.confirm("You have unsaved agent changes. Leave without saving?")) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const workspaceUrl = activeWorkspaceUrlRef.current;
      if (workspaceUrl) {
        window.history.pushState(activeWorkspaceHistoryStateRef.current, "", workspaceUrl);
      }
    };
    document.addEventListener("click", warnBeforeInternalNavigation, true);
    window.addEventListener("popstate", warnBeforeHistoryNavigation, true);
    return () => {
      document.removeEventListener("click", warnBeforeInternalNavigation, true);
      window.removeEventListener("popstate", warnBeforeHistoryNavigation, true);
    };
  }, [hasUnsavedWorkspaceChanges, workspacePath]);

  const eligibleToPublish = Boolean(currentValidation && validation?.eligible_for_publish);

  let flow: FlowDocument | null = null;
  let flowError: string | null = null;
  if (config) {
    try {
      flow = getFlowFromConfig(config);
    } catch (error: unknown) {
      flowError = errorMessage(error);
    }
  }

  const queueDraftSave = useCallback((snapshot: JsonObject) => {
    pendingSaveCountRef.current += 1;
    saveQueueRef.current = saveQueueRef.current.catch(() => undefined).then(async () => {
      try {
        if (conflictRef.current || discardingRef.current) return;
        const currentDraft = draftRef.current;
        if (!currentDraft) return;
        setSaveStatus("saving");
        try {
          const saved = await platformAgentApi.patchDraft(agentId, {
            expected_revision: currentDraft.draft_revision,
            config: snapshot,
          });
          draftRef.current = saved;
          setDraft(saved);
          setAgent((current) => current ? { ...current, draft_revision: saved.draft_revision, updated_at: saved.updated_at } : current);
          setSaveError(null);
          setSaveConflict(false);
          setValidation(null);
          setValidationConfigKey(null);
          if (configRef.current === snapshot) {
            configRef.current = saved.config;
            setConfig(saved.config);
            dirtyRef.current = false;
            setDirty(false);
            setSaveStatus("saved");
            setSnapshotRefreshNeeded(false);
          } else {
            setSaveStatus("unsaved");
          }
        } catch (error: unknown) {
          const conflict = error instanceof PlatformApiError && error.status === 409;
          if (conflict) conflictRef.current = true;
          setSaveConflict(conflict);
          setSaveError(errorMessage(error));
          setSaveStatus("error");
        }
      } finally {
        pendingSaveCountRef.current -= 1;
      }
    });
  }, [agentId]);

  useEffect(() => {
    if (!dirty || !config || saveConflict || discardingRef.current) return;
    const timer = window.setTimeout(() => {
      saveTimerRef.current = null;
      queueDraftSave(config);
    }, 800);
    saveTimerRef.current = timer;
    return () => {
      window.clearTimeout(timer);
      if (saveTimerRef.current === timer) saveTimerRef.current = null;
    };
  }, [config, dirty, queueDraftSave, saveConflict]);

  const updateConfig = (nextConfig: JsonObject) => {
    configRef.current = nextConfig;
    dirtyRef.current = true;
    setConfig(nextConfig);
    setDirty(true);
    setSaveStatus("unsaved");
    if (!conflictRef.current) setSaveError(null);
    setWorkspaceError(null);
    setValidation(null);
    setValidationConfigKey(null);
    setAcknowledgedWarnings([]);
  };

  const updateFlow = (nextFlow: FlowDocument) => {
    if (config) updateConfig(configWithFlow(config, nextFlow));
  };


  const discardLocalAndReload = async () => {
    if (saveStatus === "saving" || pendingSaveCountRef.current > 0) return;
    if (dirtyRef.current && !window.confirm("Discard your unsaved local changes and load the latest server draft?")) return;
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    discardingRef.current = true;
    setWorkspaceError(null);
    try {
      const fresh = await platformAgentApi.getDraft(agentId);
      draftRef.current = fresh;
      configRef.current = fresh.config;
      dirtyRef.current = false;
      conflictRef.current = false;
      setDraft(fresh);
      setConfig(fresh.config);
      setAgent((current) => current ? { ...current, draft_revision: fresh.draft_revision, updated_at: fresh.updated_at } : current);
      setTextOnly(fresh.config.tts_voice_en === "" && fresh.config.tts_voice_hi === "");
      setSnapshotRefreshNeeded(false);
      setDirty(false);
      setSaveConflict(false);
      setSaveError(null);
      setSaveStatus("saved");
      setValidation(null);
      setValidationConfigKey(null);
      setAcknowledgedWarnings([]);
    } catch (error: unknown) {
      setWorkspaceError(errorMessage(error));
    } finally {
      discardingRef.current = false;
    }
  };


  const updateRuntimeSetting = (key: RuntimeSettingKey, value: string) => {
    if (!config) return;
    const next = { ...config };
    if (value.trim()) next[key] = value;
    else delete next[key];
    updateConfig(next);
  };

  const updateTextOnly = (checked: boolean) => {
    if (!config) return;
    const next = { ...config };
    if (checked) {
      next.tts_voice_en = "";
      next.tts_voice_hi = "";
    } else {
      if (next.tts_voice_en === "") delete next.tts_voice_en;
      if (next.tts_voice_hi === "") delete next.tts_voice_hi;
    }
    setTextOnly(checked);
    updateConfig(next);
  };
  const updateLlmSelection = (provider: string, model: string) => {
    if (!config) return;
    updateConfig({ ...config, llm_provider: provider, llm_model: model });
  };
  const updateSttSelection = (provider: string, model: string) => {
    if (!config) return;
    updateConfig({ ...config, stt_provider: provider, stt_model: model });
  };

  const updateVoiceSettings = (provider: string, model: string, en: string, hi: string, checked: boolean) => {
    if (!config) return;
    const next = { ...config };
    if (provider) next.tts_provider = provider;
    else delete next.tts_provider;
    if (model) next.tts_model = model;
    else delete next.tts_model;
    if (checked) {
      next.tts_voice_en = "";
      next.tts_voice_hi = "";
    } else {
      next.tts_voice_en = en;
      next.tts_voice_hi = hi;
    }
    setTextOnly(checked);
    updateConfig(next);
  };

  const refreshDraftSnapshot = async (identityAlreadySaved = false): Promise<boolean> => {
    const hasLocalIdentityChanges = Boolean(agent && (
      identityName !== agent.name ||
      identityDescription !== agent.description ||
      identityPersona !== agent.persona_prompt
    ));
    if (dirtyRef.current || pendingSaveCountRef.current > 0 || (!identityAlreadySaved && hasLocalIdentityChanges)) {
      setSnapshotRefreshNeeded(true);
      setWorkspaceError("Resolve local changes before refreshing the server draft snapshot. Your local edits are still here.");
      return false;
    }
    setSnapshotRefreshLoading(true);
    try {
      const [refreshedAgent, refreshedDraft] = await Promise.all([
        platformAgentApi.getAgent(agentId),
        platformAgentApi.getDraft(agentId),
      ]);
      if (dirtyRef.current || pendingSaveCountRef.current > 0) {
        setSnapshotRefreshNeeded(true);
        setWorkspaceError("Local draft changes were made during refresh. Your edits are still here; save or resolve them before refreshing the snapshot.");
        return false;
      }
      draftRef.current = refreshedDraft;
      configRef.current = refreshedDraft.config;
      dirtyRef.current = false;
      conflictRef.current = false;
      setAgent(refreshedAgent);
      setIdentityName(refreshedAgent.name);
      setIdentityDescription(refreshedAgent.description);
      setIdentityPersona(refreshedAgent.persona_prompt);
      setDraft(refreshedDraft);
      setConfig(refreshedDraft.config);
      setTextOnly(refreshedDraft.config.tts_voice_en === "" && refreshedDraft.config.tts_voice_hi === "");
      setDirty(false);
      setSaveConflict(false);
      setSaveError(null);
      setSaveStatus("saved");
      setValidation(null);
      setValidationConfigKey(null);
      setSnapshotRefreshNeeded(false);
      setWorkspaceError(null);
      return true;
    } catch (error: unknown) {
      setSnapshotRefreshNeeded(true);
      setWorkspaceError(`Could not refresh the current draft snapshot: ${errorMessage(error)}`);
      return false;
    } finally {
      setSnapshotRefreshLoading(false);
    }
  };

  const saveIdentity = async (): Promise<boolean> => {
    if (!agent || !identityDirty) return true;
    const request: AgentUpdateRequest = {
      name: identityName.trim(),
      description: identityDescription.trim(),
      persona_prompt: identityPersona,
    };
    setIdentitySaving(true);
    setIdentityError(null);
    try {
      const updated = await platformAgentApi.updateAgent(agentId, request);
      setAgent(updated);
      setIdentityName(updated.name);
      setIdentityDescription(updated.description);
      setIdentityPersona(updated.persona_prompt);
      if (updated.persona_prompt !== agent.persona_prompt || snapshotRefreshNeeded) {
        setValidation(null);
        setValidationConfigKey(null);
        setValidationRevision(null);
        setAcknowledgedWarnings([]);
        setDraft((current) => (current ? { ...current, snapshot_hash: null } : current));
        setSnapshotRefreshNeeded(true);
        await refreshDraftSnapshot(true);
      }
      return true;
    } catch (error: unknown) {
      setIdentityError(errorMessage(error));
      return false;
    } finally {
      setIdentitySaving(false);
    }
  };

  const saveDraftNow = async () => {
    if (saveConflict) return;
    if (identityDirty) {
      const ok = await saveIdentity();
      if (!ok) return;
    }
    if (!config || !dirty) return;
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    setSaveError(null);
    queueDraftSave(config);
  };

  const handleEnabledChange = async () => {
    if (!agent) return;
    setEnabledSaving(true);
    setWorkspaceError(null);
    try {
      const updated = await platformAgentApi.setEnabled(agentId, !agent.enabled);
      setAgent(updated);
    } catch (error: unknown) {
      setWorkspaceError(errorMessage(error));
    } finally {
      setEnabledSaving(false);
    }
  };

  const handleValidate = async () => {
    if (!draft || !config || identityDirty || identitySaving || flowJsonDirty || publishLoading || snapshotRefreshNeeded || snapshotRefreshLoading) return;
    setValidationLoading(true);
    setWorkspaceError(null);
    try {
      const result = await platformAgentApi.validateDraft(agentId, dirty
        ? { expected_revision: draft.draft_revision, config }
        : { expected_revision: draft.draft_revision, expected_authoring_hash: draft.authoring_hash });
      setValidation(result);
      setValidationConfigKey(configKey);
      setValidationRevision(draft.draft_revision);
      setAcknowledgedWarnings([]);
    } catch (error: unknown) {
      setWorkspaceError(errorMessage(error));
    } finally {
      setValidationLoading(false);
    }
  };

  const handlePublish = async () => {
    if (!draft || !config || identityDirty || identitySaving || flowJsonDirty || publishLoading || snapshotRefreshNeeded || snapshotRefreshLoading || !validation || !currentValidation || !validation.eligible_for_publish || !allWarningsAcknowledged || !changelog.trim()) return;
    setPublishLoading(true);
    setWorkspaceError(null);
    try {
      const request = {
        expected_revision: draft.draft_revision,
        changelog: changelog.trim(),
        acknowledged_warning_codes: acknowledgedWarnings,
      };
      const key = idempotencyKeyFor(publishKeyRef, JSON.stringify({ agentId, request }));
      await platformAgentApi.publishDraft(agentId, request, key);
      setChangelog("");
      setRefreshKey((value) => value + 1);
    } catch (error: unknown) {
      setWorkspaceError(errorMessage(error));
    } finally {
      setPublishLoading(false);
    }
  };

  const handleRestoreVersion = async (version: VersionView) => {
    if (!draft || saveStatus === "saving" || pendingSaveCountRef.current > 0) {
      if (pendingSaveCountRef.current > 0) setWorkspaceError("Wait for the current draft save to finish before restoring a version.");
      return;
    }
    const warning = dirtyRef.current
      ? "This replaces the saved draft and discards your unsaved local edits. Continue?"
      : `Restore published version ${version.version} to the current draft?`;
    if (!window.confirm(warning)) return;
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    setRestoringVersionId(version.version_id);
    setWorkspaceError(null);
    try {
      const restored = await platformAgentApi.restoreDraft(agentId, {
        version_id: version.version_id,
        expected_revision: draft.draft_revision,
      });
      draftRef.current = restored;
      configRef.current = restored.config;
      dirtyRef.current = false;
      conflictRef.current = false;
      setDraft(restored);
      setConfig(restored.config);
      setAgent((current) => current ? { ...current, draft_revision: restored.draft_revision, updated_at: restored.updated_at } : current);
      setTextOnly(restored.config.tts_voice_en === "" && restored.config.tts_voice_hi === "");
      setSnapshotRefreshNeeded(false);
      setDirty(false);
      setSaveStatus("saved");
      setSaveError(null);
      setSaveConflict(false);
      setValidation(null);
      setValidationConfigKey(null);
      setAcknowledgedWarnings([]);
    } catch (error: unknown) {
      setWorkspaceError(errorMessage(error));
    } finally {
      setRestoringVersionId(null);
    }
  };

  const setMobileMode = (tab: MobileTab) => {
    setMobileTab(tab);
    if (tab === "test") {
      setActiveDrawer("chat");
    } else {
      setActiveDrawer("none");
    }
    const params = new URLSearchParams(searchParams.toString());
    if (tab === "test") {
      params.set("tab", "test");
    } else {
      params.delete("tab");
    }
    const query = params.toString();
    router.replace(query ? `/agents/${agentId}?${query}` : `/agents/${agentId}`, { scroll: false });
  };

  const setWorkspaceTab = (tab: WorkspaceTab) => {
    setWorkspaceTabState(tab);
    const params = new URLSearchParams(searchParams.toString());
    if (tab === "builder") {
      params.delete("section");
    } else {
      params.set("section", tab);
    }
    const query = params.toString();
    router.replace(query ? `/agents/${agentId}?${query}` : `/agents/${agentId}`, { scroll: false });
  };


  if (loading) {
    return <div role="status" className="flex min-h-[40vh] items-center justify-center gap-3 text-sm text-limestone-400"><RefreshCw className="h-4 w-4 animate-spin text-copper-400" />Loading agent workspace…</div>;
  }

  if (loadError || !agent || !draft || !config) {
    return (
      <div className="surface mx-auto mt-8 max-w-xl space-y-4 p-6 text-center">
        <h1 className="text-xl font-semibold text-limestone-100">Could not load this agent</h1>
        <p role="alert" className="text-sm text-rust-200">{loadError ?? "The agent response was incomplete."}</p>
        <div className="flex flex-wrap justify-center gap-2">
          <button type="button" className="button-secondary" onClick={() => setRefreshKey((value) => value + 1)}>Retry</button>
          <Link href="/agents" className="button-primary">Back to agents</Link>
        </div>
      </div>
    );
  }

  const latestVersion = versions.find((version) => version.version_id === agent.published_version_id) ?? null;
  const draftStatusLabel = identitySaving
    ? "Saving agent details…"
    : identityDirty
      ? "Unsaved agent details"
      : flowJsonDirty
        ? "Unapplied Flow JSON"
        : saveStatus === "saving"
          ? "Saving draft…"
          : saveStatus === "unsaved"
            ? "Unsaved changes"
            : saveStatus === "error"
              ? "Save failed"
              : `Saved r${draft.draft_revision}`;
  const publishDisabled = publishLoading || identityDirty || identitySaving || flowJsonDirty || snapshotRefreshNeeded || snapshotRefreshLoading || dirty || saveStatus === "saving" || !eligibleToPublish || !allWarningsAcknowledged || !changelog.trim();

  return (
    <div className="mx-auto max-w-[1600px] space-y-5">
      <div className="flex items-center justify-between">
        <Link href="/agents" className="inline-flex items-center gap-2 text-xs text-limestone-400 hover:text-limestone-100 transition-colors">
          <ArrowLeft className="h-3.5 w-3.5" />Back to agents
        </Link>
      </div>

      <header className="flex flex-col gap-4 border-b border-ink-800 pb-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="truncate text-2xl font-bold tracking-tight text-limestone-100">{agent.name}</h1>
            <span className={`rounded-md border px-2 py-0.5 text-xs font-mono ${agent.enabled ? "border-moss-700 bg-moss-950 text-moss-200" : "border-ink-700 text-limestone-400"}`}>
              {agent.enabled ? "Enabled" : "Disabled"}
            </span>
            <span className="rounded-md border border-ink-700 px-2 py-0.5 text-xs font-mono text-limestone-300">
              Draft r{draft.draft_revision}
            </span>
            {latestVersion ? (
              <span className="rounded-md border border-copper-800 px-2 py-0.5 text-xs font-mono text-copper-200">
                v{latestVersion.version}
              </span>
            ) : null}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span role="status" aria-live="polite" className={`text-xs ${saveStatus === "error" ? "text-rust-300" : "text-limestone-400"}`}>{draftStatusLabel}</span>
          {snapshotRefreshNeeded ? (
            <button
              type="button"
              className="button-secondary"
              onClick={() => void refreshDraftSnapshot()}
              disabled={identitySaving || snapshotRefreshLoading || saveStatus === "saving"}
            >
              {snapshotRefreshLoading ? "Refreshing…" : "Refresh draft snapshot"}
            </button>
          ) : null}
          <button
            type="button"
            className={activeDrawer === "chat" ? "button-primary" : "button-secondary"}
            onClick={() => setActiveDrawer((cur) => (cur === "chat" ? "none" : "chat"))}
            title="Toggle Chat Test simulator"
          >
            <MessageSquare className="h-4 w-4" />Chat Test
          </button>
          <button
            type="button"
            className={activeDrawer === "qa" ? "button-primary" : "button-secondary"}
            onClick={() => setActiveDrawer((cur) => (cur === "qa" ? "none" : "qa"))}
            title="Toggle Flow QA test scenarios"
          >
            <Activity className="h-4 w-4" />Flow QA
          </button>
          <button
            type="button"
            className="button-secondary"
            onClick={() => void saveDraftNow()}
            disabled={(!dirty && !identityDirty) || saveStatus === "saving" || identitySaving || saveConflict}
          >
            <Check className="h-4 w-4" />Save draft
          </button>
          <button type="button" className="button-secondary" onClick={handleEnabledChange} disabled={enabledSaving}>
            {enabledSaving ? "Updating…" : agent.enabled ? "Disable" : "Enable"}
          </button>
        </div>
      </header>

      {workspaceError ? <div role="alert" className="surface flex flex-wrap items-center justify-between gap-3 p-4 text-sm text-rust-200"><span>{workspaceError}</span><button type="button" className="button-secondary" onClick={() => setWorkspaceError(null)}>Dismiss</button></div> : null}
      {saveError ? (
        <div role="alert" className="surface space-y-3 border-rust-900 p-4 text-sm text-rust-200">
          <p>{saveConflict ? "This draft changed on the server. Your local edits are still here and were not overwritten." : `Draft save failed: ${saveError}`}</p>
          {saveConflict ? <p className="text-xs text-limestone-400">Copy or review your local flow before reloading the latest server revision.</p> : null}
          <div className="flex flex-wrap gap-2">
            {!saveConflict ? <button type="button" className="button-secondary" onClick={() => void saveDraftNow()} disabled={saveStatus === "saving"}>Retry save</button> : null}
            <button type="button" className="button-secondary" onClick={() => void discardLocalAndReload()} disabled={saveStatus === "saving"}>Discard local changes and reload</button>
            <button type="button" className="button-secondary" onClick={() => setSaveError(null)}>Dismiss</button>
          </div>
        </div>
      ) : null}

      <div role="group" aria-label="Agent workspace section" className="flex gap-2 lg:hidden">
        <button type="button" aria-pressed={mobileTab === "editor"} className={mobileTab === "editor" ? "button-primary flex-1" : "button-secondary flex-1"} onClick={() => setMobileMode("editor")}>Workspace</button>
        <button type="button" aria-pressed={mobileTab === "test"} className={mobileTab === "test" ? "button-primary flex-1" : "button-secondary flex-1"} onClick={() => setMobileMode("test")}>Test agent</button>
      </div>

      <div className="w-full">
        <main className={`${mobileTab === "editor" ? "block" : "hidden"} space-y-6 lg:block w-full`}>
          <div role="tablist" aria-label="Workspace section" className="flex gap-2 border-b border-ink-800 pb-3">
            {(["builder", "runtime", "publish"] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                role="tab"
                id={`tab-${tab}`}
                aria-controls={`tabpanel-${tab}`}
                aria-selected={workspaceTab === tab}
                className={`px-3.5 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                  workspaceTab === tab
                    ? "bg-ink-800 text-copper-200 border-b-2 border-copper-500 font-semibold"
                    : "text-limestone-400 hover:text-limestone-200 hover:bg-ink-900"
                }`}
                onClick={() => setWorkspaceTab(tab)}
              >
                {tab === "builder" ? "Builder" : tab === "runtime" ? "Runtime Settings" : "Releases & History"}
              </button>
            ))}
          </div>

          <div hidden={workspaceTab !== "builder"}>
            <AgentBuilderTab
              identityDescription={identityDescription}
              identityPersona={identityPersona}
              onIdentityDescriptionChange={(val) => {
                setIdentityDescription(val);
                setIdentityError(null);
              }}
              onIdentityPersonaChange={(val) => {
                setIdentityPersona(val);
                setIdentityError(null);
              }}
              identityDisabled={identitySaving || snapshotRefreshLoading || publishLoading}
              identityError={identityError}
              config={config}
              onLlmSelectionChange={updateLlmSelection}
              onSttSelectionChange={updateSttSelection}
              textOnly={textOnly}
              onVoiceSettingsChange={updateVoiceSettings}
              flow={flow}
              flowError={flowError}
              visibleDiagnostics={visibleDiagnostics}
              onFlowChange={updateFlow}
              onFlowJsonDirtyChange={setFlowJsonDirty}
            />
          </div>

          {workspaceTab === "runtime" ? (
            <AgentRuntimeTab
              createdAt={agent.created_at}
              config={config}
              onRuntimeSettingChange={updateRuntimeSetting}
              textOnly={textOnly}
              onTextOnlyChange={updateTextOnly}
              agentName={identityName}
              onAgentNameChange={(val) => {
                setIdentityName(val);
                setIdentityError(null);
              }}
              nameDisabled={identitySaving || snapshotRefreshLoading || publishLoading}
            />
          ) : null}

          {workspaceTab === "publish" ? (
            <AgentPublishTab
              validation={validation}
              currentValidation={currentValidation}
              eligibleToPublish={eligibleToPublish}
              validationLoading={validationLoading}
              onValidate={() => void handleValidate()}
              validateDisabled={
                validationLoading ||
                identityDirty ||
                identitySaving ||
                flowJsonDirty ||
                snapshotRefreshNeeded ||
                snapshotRefreshLoading ||
                publishLoading ||
                saveStatus === "saving"
              }
              warningCodes={warningCodes}
              acknowledgedWarnings={acknowledgedWarnings}
              onAcknowledgedWarningsChange={setAcknowledgedWarnings}
              allWarningsAcknowledged={allWarningsAcknowledged}
              changelog={changelog}
              onChangelogChange={setChangelog}
              publishDisabled={publishDisabled}
              publishLoading={publishLoading}
              onPublish={() => void handlePublish()}
              dirty={dirty}
              versions={versions}
              versionsLoading={versionsLoading}
              versionsError={versionsError}
              onRetryVersions={() => void refreshVersions()}
              restoringVersionId={restoringVersionId}
              onRestoreVersion={(v) => void handleRestoreVersion(v)}
              restoreDisabled={
                restoringVersionId !== null || saveStatus === "saving" || pendingSaveCountRef.current > 0
              }
            />
          ) : null}
        </main>

        {/* Right-side docked testing drawer (occupies complete right edge) */}
        {activeDrawer !== "none" ? (
          <aside
            aria-label="Agent test rail"
            className="fixed right-0 top-0 bottom-0 z-50 w-full sm:w-[440px] md:w-[460px] lg:w-[480px] bg-ink-950 border-l border-ink-800 shadow-2xl p-5 overflow-y-auto space-y-4"
          >
            <div className="flex items-center justify-between border-b border-ink-800 pb-3">
              <div className="flex items-center gap-2">
                {activeDrawer === "chat" ? (
                  <MessageSquare className="h-4 w-4 text-copper-400" />
                ) : (
                  <Activity className="h-4 w-4 text-copper-400" />
                )}
                <span className="text-xs font-semibold uppercase tracking-wider text-limestone-200">
                  {activeDrawer === "chat" ? "Interactive Chat Test" : "Flow QA Runner"}
                </span>
              </div>
              <button
                type="button"
                onClick={() => setActiveDrawer("none")}
                className="rounded-lg p-1.5 text-limestone-400 hover:bg-ink-800 hover:text-limestone-100 transition-colors"
                aria-label="Close test rail"
                title="Close"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {activeDrawer === "chat" ? (
              <ChatTestPanel
                key={agentId}
                agentId={agentId}
                draft={draft}
                versions={versions}
                onRefreshDraftSnapshot={refreshDraftSnapshot}
              />
            ) : (
              <FlowQAPanel
                key={agentId}
                agentId={agentId}
                draft={draft}
                versions={versions}
                draftReady={
                  !identityDirty &&
                  !identitySaving &&
                  !flowJsonDirty &&
                  !dirty &&
                  !dirtyRef.current &&
                  pendingSaveCountRef.current === 0 &&
                  saveStatus === "saved" &&
                  !saveConflict &&
                  !snapshotRefreshNeeded &&
                  !snapshotRefreshLoading &&
                  !publishLoading &&
                  restoringVersionId === null
                }
                onRefreshDraftSnapshot={() => refreshDraftSnapshot(true)}
              />
            )}
          </aside>
        ) : null}
      </div>
    </div>
  );
}

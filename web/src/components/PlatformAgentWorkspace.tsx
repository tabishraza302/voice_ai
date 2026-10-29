"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, Check, Clock3, RefreshCw, RotateCcw } from "lucide-react";
import { ChatTestPanel } from "@/components/ChatTestPanel";
import { ConversationFlowEditor } from "@/components/ConversationFlowEditor";
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
  summarizeRuntimeSettings,
  type FlowDocument,
} from "@/lib/platform-agent-model";

interface PlatformAgentWorkspaceProps {
  agentId: string;
}

interface IdempotencyRecord {
  fingerprint: string;
  key: string;
}

const SETTINGS = [
  { key: "llm_model", label: "Runtime model ID", maxLength: 64 },
  { key: "stt_provider", label: "Speech recognition provider ID", maxLength: 32 },
  { key: "tts_voice_en", label: "English voice ID", maxLength: 32 },
  { key: "tts_voice_hi", label: "Hindi voice ID", maxLength: 32 },
] as const;

type RuntimeSettingKey = typeof SETTINGS[number]["key"];
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

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Date unavailable"
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function getString(config: JsonObject, key: RuntimeSettingKey): string {
  return typeof config[key] === "string" ? config[key] as string : "";
}

export function PlatformAgentWorkspace({ agentId }: PlatformAgentWorkspaceProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [mobileTab, setMobileTab] = useState<MobileTab>(searchParams.get("tab") === "test" ? "test" : "editor");
  const [agent, setAgent] = useState<AgentView | null>(null);
  const [draft, setDraft] = useState<DraftView | null>(null);
  const [config, setConfig] = useState<JsonObject | null>(null);
  const [versions, setVersions] = useState<VersionView[]>([]);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [dirty, setDirty] = useState(false);
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

  useEffect(() => {
    setMobileTab(searchParams.get("tab") === "test" ? "test" : "editor");
  }, [searchParams]);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setLoadError(null);
    setVersionsError(null);
    const load = async () => {
      try {
        const versionRequest = platformAgentApi.listAllVersions(agentId).catch((error: unknown) => {
          if (current) setVersionsError(errorMessage(error));
          return [];
        });
        const [loadedAgent, loadedDraft, loadedVersions] = await Promise.all([
          platformAgentApi.getAgent(agentId),
          platformAgentApi.getDraft(agentId),
          versionRequest,
        ]);
        if (!current) return;
        draftRef.current = loadedDraft;
        configRef.current = loadedDraft.config;
        dirtyRef.current = false;
        conflictRef.current = false;
        setAgent(loadedAgent);
        setDraft(loadedDraft);
        setConfig(loadedDraft.config);
        setVersions(loadedVersions);
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
    return () => { current = false; };
  }, [agentId, refreshKey]);

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

  const saveDraftNow = () => {
    if (!config || saveConflict) return;
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    setSaveError(null);
    queueDraftSave(config);
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

  const handleSaveIdentity = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
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
      if (updated.persona_prompt !== agent?.persona_prompt || snapshotRefreshNeeded) {
        setDraft((current) => current ? { ...current, snapshot_hash: null } : current);
        setSnapshotRefreshNeeded(true);
        await refreshDraftSnapshot(true);
      }
    } catch (error: unknown) {
      setIdentityError(errorMessage(error));
    } finally {
      setIdentitySaving(false);
    }
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
    if (!draft || !config) return;
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
    if (!draft || !config || !validation || !currentValidation || !validation.eligible_for_publish || !allWarningsAcknowledged || !changelog.trim()) return;
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
    router.replace(tab === "test" ? `/agents/${agentId}?tab=test` : `/agents/${agentId}`, { scroll: false });
  };

  const identityDirty = Boolean(agent && (
    identityName !== agent.name ||
    identityDescription !== agent.description ||
    identityPersona !== agent.persona_prompt
  ));

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
  const runtimeSettings = summarizeRuntimeSettings(config);
  const draftStatusLabel = saveStatus === "saving" ? "Saving draft…" : saveStatus === "unsaved" ? "Unsaved changes" : saveStatus === "error" ? "Save failed" : `Saved r${draft.draft_revision}`;
  const publishDisabled = publishLoading || dirty || saveStatus === "saving" || !eligibleToPublish || !allWarningsAcknowledged || !changelog.trim();

  return (
    <div className="mx-auto max-w-[1600px] space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Link href="/agents" className="inline-flex items-center gap-2 text-sm text-limestone-400 hover:text-limestone-100">
          <ArrowLeft className="h-4 w-4" />Back to agents
        </Link>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className={`rounded-md border px-2 py-1 ${agent.enabled ? "border-moss-700 bg-moss-950 text-moss-200" : "border-ink-700 text-limestone-400"}`}>{agent.enabled ? "Enabled" : "Disabled"}</span>
          <span className="rounded-md border border-ink-700 px-2 py-1 text-limestone-300">Draft r{draft.draft_revision}</span>
          {latestVersion ? <span className="rounded-md border border-copper-800 px-2 py-1 text-copper-200">Published v{latestVersion.version}</span> : <span className="rounded-md border border-ink-700 px-2 py-1 text-limestone-400">Not published</span>}
        </div>
      </div>

      <header className="flex flex-col gap-4 border-b border-ink-800 pb-5 lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0">
          <p className="text-sm font-medium text-copper-300">Agent workspace</p>
          <h1 className="mt-1 truncate text-3xl font-semibold tracking-tight text-limestone-100">{agent.name}</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-limestone-400">Configure the saved flow, inspect validation, and test this exact draft or a published version.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span role="status" aria-live="polite" className={`text-xs ${saveStatus === "error" ? "text-rust-300" : "text-limestone-400"}`}>{draftStatusLabel}</span>
          <button type="button" className="button-secondary" onClick={saveDraftNow} disabled={!dirty || saveStatus === "saving" || saveConflict}>
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
            {!saveConflict ? <button type="button" className="button-secondary" onClick={saveDraftNow} disabled={saveStatus === "saving"}>Retry save</button> : null}
            <button type="button" className="button-secondary" onClick={() => void discardLocalAndReload()} disabled={saveStatus === "saving"}>Discard local changes and reload</button>
            <button type="button" className="button-secondary" onClick={() => setSaveError(null)}>Dismiss</button>
          </div>
        </div>
      ) : null}

      <div role="tablist" aria-label="Agent workspace section" className="flex gap-2 lg:hidden">
        <button type="button" role="tab" aria-selected={mobileTab === "editor"} className={mobileTab === "editor" ? "button-primary flex-1" : "button-secondary flex-1"} onClick={() => setMobileMode("editor")}>Workspace</button>
        <button type="button" role="tab" aria-selected={mobileTab === "test"} className={mobileTab === "test" ? "button-primary flex-1" : "button-secondary flex-1"} onClick={() => setMobileMode("test")}>Test agent</button>
      </div>

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(22rem,28rem)]">
        <main className={`${mobileTab === "editor" ? "block" : "hidden"} space-y-6 lg:block`}>
          <section className="surface p-5 sm:p-6">
            <div className="mb-4 flex flex-wrap items-end justify-between gap-3 border-b border-ink-800 pb-4">
              <div>
                <h2 className="text-lg font-semibold text-limestone-100">Agent details</h2>
                <p className="mt-1 text-sm text-limestone-400">Name, purpose, and persona instructions for this agent.</p>
              </div>
              <span className="text-xs text-limestone-500">Created <time dateTime={agent.created_at}>{formatDate(agent.created_at)}</time></span>
            </div>
            <form className="space-y-4" onSubmit={handleSaveIdentity}>
              <div>
                <label className="field-label" htmlFor="workspace-agent-name">Agent name</label>
                <input id="workspace-agent-name" className="field-control" value={identityName} onChange={(event) => { setIdentityName(event.currentTarget.value); setIdentityError(null); }} required disabled={identitySaving || snapshotRefreshLoading} />
              </div>
              <div>
                <label className="field-label" htmlFor="workspace-agent-description">Purpose</label>
                <textarea id="workspace-agent-description" className="field-control min-h-20" value={identityDescription} onChange={(event) => { setIdentityDescription(event.currentTarget.value); setIdentityError(null); }} disabled={identitySaving || snapshotRefreshLoading} />
              </div>
              <div>
                <label className="field-label" htmlFor="workspace-agent-persona">Persona instructions</label>
                <textarea id="workspace-agent-persona" className="field-control min-h-32 font-mono text-sm" value={identityPersona} onChange={(event) => { setIdentityPersona(event.currentTarget.value); setIdentityError(null); }} disabled={identitySaving || snapshotRefreshLoading} />
              </div>
              {identityError ? <p role="alert" className="text-sm text-rust-200">{identityError}</p> : null}
              {snapshotRefreshNeeded ? (
                <button type="button" className="button-secondary" onClick={() => void refreshDraftSnapshot()} disabled={identitySaving || snapshotRefreshLoading}>
                  {snapshotRefreshLoading ? "Refreshing…" : "Refresh draft snapshot"}
                </button>
              ) : null}
              <button type="submit" className="button-secondary" disabled={identitySaving || snapshotRefreshLoading || !identityDirty}>
                {identitySaving ? "Saving details…" : "Save agent details"}
              </button>
            </form>
          </section>

          <section className="surface space-y-4 p-5 sm:p-6">
            <div className="flex flex-wrap items-start justify-between gap-3 border-b border-ink-800 pb-4">
              <div>
                <h2 className="text-lg font-semibold text-limestone-100">Runtime settings</h2>
                <p className="mt-1 text-sm text-limestone-400">Leave provider IDs blank to use platform defaults. The platform checks support before publishing.</p>
              </div>
              {runtimeSettings.llmModel || runtimeSettings.sttProvider || runtimeSettings.voices ? <span className="text-xs text-limestone-500">Explicit settings</span> : <span className="text-xs text-limestone-500">Platform defaults</span>}
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              {SETTINGS.slice(0, 2).map((setting) => (
                <div key={setting.key}>
                  <label className="field-label" htmlFor={`setting-${setting.key}`}>{setting.label}</label>
                  <input
                    id={`setting-${setting.key}`}
                    className="field-control font-mono text-sm"
                    value={getString(config, setting.key)}
                    onChange={(event) => updateRuntimeSetting(setting.key, event.currentTarget.value)}
                    maxLength={setting.maxLength}
                    autoComplete="off"
                  />
                  <p className="mt-1 text-xs text-limestone-500">Blank uses the platform default.</p>
                </div>
              ))}
            </div>
            <label className="flex min-h-11 items-center gap-3 rounded-lg border border-ink-800 p-3 text-sm text-limestone-200">
              <input type="checkbox" className="h-4 w-4 accent-copper-500" checked={textOnly} onChange={(event) => updateTextOnly(event.currentTarget.checked)} />
              Publish as text-only (no voice output)
            </label>
            <div className="grid gap-4 sm:grid-cols-2">
              {SETTINGS.slice(2).map((setting) => (
                <div key={setting.key}>
                  <label className="field-label" htmlFor={`setting-${setting.key}`}>{setting.label}</label>
                  <input
                    id={`setting-${setting.key}`}
                    className="field-control font-mono text-sm"
                    value={getString(config, setting.key)}
                    onChange={(event) => updateRuntimeSetting(setting.key, event.currentTarget.value)}
                    maxLength={setting.maxLength}
                    autoComplete="off"
                    disabled={textOnly}
                  />
                </div>
              ))}
            </div>
            <p className="text-xs leading-5 text-limestone-500">Configure both voice IDs, or leave both blank for platform defaults. The text-only switch explicitly stores empty voice IDs.</p>
          </section>

          {flow ? (
            <ConversationFlowEditor flow={flow} diagnostics={visibleDiagnostics} onChange={updateFlow} />
          ) : (
            <section className="surface space-y-3 p-5">
              <h2 className="text-lg font-semibold text-limestone-100">Flow document unavailable</h2>
              <p role="alert" className="text-sm text-rust-200">{flowError ?? "The draft has no flow configuration."}</p>
              <p className="text-xs text-limestone-400">Your saved configuration has not been replaced. Correct its flow envelope through the platform draft contract.</p>
            </section>
          )}

          <section className="surface space-y-4 p-5 sm:p-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-limestone-100">Validate and publish</h2>
                <p className="mt-1 text-sm text-limestone-400">Validation uses the current schema. Publishing creates an immutable version; it never edits the saved source.</p>
              </div>
              <button type="button" className="button-secondary" onClick={() => void handleValidate()} disabled={validationLoading || saveStatus === "saving"}>
                {validationLoading ? "Validating…" : "Validate current draft"}
              </button>
            </div>
            {currentValidation && validation ? (
              <div role="status" className={`rounded-lg border p-3 text-sm ${validation.eligible_for_publish ? "border-moss-800 bg-moss-950/30 text-moss-200" : "border-rust-800 bg-rust-950/30 text-rust-200"}`}>
                {validation.eligible_for_publish ? "This draft is eligible for publishing." : "This draft is not eligible for publishing. Review the diagnostics above."}
                <span className="ml-2 text-xs text-limestone-400">Authoring hash {validation.authoring_hash.slice(0, 12)}…</span>
              </div>
            ) : null}
            {currentValidation && warningCodes.length > 0 ? (
              <fieldset className="space-y-2 rounded-lg border border-copper-900/70 p-4">
                <legend className="px-1 text-sm font-semibold text-copper-200">Acknowledge publish warnings</legend>
                {warningCodes.map((code) => (
                  <label key={code} className="flex min-h-11 items-center gap-3 text-sm text-limestone-300">
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-copper-500"
                      checked={acknowledgedWarnings.includes(code)}
                      onChange={(event) => setAcknowledgedWarnings((current) => event.currentTarget.checked
                        ? [...current, code]
                        : current.filter((item) => item !== code))}
                    />
                    <span>Confirm warning <code className="font-mono text-copper-200">{code}</code></span>
                  </label>
                ))}
              </fieldset>
            ) : null}
            <div>
              <label className="field-label" htmlFor="publish-changelog">Version notes</label>
              <input id="publish-changelog" className="field-control" value={changelog} onChange={(event) => setChangelog(event.currentTarget.value)} placeholder="Summarize what this version changes" />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <button type="button" className="button-primary" onClick={() => void handlePublish()} disabled={publishDisabled}>
                {publishLoading ? "Publishing…" : "Publish version"}
              </button>
              {dirty ? <p className="text-xs text-limestone-500">Wait for the draft to save before publishing.</p> : null}
            </div>
          </section>

          <section className="surface space-y-4 p-5 sm:p-6">
            <div className="flex flex-wrap items-start justify-between gap-3 border-b border-ink-800 pb-4">
              <div>
                <h2 className="text-lg font-semibold text-limestone-100">Version history</h2>
                <p className="mt-1 text-sm text-limestone-400">Restore a published version into the mutable draft without changing its original version.</p>
              </div>
              <span className="inline-flex items-center gap-1 text-xs text-limestone-500"><Clock3 className="h-3.5 w-3.5" />{versions.length} versions</span>
            </div>
            {versionsError ? (
              <div role="alert" className="flex flex-wrap items-center justify-between gap-3 text-sm text-rust-200">
                <span>Could not load version history: {versionsError}</span>
                <button type="button" className="button-secondary" onClick={() => setRefreshKey((value) => value + 1)}>Retry</button>
              </div>
            ) : versions.length === 0 ? (
              <p className="text-sm text-limestone-400">No published versions yet.</p>
            ) : (
              <ol className="divide-y divide-ink-800">
                {versions.map((version) => (
                  <li key={version.version_id} className="flex flex-col gap-3 py-4 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0">
                      <h3 className="font-medium text-limestone-100">Version {version.version}</h3>
                      <p className="mt-1 text-sm text-limestone-400">{version.changelog || "No version notes."}</p>
                      <p className="mt-1 text-xs text-limestone-500"><time dateTime={version.created_at}>{formatDate(version.created_at)}</time> · compiler {version.compiler_version}</p>
                      <p className="mt-1 break-all font-mono text-[10px] text-limestone-600">Artifact {version.artifact_hash}</p>
                    </div>
                    <button
                      type="button"
                      className="button-secondary shrink-0"
                      onClick={() => void handleRestoreVersion(version)}
                      disabled={restoringVersionId !== null || saveStatus === "saving" || pendingSaveCountRef.current > 0}
                    >
                      <RotateCcw className="h-4 w-4" />
                      {restoringVersionId === version.version_id ? "Restoring…" : "Restore to draft"}
                    </button>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </main>

        <aside className={`${mobileTab === "test" ? "block" : "hidden"} lg:sticky lg:top-4 lg:block`}>
          <ChatTestPanel key={agentId} agentId={agentId} draft={draft} versions={versions} onRefreshDraftSnapshot={refreshDraftSnapshot} />
        </aside>
      </div>
    </div>
  );
}

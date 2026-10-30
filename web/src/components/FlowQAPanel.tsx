"use client";

import { useEffect, useState } from "react";
import { Check, CircleStop, Play, RefreshCw } from "lucide-react";
import {
  platformAgentApi,
  type DraftView,
  type GenerationModel,
  type QAJobStatus,
  type QAJobView,
  type QACatalogView,
  type QAScenarioView,
  type QASuggestionView,
  type VersionView,
} from "@/lib/platform-agent-client";

interface FlowQAPanelProps {
  agentId: string;
  draft: DraftView;
  versions: VersionView[];
  draftReady: boolean;
  onRefreshDraftSnapshot: () => Promise<boolean>;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The platform request failed.";
}

function isActive(status: QAJobStatus | null): boolean {
  return status === "QUEUED" || status === "RUNNING";
}

function statusLabel(status: QAJobStatus): string {
  switch (status) {
    case "QUEUED": return "Queued";
    case "RUNNING": return "Running";
    case "COMPLETED": return "Completed";
    case "FAILED": return "Failed";
    case "CANCELLED": return "Cancelled";
    case "EXPIRED": return "Expired";
  }
}

function formatTokens(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toLocaleString();
}

function ScenarioRow({
  scenario,
  checked,
  disabled,
  onChange,
}: {
  scenario: QAScenarioView;
  checked: boolean;
  disabled: boolean;
  onChange: () => void;
}) {
  return (
    <label className="flex cursor-pointer gap-3 border-b border-ink-800 py-3 last:border-b-0">
      <input
        type="checkbox"
        className="mt-1 accent-copper-400"
        checked={checked}
        disabled={disabled}
        onChange={onChange}
        aria-label={`Include ${scenario.case_key}`}
      />
      <span className="min-w-0">
        <span className="block text-xs font-medium text-limestone-200">{scenario.case_key}</span>
        <span className="mt-1 block text-[11px] leading-4 text-limestone-500">{scenario.task}</span>
      </span>
    </label>
  );
}

function SuggestionCard({
  suggestion,
  canApply,
  busy,
  onApply,
  onDismiss,
}: {
  suggestion: QASuggestionView;
  canApply: boolean;
  busy: boolean;
  onApply: () => void;
  onDismiss: () => void;
}) {
  return (
    <li className="space-y-2 rounded-lg border border-copper-800/60 bg-copper-950/20 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-xs font-semibold text-limestone-100">{suggestion.title}</h4>
        <span className="rounded border border-ink-700 px-2 py-0.5 text-[10px] text-limestone-400">
          {suggestion.status === "DRAFT" ? "Draft suggestion" : suggestion.status.replaceAll("_", " ").toLowerCase()}
        </span>
      </div>
      <p className="text-xs leading-5 text-limestone-300">{suggestion.summary}</p>
      {suggestion.proposed_patch ? (
        <details className="text-[10px] text-limestone-500">
          <summary className="cursor-pointer">Suggested draft edit</summary>
          <p className="mt-1 break-all font-mono">{suggestion.proposed_patch.path}</p>
          <p className="mt-1 whitespace-pre-wrap">{suggestion.proposed_patch.append_text}</p>
        </details>
      ) : null}
      {suggestion.status === "DRAFT" ? (
        <div className="flex flex-wrap gap-2 pt-1">
          <button
            type="button"
            className="button-secondary min-h-9 px-3 text-xs"
            onClick={onApply}
            disabled={!canApply || busy || !suggestion.proposed_patch}
          >
            <Check className="h-3.5 w-3.5" />Apply to draft
          </button>
          <button
            type="button"
            className="button-secondary min-h-9 px-3 text-xs"
            onClick={onDismiss}
            disabled={busy}
          >
            Dismiss
          </button>
        </div>
      ) : null}
    </li>
  );
}

export function FlowQAPanel({
  agentId,
  draft,
  versions,
  draftReady,
  onRefreshDraftSnapshot,
}: FlowQAPanelProps) {
  const [selectedSnapshotId, setSelectedSnapshotId] = useState("draft");
  const [models, setModels] = useState<GenerationModel[]>([]);
  const [generationModelId, setGenerationModelId] = useState("");
  const [modelsLoading, setModelsLoading] = useState(true);
  const [catalog, setCatalog] = useState<QACatalogView | null>(null);
  const [catalogSnapshotHash, setCatalogSnapshotHash] = useState<string | null>(null);
  const [selectedScenarioIds, setSelectedScenarioIds] = useState<string[]>([]);
  const [maxTurns, setMaxTurns] = useState(12);
  const [maxTokens, setMaxTokens] = useState(4096);
  const [maxRuntimeSeconds, setMaxRuntimeSeconds] = useState(120);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<QAJobStatus | null>(null);
  const [job, setJob] = useState<QAJobView | null>(null);
  const [pinnedDraftRevision, setPinnedDraftRevision] = useState<number | null>(null);
  const [scenariosLoading, setScenariosLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [suggestionActionId, setSuggestionActionId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    setModelsLoading(true);
    void platformAgentApi.listGenerationModels().then((items) => {
      if (!current) return;
      setModels(items);
      setGenerationModelId((selected) => selected || items[0]?.id || "");
    }).catch((requestError: unknown) => {
      if (current) setError(errorMessage(requestError));
    }).finally(() => {
      if (current) setModelsLoading(false);
    });
    return () => { current = false; };
  }, []);

  useEffect(() => {
    if (selectedSnapshotId !== "draft" && !versions.some((version) => version.version_id === selectedSnapshotId)) {
      setSelectedSnapshotId("draft");
    }
  }, [selectedSnapshotId, versions]);

  useEffect(() => {
    if (!jobId) return;
    let current = true;
    let timer: number | null = null;
    let lastStatus: QAJobStatus = "QUEUED";
    const poll = async () => {
      try {
        const updated = await platformAgentApi.getQAJob(jobId);
        if (!current) return;
        lastStatus = updated.status;
        setJob(updated);
        setJobStatus(updated.status);
        setError(null);
        if (isActive(updated.status)) timer = window.setTimeout(() => void poll(), 1500);
      } catch (requestError: unknown) {
        if (!current) return;
        setError(errorMessage(requestError));
        if (isActive(lastStatus)) timer = window.setTimeout(() => void poll(), 2500);
      }
    };
    void poll();
    return () => {
      current = false;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [jobId]);

  const selectedVersion = versions.find((version) => version.version_id === selectedSnapshotId);
  const selectedSnapshotHash = selectedSnapshotId === "draft"
    ? draft.snapshot_hash
    : selectedVersion?.artifact_hash ?? null;
  const selectedSnapshotLabel = selectedSnapshotId === "draft"
    ? `Draft r${draft.draft_revision}`
    : selectedVersion ? `Published v${selectedVersion.version}` : "Published snapshot";
  const draftCanRun = draftReady && Boolean(draft.snapshot_hash);
  const catalogIsCurrent = Boolean(catalog && catalogSnapshotHash === selectedSnapshotHash);
  const visibleCatalog = catalogIsCurrent ? catalog : null;
  const selectedCount = selectedScenarioIds.length;
  const active = isActive(jobStatus);

  const loadScenarios = async () => {
    if (!selectedSnapshotHash || (selectedSnapshotId === "draft" && !draftCanRun)) return;
    setScenariosLoading(true);
    setError(null);
    setNotice(null);
    try {
      const result = await platformAgentApi.listQAScenarios(agentId, selectedSnapshotHash, 64);
      setCatalog(result);
      setCatalogSnapshotHash(selectedSnapshotHash);
      setSelectedScenarioIds(result.scenarios.map((scenario) => scenario.scenario_id));
    } catch (requestError: unknown) {
      setCatalog(null);
      setCatalogSnapshotHash(null);
      setSelectedScenarioIds([]);
      setError(errorMessage(requestError));
    } finally {
      setScenariosLoading(false);
    }
  };

  const toggleScenario = (scenarioId: string) => {
    setSelectedScenarioIds((current) => current.includes(scenarioId)
      ? current.filter((item) => item !== scenarioId)
      : [...current, scenarioId]);
  };

  const createJob = async () => {
    if (!visibleCatalog || !selectedSnapshotHash || !generationModelId || selectedCount === 0 ||
        (selectedSnapshotId === "draft" && !draftCanRun) || active) return;
    setSubmitting(true);
    setError(null);
    setNotice(null);
    setJob(null);
    setJobId(null);
    setJobStatus(null);
    setPinnedDraftRevision(selectedSnapshotId === "draft" ? draft.draft_revision : null);
    try {
      const accepted = await platformAgentApi.createQAJob({
        agent_id: agentId,
        snapshot_hash: selectedSnapshotHash,
        scenario_count: selectedCount,
        scenario_ids: selectedScenarioIds,
        max_turns: maxTurns,
        max_tokens: maxTokens,
        max_runtime_seconds: maxRuntimeSeconds,
        utterance_model_id: generationModelId,
      });
      setJobId(accepted.job_id);
      setJobStatus(accepted.status);
      setNotice(`${selectedCount} frozen scenario${selectedCount === 1 ? "" : "s"} queued on ${selectedSnapshotLabel}.`);
    } catch (requestError: unknown) {
      setError(errorMessage(requestError));
    } finally {
      setSubmitting(false);
    }
  };

  const cancelJob = async () => {
    if (!jobId || !active) return;
    setSubmitting(true);
    setError(null);
    try {
      const cancelled = await platformAgentApi.cancelQAJob(jobId);
      setJobStatus(cancelled.status);
      const latest = await platformAgentApi.getQAJob(jobId);
      setJob(latest);
      setJobStatus(latest.status);
    } catch (requestError: unknown) {
      setError(errorMessage(requestError));
    } finally {
      setSubmitting(false);
    }
  };

  const refreshJob = async () => {
    if (!jobId) return;
    setError(null);
    try {
      const latest = await platformAgentApi.getQAJob(jobId);
      setJob(latest);
      setJobStatus(latest.status);
    } catch (requestError: unknown) {
      setError(errorMessage(requestError));
    }
  };

  const applySuggestion = async (suggestion: QASuggestionView) => {
    if (!jobId || !job || pinnedDraftRevision === null || job.snapshot_kind !== "draft" ||
        job.status !== "COMPLETED" || !suggestion.draft_only || suggestion.status !== "DRAFT") return;
    setSuggestionActionId(suggestion.suggestion_id);
    setError(null);
    setNotice(null);
    try {
      const applied = await platformAgentApi.applyQASuggestion(
        jobId,
        suggestion.suggestion_id,
        pinnedDraftRevision,
      );
      const refreshed = await onRefreshDraftSnapshot();
      const latest = await platformAgentApi.getQAJob(jobId);
      setJob(latest);
      setNotice(refreshed
        ? `Suggestion applied to draft r${applied.draft_revision}. It was not published.`
        : `Suggestion applied to draft r${applied.draft_revision}; refresh the workspace draft before continuing. It was not published.`);
    } catch (requestError: unknown) {
      setError(errorMessage(requestError));
    } finally {
      setSuggestionActionId(null);
    }
  };

  const dismissSuggestion = async (suggestion: QASuggestionView) => {
    if (!jobId || job?.status !== "COMPLETED" || suggestion.status !== "DRAFT") return;
    setSuggestionActionId(suggestion.suggestion_id);
    setError(null);
    setNotice(null);
    try {
      await platformAgentApi.dismissQASuggestion(jobId, suggestion.suggestion_id);
      setJob(await platformAgentApi.getQAJob(jobId));
      setNotice("Suggestion dismissed. The draft and published version were not changed.");
    } catch (requestError: unknown) {
      setError(errorMessage(requestError));
    } finally {
      setSuggestionActionId(null);
    }
  };

  const canCreate = Boolean(
    visibleCatalog && selectedSnapshotHash && generationModelId && selectedCount > 0 && !active &&
    (selectedSnapshotId !== "draft" || draftCanRun),
  );
  const canApplySuggestion = Boolean(
    job?.snapshot_kind === "draft" &&
    job.status === "COMPLETED" &&
    pinnedDraftRevision !== null &&
    draftReady &&
    draft.draft_revision === pinnedDraftRevision,
  );
  const statusCounts = job?.coverage.scenario_status_counts;
  const scenarioStatusSummary =
    typeof statusCounts === "object" && statusCounts !== null && !Array.isArray(statusCounts)
      ? (["PASS", "FAIL", "INCONCLUSIVE", "ERROR"] as const)
        .map((status) => {
          const count = statusCounts[status];
          return `${status}: ${typeof count === "number" ? count : 0}`;
        })
        .join(" · ")
      : null;
  const coveredEdges = typeof job?.coverage.covered_edge_count === "number"
    ? job.coverage.covered_edge_count
    : null;

  return (
    <section aria-labelledby="flow-qa-heading" className="mt-4 overflow-hidden rounded-2xl border border-ink-800 bg-ink-950/70 shadow-xl shadow-black/20">
      <header className="space-y-2 border-b border-ink-800 p-4">
        <div className="flex items-center justify-between gap-3">
          <h2 id="flow-qa-heading" className="text-lg font-semibold text-limestone-100">Flow QA</h2>
          <span className="rounded-md border border-copper-800/70 bg-copper-950/30 px-2 py-1 text-[10px] uppercase tracking-wide text-copper-200">Frozen run</span>
        </div>
        <p className="text-xs leading-5 text-limestone-400">Choose saved scenarios, then run bounded checks against one draft or published snapshot. Suggestions stay in draft until you choose an action.</p>
      </header>

      <div className="space-y-4 p-4">
        <div>
          <label htmlFor="qa-snapshot" className="field-label text-xs">QA snapshot</label>
          <select
            id="qa-snapshot"
            className="field-control text-sm"
            value={selectedSnapshotId}
            onChange={(event) => setSelectedSnapshotId(event.currentTarget.value)}
            disabled={scenariosLoading || submitting}
          >
            <option value="draft">Draft r{draft.draft_revision}</option>
            {versions.map((version) => (
              <option key={version.version_id} value={version.version_id}>Published v{version.version}</option>
            ))}
          </select>
          {selectedSnapshotId === "draft" && !draftCanRun ? (
            <p role="status" className="mt-2 text-[11px] leading-4 text-copper-200">
              Save the current draft changes before loading or running its snapshot.
            </p>
          ) : selectedSnapshotHash ? (
            <p className="mt-2 break-all font-mono text-[10px] text-limestone-500">{selectedSnapshotLabel} · {selectedSnapshotHash}</p>
          ) : (
            <p role="status" className="mt-2 text-[11px] text-copper-200">This snapshot has no executable flow yet.</p>
          )}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-xs font-semibold text-limestone-200">Frozen scenarios</h3>
            <p className="mt-1 text-[10px] leading-4 text-limestone-500">Preview the owner-scoped catalog for this exact snapshot before queueing a job.</p>
          </div>
          <button
            type="button"
            className="button-secondary min-h-9 px-3 text-xs"
            onClick={() => void loadScenarios()}
            disabled={!selectedSnapshotHash || (selectedSnapshotId === "draft" && !draftCanRun) || scenariosLoading || submitting}
          >
            {scenariosLoading ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : null}
            {scenariosLoading ? "Loading…" : "Load scenarios"}
          </button>
        </div>

        {visibleCatalog ? (
          <div className="rounded-lg border border-ink-800 bg-ink-950/70 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-limestone-400">
              <span>{selectedCount} of {visibleCatalog.scenario_count} selected</span>
              {visibleCatalog.omitted_scenario_count > 0 ? (
                <span className="text-copper-200">{visibleCatalog.omitted_scenario_count} additional non-branch cases omitted by the catalog limit.</span>
              ) : null}
            </div>
            <div className="mt-2 max-h-64 overflow-y-auto pr-1">
              {visibleCatalog.scenarios.map((scenario) => (
                <ScenarioRow
                  key={scenario.scenario_id}
                  scenario={scenario}
                  checked={selectedScenarioIds.includes(scenario.scenario_id)}
                  disabled={active || submitting}
                  onChange={() => toggleScenario(scenario.scenario_id)}
                />
              ))}
            </div>
          </div>
        ) : (
          <div className="rounded-lg border border-dashed border-ink-700 p-3 text-xs text-limestone-500">Load the frozen scenario catalog to select what this job will run.</div>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <label htmlFor="qa-utterance-model" className="field-label text-xs">Utterance model</label>
            <select
              id="qa-utterance-model"
              className="field-control text-sm"
              value={generationModelId}
              onChange={(event) => setGenerationModelId(event.currentTarget.value)}
              disabled={modelsLoading || active || submitting || models.length === 0}
            >
              {models.length === 0 ? <option value="">{modelsLoading ? "Loading models…" : "No qualified models available"}</option> : null}
              {models.map((model) => <option key={model.id} value={model.id}>{model.name} · {model.id}</option>)}
            </select>
          </div>
          <label className="text-xs text-limestone-400" htmlFor="qa-max-turns">
            Max turns
            <input id="qa-max-turns" className="field-control mt-1 text-sm" type="number" min={1} max={50} value={maxTurns} onChange={(event) => setMaxTurns(Number(event.currentTarget.value))} disabled={active || submitting} />
          </label>
          <label className="text-xs text-limestone-400" htmlFor="qa-max-tokens">
            Total token budget
            <input id="qa-max-tokens" className="field-control mt-1 text-sm" type="number" min={64} max={16384} step={64} value={maxTokens} onChange={(event) => setMaxTokens(Number(event.currentTarget.value))} disabled={active || submitting} />
          </label>
          <label className="text-xs text-limestone-400 sm:col-span-2" htmlFor="qa-max-runtime">
            Runtime limit (seconds)
            <input id="qa-max-runtime" className="field-control mt-1 text-sm" type="number" min={1} max={300} value={maxRuntimeSeconds} onChange={(event) => setMaxRuntimeSeconds(Number(event.currentTarget.value))} disabled={active || submitting} />
          </label>
        </div>

        <div className="flex flex-wrap gap-2">
          <button type="button" className="button-primary min-h-10 px-3 text-xs" onClick={() => void createJob()} disabled={!canCreate || submitting}>
            {submitting && !active ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
            {submitting && !active ? "Queueing…" : "Run selected scenarios"}
          </button>
          {jobId && active ? (
            <button type="button" className="button-secondary min-h-10 px-3 text-xs" onClick={() => void cancelJob()} disabled={submitting}>
              <CircleStop className="h-3.5 w-3.5" />Cancel job
            </button>
          ) : null}
          {jobId && !active ? (
            <button type="button" className="button-secondary min-h-10 px-3 text-xs" onClick={() => void refreshJob()} disabled={submitting}>
              <RefreshCw className="h-3.5 w-3.5" />Refresh results
            </button>
          ) : null}
        </div>

        {jobId ? (
          <section aria-labelledby="qa-job-heading" className="space-y-3 border-t border-ink-800 pt-4">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <h3 id="qa-job-heading" className="text-sm font-semibold text-limestone-100">Current QA job</h3>
                <p className="mt-1 break-all font-mono text-[10px] text-limestone-500">{jobId}</p>
              </div>
              <span role="status" className="rounded-md border border-ink-700 px-2 py-1 text-[10px] text-limestone-300">
                {jobStatus ? statusLabel(jobStatus) : "Loading status"}
              </span>
            </div>
            {pinnedDraftRevision !== null ? <p className="text-[10px] text-limestone-500">Pinned to draft revision r{pinnedDraftRevision}; any suggestion requires your explicit apply action.</p> : null}
            {job?.failure_code ? <p className="text-xs text-rust-200">{job.failure_code}</p> : null}
            {job?.usage ? (
              <p className="text-[10px] text-limestone-400">Tokens: {formatTokens(job.usage.total_tokens)} / {formatTokens(job.budget.max_tokens)} · runtime {job.runtime_seconds === null ? "—" : `${job.runtime_seconds.toFixed(1)}s`}</p>
            ) : null}
            {job ? (
              <div className="rounded-lg border border-ink-800 bg-ink-950/70 p-3">
                <h4 className="text-xs font-semibold text-limestone-200">Coverage summary</h4>
                <p className="mt-1 text-[10px] text-limestone-400">{scenarioStatusSummary ?? "Scenario outcome counts are pending."}</p>
                <p className="mt-1 text-[10px] text-limestone-500">
                  {coveredEdges === null ? "No transitions observed yet." : `${coveredEdges} unique transitions observed.`}
                </p>
              </div>
            ) : null}
            {job && job.results.length > 0 ? (
              <ol aria-label="QA results" className="max-h-72 space-y-2 overflow-y-auto">
                {job.results.map((result) => {
                  const scenario = job.scenarios.find((item) => item.scenario_id === result.scenario_id);
                  return (
                    <li key={result.scenario_id} className="rounded-lg border border-ink-800 bg-ink-950 p-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <h4 className="text-xs font-semibold text-limestone-200">{scenario?.case_key ?? result.kind}</h4>
                        <span className={`rounded border px-2 py-0.5 text-[10px] ${result.status === "PASS" ? "border-moss-800 text-moss-200" : result.status === "FAIL" || result.status === "ERROR" ? "border-rust-800 text-rust-200" : "border-ink-700 text-limestone-400"}`}>
                          {result.status}
                        </span>
                      </div>
                      <p className="mt-2 text-xs leading-5 text-limestone-400">{result.assertion}</p>
                      {result.failure_code ? <p className="mt-1 font-mono text-[10px] text-rust-200">{result.failure_code}</p> : null}
                    </li>
                  );
                })}
              </ol>
            ) : job && !active ? (
              <p className="text-xs text-limestone-500">This job has no completed scenario results.</p>
            ) : (
              <p role="status" className="text-xs text-limestone-500">Waiting for the worker to return results…</p>
            )}
            {job?.suggestions.length ? (
              <div className="space-y-2 border-t border-ink-800 pt-3">
                <h4 className="text-xs font-semibold text-limestone-200">Draft-only suggestions</h4>
                {job.snapshot_kind !== "draft" ? <p className="text-[10px] text-limestone-500">Suggestions from published runs cannot be applied to a draft.</p> : null}
                {job.snapshot_kind === "draft" && pinnedDraftRevision !== null &&
                    (!draftReady || draft.draft_revision !== pinnedDraftRevision) ? (
                  <p className="text-[10px] leading-4 text-copper-200">This saved draft changed after the QA run. Apply is disabled; run QA again on the current revision.</p>
                ) : null}
                <ul className="space-y-2">
                  {job.suggestions.map((suggestion) => (
                    <SuggestionCard
                      key={suggestion.suggestion_id}
                      suggestion={suggestion}
                      canApply={canApplySuggestion}
                      busy={suggestionActionId !== null}
                      onApply={() => void applySuggestion(suggestion)}
                      onDismiss={() => void dismissSuggestion(suggestion)}
                    />
                  ))}
                </ul>
              </div>
            ) : null}
          </section>
        ) : null}

        {error ? <div role="alert" className="rounded-lg border border-rust-800 bg-rust-950/30 p-3 text-xs leading-5 text-rust-200">{error}</div> : null}
        {notice ? <p role="status" className="text-xs leading-5 text-moss-200">{notice}</p> : null}
        <p className="text-[10px] leading-4 text-limestone-600">Flow QA only writes job results. It never publishes a version; review every suggestion before applying it to the draft.</p>
      </div>
    </section>
  );
}

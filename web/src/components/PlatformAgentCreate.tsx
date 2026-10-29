"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, RefreshCw, WandSparkles } from "lucide-react";
import {
  platformAgentApi,
  type AgentCreateRequest,
  type AgentProposal,
  type AgentTemplateView,
  type GenerationJobAccepted,
  type GenerationJobView,
  type GenerationModel,
  type GenerationStatus,
} from "@/lib/platform-agent-client";

interface PlatformAgentCreateProps {
  initialMode: "manual" | "generate";
  initialJobId?: string | null;
}

interface KeyRecord {
  fingerprint: string;
  key: string;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The platform request failed.";
}

function idempotencyKeyFor(ref: { current: KeyRecord | null }, fingerprint: string): string {
  if (ref.current?.fingerprint === fingerprint) return ref.current.key;
  if (!globalThis.crypto?.randomUUID) {
    throw new Error("This browser cannot create an idempotency key for the requested action.");
  }
  const key = globalThis.crypto.randomUUID();
  ref.current = { fingerprint, key };
  return key;
}

function isRunning(status: GenerationStatus | undefined): boolean {
  return status === "QUEUED" || status === "RUNNING";
}

export function PlatformAgentCreate({ initialMode, initialJobId = null }: PlatformAgentCreateProps) {
  const router = useRouter();
  const [mode, setMode] = useState<"manual" | "generate">(initialMode);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [personaPrompt, setPersonaPrompt] = useState("");
  const [templateVersionId, setTemplateVersionId] = useState("");
  const [templates, setTemplates] = useState<AgentTemplateView[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(true);
  const [templatesError, setTemplatesError] = useState<string | null>(null);
  const [templateRetry, setTemplateRetry] = useState(0);
  const [models, setModels] = useState<GenerationModel[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [modelsRetry, setModelsRetry] = useState(0);
  const [requirement, setRequirement] = useState("");
  const [generationModelId, setGenerationModelId] = useState("");
  const [language, setLanguage] = useState<"" | "en" | "hi">("");
  const [tokenBudget, setTokenBudget] = useState("");
  const [job, setJob] = useState<GenerationJobAccepted | null>(null);
  const [jobDetails, setJobDetails] = useState<GenerationJobView | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [pollNonce, setPollNonce] = useState(0);
  const [pageError, setPageError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [createdAgentId, setCreatedAgentId] = useState<string | null>(null);
  const manualKeyRef = useRef<KeyRecord | null>(null);
  const generatedKeyRef = useRef<KeyRecord | null>(null);

  useEffect(() => {
    let current = true;
    setTemplatesLoading(true);
    setTemplatesError(null);
    platformAgentApi.listAllTemplates().then((items) => {
      if (current) setTemplates(items);
    }).catch((error: unknown) => {
      if (current) setTemplatesError(errorMessage(error));
    }).finally(() => {
      if (current) setTemplatesLoading(false);
    });
    return () => { current = false; };
  }, [templateRetry]);

  useEffect(() => {
    if (mode !== "generate") return;
    let current = true;
    setModelsLoading(true);
    setModelsError(null);
    platformAgentApi.listGenerationModels().then((result) => {
      if (current) setModels(result);
    }).catch((error: unknown) => {
      if (current) setModelsError(errorMessage(error));
    }).finally(() => {
      if (current) setModelsLoading(false);
    });
    return () => { current = false; };
  }, [mode, modelsRetry]);

  useEffect(() => {
    if (generationModelId && models.length > 0 && !models.some((model) => model.id === generationModelId)) {
      setGenerationModelId("");
    }
  }, [generationModelId, models]);
  useEffect(() => {
    if (!initialJobId) return;
    let current = true;
    setPageError(null);
    platformAgentApi.getGenerationJob(initialJobId).then((details) => {
      if (!current) return;
      setJob({ job_id: initialJobId, status: details.status });
      setJobDetails(details);
    }).catch((error: unknown) => {
      if (current) setPageError(`Could not recover generation job ${initialJobId}: ${errorMessage(error)}`);
    });
    return () => { current = false; };
  }, [initialJobId]);

  useEffect(() => {
    if (!job || !isRunning(job.status)) return;
    let current = true;
    const timer = window.setTimeout(() => {
      platformAgentApi.getGenerationJob(job.job_id).then((details) => {
        if (!current) return;
        setJobDetails(details);
        setJob((existing) => existing ? { ...existing, status: details.status } : existing);
        setPollError(null);
        if (isRunning(details.status)) setPollNonce((value) => value + 1);
      }).catch((error: unknown) => {
        if (current) setPollError(errorMessage(error));
      });
    }, 2000);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [job?.job_id, job?.status, pollNonce]);

  const switchMode = (nextMode: "manual" | "generate") => {
    setMode(nextMode);
    setPageError(null);
    const jobQuery = job ? `&job=${encodeURIComponent(job.job_id)}` : "";
    router.replace(`/agents/new?mode=${nextMode}${jobQuery}`, { scroll: false });
  };

  const manualRequest = (): AgentCreateRequest => ({
    name: name.trim(),
    description: description.trim(),
    source: templateVersionId ? "template" : "manual",
    ...(personaPrompt.trim() ? { persona_prompt: personaPrompt.trim() } : {}),
    ...(templateVersionId ? { template_version_id: templateVersionId } : {}),
  });

  const handleManualCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const request = manualRequest();
    setSubmitting(true);
    setPageError(null);
    try {
      const key = idempotencyKeyFor(manualKeyRef, JSON.stringify(request));
      const agent = await platformAgentApi.createAgent(request, key);
      router.push(`/agents/${agent.id}`);
    } catch (error: unknown) {
      setPageError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  const handleStartGeneration = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setPageError(null);
    setPollError(null);
    setJob(null);
    setJobDetails(null);
    setCreatedAgentId(null);
    generatedKeyRef.current = null;
    try {
      const accepted = await platformAgentApi.startGenerationJob({
        requirement: requirement.trim(),
        generation_model_id: generationModelId,
        ...(language ? { language } : {}),
        token_budget: Number(tokenBudget),
      });
      setJob(accepted);
      router.replace(`/agents/new?mode=generate&job=${encodeURIComponent(accepted.job_id)}`, { scroll: false });
    } catch (error: unknown) {
      setPageError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancelGeneration = async () => {
    if (!job || !isRunning(job.status)) return;
    setSubmitting(true);
    setPageError(null);
    try {
      const cancelled = await platformAgentApi.cancelGenerationJob(job.job_id);
      setJob(cancelled);
      setJobDetails(null);
      setPollError(null);
    } catch (error: unknown) {
      setPageError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  const handleSaveProposal = async () => {
    const proposal = jobDetails?.proposal;
    if (!proposal) return;
    setSubmitting(true);
    setPageError(null);
    try {
      const request: AgentCreateRequest = {
        name: proposal.name,
        description: proposal.description,
        persona_prompt: proposal.persona_prompt,
        source: "generated",
      };
      const key = idempotencyKeyFor(generatedKeyRef, JSON.stringify({ request, config: proposal.config }));
      let agentId = createdAgentId;
      if (!agentId) {
        const agent = await platformAgentApi.createAgent(request, key);
        agentId = agent.id;
        setCreatedAgentId(agentId);
      }
      const draft = await platformAgentApi.getDraft(agentId);
      await platformAgentApi.patchDraft(agentId, {
        expected_revision: draft.draft_revision,
        config: proposal.config,
      });
      router.push(`/agents/${agentId}`);
    } catch (error: unknown) {
      setPageError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  const recoverPoll = () => {
    setPollError(null);
    setPollNonce((value) => value + 1);
  };

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <Link href="/agents" className="inline-flex items-center gap-2 text-sm text-limestone-400 hover:text-limestone-100">
        <ArrowLeft className="h-4 w-4" />Back to agents
      </Link>

      <header className="border-b border-ink-800 pb-5">
        <p className="text-sm font-medium text-copper-300">New agent</p>
        <h1 className="mt-1 text-3xl font-semibold tracking-tight text-limestone-100">Create an agent draft</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">
          Start with a blank or versioned template, or ask a configured generator for a proposal you can review before saving.
        </p>
      </header>

      {pageError ? (
        <div role="alert" className="surface space-y-3 p-4 text-sm text-rust-200">
          <p>{pageError}</p>
          {createdAgentId ? (
            <Link className="button-secondary" href={`/agents/${createdAgentId}`}>Open the created draft</Link>
          ) : null}
          <button type="button" className="button-secondary" onClick={() => setPageError(null)}>Dismiss</button>
        </div>
      ) : null}

      <div role="tablist" aria-label="Agent creation method" className="flex flex-wrap gap-2">
        <button
          type="button"
          role="tab"
          aria-selected={mode === "manual"}
          className={mode === "manual" ? "button-primary" : "button-secondary"}
          onClick={() => switchMode("manual")}
        >
          Create manually
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "generate"}
          className={mode === "generate" ? "button-primary" : "button-secondary"}
          onClick={() => switchMode("generate")}
        >
          <WandSparkles className="h-4 w-4" />Generate from prompt
        </button>
      </div>

      {mode === "manual" ? (
        <section role="tabpanel" aria-label="Create manually" className="surface p-5 sm:p-7">
          <form className="space-y-5" onSubmit={handleManualCreate}>
            <div>
              <label className="field-label" htmlFor="manual-agent-name">Agent name</label>
              <input
                id="manual-agent-name"
                className="field-control"
                value={name}
                onChange={(event) => setName(event.currentTarget.value)}
                autoComplete="off"
                required
              />
            </div>
            <div>
              <label className="field-label" htmlFor="manual-agent-description">Purpose</label>
              <textarea
                id="manual-agent-description"
                className="field-control min-h-24"
                value={description}
                onChange={(event) => setDescription(event.currentTarget.value)}
                placeholder="What should this agent help your customers with?"
              />
            </div>
            <div>
              <label className="field-label" htmlFor="manual-agent-persona">Persona instructions <span className="font-normal text-limestone-500">(optional)</span></label>
              <textarea
                id="manual-agent-persona"
                className="field-control min-h-32 font-mono text-sm"
                value={personaPrompt}
                onChange={(event) => setPersonaPrompt(event.currentTarget.value)}
                placeholder="Leave blank to use the platform's manual-agent default."
              />
            </div>
            <div>
              <label className="field-label" htmlFor="manual-agent-template">Start from a template</label>
              <select
                id="manual-agent-template"
                className="field-control"
                value={templateVersionId}
                onChange={(event) => setTemplateVersionId(event.currentTarget.value)}
                disabled={templatesLoading}
              >
                <option value="">Blank flow</option>
                {templates.map((template) => (
                  <option key={template.id} value={template.id}>{template.name} · v{template.version}</option>
                ))}
              </select>
              {templatesLoading ? <p role="status" className="mt-1 text-xs text-limestone-500">Loading available templates…</p> : null}
              {templatesError ? (
                <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-rust-200">
                  <span>Templates could not be loaded: {templatesError}</span>
                  <button type="button" className="button-secondary" onClick={() => setTemplateRetry((value) => value + 1)}>Retry</button>
                </div>
              ) : null}
              {!templatesLoading && templates.length === 0 && !templatesError ? (
                <p className="mt-1 text-xs text-limestone-500">No templates are available. A blank flow will be created.</p>
              ) : null}
            </div>
            <div className="flex flex-wrap items-center gap-3 border-t border-ink-800 pt-5">
              <button type="submit" className="button-primary" disabled={submitting || !name.trim()}>
                {submitting ? "Creating draft…" : "Create draft and configure"}
              </button>
              <p className="text-xs text-limestone-500">The agent is created as a draft and is not published.</p>
            </div>
          </form>
        </section>
      ) : (
        <section role="tabpanel" aria-label="Generate from prompt" className="space-y-5">
          <form className="surface space-y-5 p-5 sm:p-7" onSubmit={handleStartGeneration}>
            <div>
              <label className="field-label" htmlFor="generation-requirement">What should the agent do?</label>
              <textarea
                id="generation-requirement"
                className="field-control min-h-36"
                value={requirement}
                onChange={(event) => setRequirement(event.currentTarget.value)}
                placeholder="Describe the customer's goal, the information to collect, and the decisions the agent should make."
                required
              />
            </div>
            <div className="grid gap-4 sm:grid-cols-3">
              <div>
                <label className="field-label" htmlFor="generation-model">Generation model</label>
                <select
                  id="generation-model"
                  className="field-control"
                  value={generationModelId}
                  onChange={(event) => setGenerationModelId(event.currentTarget.value)}
                  disabled={modelsLoading || models.length === 0}
                  required
                >
                  <option value="">Select a configured model</option>
                  {models.map((model) => (
                    <option key={model.id} value={model.id}>{model.name}</option>
                  ))}
                </select>
                {modelsLoading ? <p role="status" className="mt-1 text-xs text-limestone-500">Loading generator models…</p> : null}
                {modelsError ? (
                  <div className="mt-2 space-y-2 text-xs text-rust-200">
                    <p>Generator models could not be loaded: {modelsError}</p>
                    <button type="button" className="button-secondary" onClick={() => setModelsRetry((value) => value + 1)}>Retry</button>
                  </div>
                ) : null}
                {!modelsLoading && !modelsError && models.length === 0 ? (
                  <p className="mt-1 text-xs text-limestone-500">No generation models are configured for this account.</p>
                ) : null}
              </div>
              <div>
                <label className="field-label" htmlFor="generation-language">Proposal language</label>
                <select
                  id="generation-language"
                  className="field-control"
                  value={language}
                  onChange={(event) => setLanguage(event.currentTarget.value as "" | "en" | "hi")}
                >
                  <option value="">Use the requirement language</option>
                  <option value="en">English</option>
                  <option value="hi">Hindi</option>
                </select>
              </div>
              <div>
                <label className="field-label" htmlFor="generation-budget">Token budget</label>
                <input
                  id="generation-budget"
                  className="field-control"
                  type="number"
                  inputMode="numeric"
                  min={64}
                  step={1}
                  value={tokenBudget}
                  onChange={(event) => setTokenBudget(event.currentTarget.value)}
                  required
                />
                <p className="mt-1 text-xs text-limestone-500">At least 64; the platform enforces your configured maximum.</p>
              </div>
            </div>
            {models.find((model) => model.id === generationModelId)?.description ? (
              <p className="text-sm text-limestone-400">{models.find((model) => model.id === generationModelId)?.description}</p>
            ) : null}
            <div className="flex flex-wrap items-center gap-3 border-t border-ink-800 pt-5">
              <button
                type="submit"
                className="button-primary"
                disabled={submitting || modelsLoading || models.length === 0 || !generationModelId || !requirement.trim() || Number(tokenBudget) < 64}
              >
                {submitting ? "Starting generation…" : "Generate proposal"}
              </button>
              <p className="text-xs text-limestone-500">The proposal will be reviewed before it becomes an agent draft.</p>
            </div>
          </form>

          {job ? (
            <GenerationJobCard
              job={job}
              details={jobDetails}
              pollingError={pollError}
              submitting={submitting}
              onCancel={() => void handleCancelGeneration()}
              onRetryPoll={recoverPoll}
              onSave={() => void handleSaveProposal()}
            />
          ) : null}
        </section>
      )}
    </div>
  );
}

interface GenerationJobCardProps {
  job: GenerationJobAccepted;
  details: GenerationJobView | null;
  pollingError: string | null;
  submitting: boolean;
  onCancel: () => void;
  onRetryPoll: () => void;
  onSave: () => void;
}

function GenerationJobCard({
  job,
  details,
  pollingError,
  submitting,
  onCancel,
  onRetryPoll,
  onSave,
}: GenerationJobCardProps) {
  const proposal: AgentProposal | null = details?.proposal ?? null;
  const running = isRunning(job.status);

  return (
    <section aria-labelledby="generation-job-heading" className="surface space-y-4 p-5 sm:p-7">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-limestone-500">Generation job</p>
          <h2 id="generation-job-heading" className="mt-1 text-lg font-semibold text-limestone-100">{job.status}</h2>
          <p className="mt-1 break-all text-xs text-limestone-500">Job ID: {job.job_id}</p>
        </div>
        {running ? (
          <button type="button" className="button-secondary" onClick={onCancel} disabled={submitting}>
            {submitting ? "Cancelling…" : "Cancel generation"}
          </button>
        ) : null}
      </div>

      {running ? (
        <p role="status" className="flex items-center gap-2 text-sm text-limestone-400">
          <RefreshCw className="h-4 w-4 animate-spin text-copper-400" />Waiting for the generator…
        </p>
      ) : null}
      {pollingError ? (
        <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-rust-200">
          <span>Could not refresh job status: {pollingError}</span>
          <button type="button" className="button-secondary" onClick={onRetryPoll}>Retry status check</button>
        </div>
      ) : null}

      {details ? (
        <>
          {details.unknown_business_facts.length > 0 ? (
            <div className="rounded-lg border border-copper-900/80 bg-copper-950/20 p-4">
              <h3 className="text-sm font-semibold text-copper-200">Facts to confirm</h3>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-limestone-300">
                {details.unknown_business_facts.map((fact, index) => <li key={`${index}-${fact}`}>{fact}</li>)}
              </ul>
            </div>
          ) : null}
          {details.validation_warnings.length > 0 ? (
            <div className="rounded-lg border border-ink-700 bg-ink-950/50 p-4">
              <h3 className="text-sm font-semibold text-limestone-100">Proposal warnings</h3>
              <ul className="mt-2 space-y-2 text-sm text-limestone-300">
                {details.validation_warnings.map((warning, index) => (
                  <li key={`${warning.code}-${index}`}>
                    <span className="font-mono text-xs text-copper-300">{warning.code}</span>
                    <span className="ml-2">{warning.message}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {details.usage ? (
            <p className="text-xs text-limestone-500">
              Usage: {details.usage.prompt_tokens} prompt + {details.usage.completion_tokens} completion tokens, {details.usage.total_tokens} total.
            </p>
          ) : null}
          {job.status === "COMPLETED" && !proposal ? (
            <p role="alert" className="text-sm text-rust-200">Generation completed without a proposal. No agent draft was created.</p>
          ) : null}
          {proposal ? (
            <div className="space-y-4 border-t border-ink-800 pt-4">
              <div>
                <h3 className="text-lg font-semibold text-limestone-100">{proposal.name}</h3>
                <p className="mt-1 text-sm text-limestone-400">{proposal.description || "No purpose provided."}</p>
              </div>
              <details className="rounded-lg border border-ink-800 bg-ink-950/50 p-4">
                <summary className="cursor-pointer text-sm font-medium text-limestone-200">Review persona and full proposal</summary>
                <h4 className="mt-4 text-xs font-semibold uppercase tracking-wide text-limestone-500">Persona instructions</h4>
                <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap rounded-md bg-ink-950 p-3 text-xs leading-5 text-limestone-300">{proposal.persona_prompt}</pre>
                <h4 className="mt-4 text-xs font-semibold uppercase tracking-wide text-limestone-500">Authoring configuration</h4>
                <pre className="mt-2 max-h-80 overflow-auto rounded-md bg-ink-950 p-3 text-xs leading-5 text-limestone-300">{JSON.stringify(proposal.config, null, 2)}</pre>
              </details>
              <div className="flex flex-wrap items-center gap-3">
                <button type="button" className="button-primary" onClick={onSave} disabled={submitting}>
                  {submitting ? "Saving draft…" : "Save proposal as draft"}
                </button>
                <p className="text-xs text-limestone-500">This does not publish the agent.</p>
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

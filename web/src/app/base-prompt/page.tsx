"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Check, Clock, Copy, History, RefreshCw, Save } from "lucide-react";
import { api } from "@/lib/api-client";
import type { PromptRevision } from "@/types/agent";

export default function BasePromptPage() {
  const [content, setContent] = useState("");
  const [version, setVersion] = useState(1);
  const [revisions, setRevisions] = useState<PromptRevision[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [samplePersona, setSamplePersona] = useState("You are a friendly bilingual assistant. Use English or Hindi naturally as appropriate.");
  const [summary, setSummary] = useState("");
  const [compiledPreview, setCompiledPreview] = useState("");
  const [tokenEstimate, setTokenEstimate] = useState(0);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const previewGenerationRef = useRef(0);

  useEffect(() => {
    let current = true;
    api.getBasePrompt().then((base) => {
      if (!current) return;
      setContent(base.prompt_text ?? base.content ?? "");
      setVersion(base.version || 1);
      setRevisions(base.revisions || []);
    }).catch((err: unknown) => {
      if (current) setError(`Could not load the shared prompt: ${err instanceof Error ? err.message : String(err)}`);
    }).finally(() => {
      if (current) setLoading(false);
    });
    return () => { current = false; };
  }, []);

  const updatePreview = useCallback(async (baseText: string, personaText: string) => {
    const generation = ++previewGenerationRef.current;
    if (!baseText.trim() && !personaText.trim()) {
      setCompiledPreview("");
      setTokenEstimate(0);
      setPreviewError(null);
      setPreviewLoading(false);
      return;
    }
    setPreviewError(null);
    setPreviewLoading(true);
    try {
      const result = await api.previewPrompt({ base_prompt: baseText, persona_prompt: personaText });
      if (generation !== previewGenerationRef.current) return;
      setCompiledPreview(result.compiled_prompt);
      setTokenEstimate(result.token_estimate || Math.ceil(result.compiled_prompt.split(/\s+/).length * 1.35));
    } catch (err) {
      if (generation !== previewGenerationRef.current) return;
      setCompiledPreview("");
      setTokenEstimate(0);
      setPreviewError(err instanceof Error ? err.message : "Prompt preview is unavailable.");
    } finally {
      if (generation === previewGenerationRef.current) setPreviewLoading(false);
    }
  }, []);

  useEffect(() => {
    if (loading) return;
    const timer = setTimeout(() => { void updatePreview(content, samplePersona); }, 250);
    return () => {
      clearTimeout(timer);
      previewGenerationRef.current += 1;
    };
  }, [content, samplePersona, loading, updatePreview]);

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const updated = await api.updateBasePrompt(content, summary.trim() || undefined);
      setContent(updated.prompt_text ?? updated.content ?? "");
      setVersion(updated.version);
      setRevisions(updated.revisions || []);
      setSummary("");
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (err: unknown) {
      setError(`Could not save the shared prompt: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  };

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(compiledPreview);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Clipboard access is unavailable in this browser.");
    }
  };

  if (loading) return <div className="flex min-h-72 items-center gap-3 text-sm text-limestone-400" role="status"><RefreshCw className="h-4 w-4 animate-spin text-copper-400" />Loading shared prompt…</div>;

  return (
    <div className="max-w-6xl space-y-7">
      <header className="flex flex-col gap-4 border-b border-ink-800 pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-limestone-100">Shared instructions</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-limestone-400">Edit the base prompt used when compiling an agent’s system instructions.</p>
        </div>
        <div className="flex items-center gap-3">
          {saved && <span role="status" className="rounded-md border border-moss-700 bg-moss-950 px-3 py-2 text-sm text-moss-200"><Check className="mr-1 inline h-4 w-4" />Saved · v{version}</span>}
          <button type="button" onClick={() => void handleSave()} disabled={saving} className="button-primary">
            {saving ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            {saving ? "Saving…" : "Save revision"}
          </button>
        </div>
      </header>

      {error && <div role="alert" className="surface flex items-start gap-3 border-rust-800 bg-rust-950/40 p-4 text-sm text-rust-200"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />{error}</div>}

      <div className="grid grid-cols-1 items-start gap-7 xl:grid-cols-[minmax(0,1fr)_minmax(420px,0.9fr)]">
        <section aria-labelledby="base-prompt-heading" className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 id="base-prompt-heading" className="text-lg font-semibold text-limestone-100">Base prompt</h2>
            <span className="rounded-md border border-ink-700 px-2 py-1 text-xs text-limestone-300">Version {version}</span>
          </div>
          <textarea aria-label="Base prompt content" value={content} onChange={(event) => setContent(event.target.value)} rows={18} className="field-control min-h-[420px] resize-y font-mono leading-relaxed" placeholder="Write shared conversational rules…" />
          <div>
            <label htmlFor="revision-summary" className="field-label">Revision summary <span className="font-normal text-limestone-500">(optional)</span></label>
            <input id="revision-summary" value={summary} onChange={(event) => setSummary(event.target.value)} className="field-control" placeholder="What changed in this revision?" />
          </div>
          <p className="text-sm leading-6 text-limestone-500">The compiled prompt keeps shared policy and agent persona in separate sections.</p>
        </section>

        <section aria-labelledby="preview-heading" className="space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 id="preview-heading" className="text-lg font-semibold text-limestone-100">Compiled preview</h2>
              <p className="mt-1 text-sm text-limestone-500">{compiledPreview ? `About ${tokenEstimate} tokens` : "Token estimate appears when compilation succeeds."}</p>
            </div>
            <button type="button" onClick={() => void handleCopy()} disabled={!compiledPreview} aria-label="Copy compiled preview" className="button-secondary min-h-9 px-3">
              {copied ? <Check className="h-4 w-4 text-moss-300" /> : <Copy className="h-4 w-4" />}
            </button>
          </div>
          <div>
            <label htmlFor="sample-persona" className="field-label">Sample agent persona</label>
            <input id="sample-persona" value={samplePersona} onChange={(event) => setSamplePersona(event.target.value)} className="field-control font-mono" />
          </div>
          <pre aria-live="polite" className="surface min-h-[320px] max-h-[60vh] overflow-auto whitespace-pre-wrap p-4 font-mono text-sm leading-6 text-limestone-200">
            {previewLoading && !compiledPreview ? "Compiling preview…" : previewError ? `Compiled preview unavailable: ${previewError}` : compiledPreview || "Enter a base prompt or sample persona to preview the compiled prompt."}
          </pre>
        </section>
      </div>

      {revisions.length > 0 && <section className="space-y-3 border-t border-ink-800 pt-6" aria-labelledby="revision-history-heading">
        <h2 id="revision-history-heading" className="flex items-center gap-2 text-lg font-semibold text-limestone-100"><History className="h-4 w-4 text-copper-300" />Revision history</h2>
        <ol className="surface divide-y divide-ink-800">
          {revisions.map((revision) => <li key={revision.id} className="flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-3"><span className="text-sm font-medium text-copper-300">v{revision.version}</span><span className="text-sm text-limestone-200">{revision.change_summary || revision.change_reason || "Revision update"}</span></div>
            <time dateTime={revision.created_at} className="flex items-center gap-2 text-xs text-limestone-500"><Clock className="h-3.5 w-3.5" />{new Date(revision.created_at).toLocaleString()}</time>
          </li>)}
        </ol>
      </section>}
    </div>
  );
}

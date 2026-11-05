"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api } from "../../lib/api";
import type { GlobalPrompt } from "./admin-types";
import {
  AdminLoadError,
  AdminLoading,
  AdminNotice,
  errorDetails,
  type LoadState,
  type Notice,
} from "./admin-ui";
import { Button } from "../ui/button";
import { Label } from "../ui/label";
import { Textarea } from "../ui/textarea";

export function GlobalPromptEditor() {
  const [load, setLoad] = useState<LoadState<GlobalPrompt>>({ kind: "loading" });
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoad({ kind: "loading" });

    void api<GlobalPrompt>("/admin/global-prompt", { signal: controller.signal })
      .then((prompt) => {
        setDraft(prompt.prompt_text);
        setLoad({ kind: "ready", data: prompt });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({ kind: "failed", message: errorDetails(error, "Couldn't load the global prompt.").message });
      });

    return () => controller.abort();
  }, [retryKey]);

  async function savePrompt(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (load.kind !== "ready") return;
    if (!draft.trim()) {
      setNotice({ kind: "error", message: "Enter prompt text before saving." });
      return;
    }

    setSaving(true);
    setNotice(null);
    try {
      const updated = await api<GlobalPrompt>("/admin/global-prompt", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt_text: draft }),
      });
      setDraft(updated.prompt_text);
      setLoad({ kind: "ready", data: updated });
      setNotice({
        kind: "success",
        message:
          updated.version === load.data.version
            ? `Prompt saved. Current version is ${updated.version}.`
            : `Prompt saved as version ${updated.version}.`,
      });
    } catch (error: unknown) {
      setNotice({ kind: "error", message: errorDetails(error, "Couldn't save the global prompt.").message });
    } finally {
      setSaving(false);
    }
  }

  if (load.kind === "loading") return <AdminLoading label="Loading the global prompt…" />;
  if (load.kind === "failed") {
    return <AdminLoadError message={load.message} onRetry={() => setRetryKey((key) => key + 1)} />;
  }

  const hasChanges = draft !== load.data.prompt_text;

  return (
    <form
      onSubmit={savePrompt}
      className="rounded-2xl border border-border bg-card p-5 sm:p-7"
      aria-busy={saving}
    >
      <div className="flex flex-col gap-1 border-b border-border pb-5 sm:flex-row sm:items-baseline sm:justify-between">
        <p className="text-sm text-muted-foreground">Current prompt version</p>
        <p className="text-base font-semibold tabular-nums">Version {load.data.version}</p>
      </div>

      <div className="mt-5 max-w-4xl">
        <Label htmlFor="global-prompt">Global prompt</Label>
        <p id="global-prompt-help" className="mt-1 text-sm leading-6 text-muted-foreground">
          This instruction applies to new calls. Calls already in progress keep the prompt saved in their session.
        </p>
        <Textarea
          id="global-prompt"
          name="prompt_text"
          value={draft}
          onChange={(event) => {
            setDraft(event.currentTarget.value);
            setNotice(null);
          }}
          className="mt-3 min-h-72 leading-6"
          rows={14}
          maxLength={20000}
          required
          spellCheck={false}
          aria-describedby="global-prompt-help global-prompt-count"
        />
        <p id="global-prompt-count" className="mt-2 text-right text-xs tabular-nums text-muted-foreground">
          {draft.length.toLocaleString()} / 20,000 characters
        </p>
      </div>

      <div className="mt-6 flex flex-col-reverse gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="sm:max-w-xl">
          <AdminNotice notice={notice} />
        </div>
        <Button type="submit" disabled={saving || !hasChanges || !draft.trim()}>
          {saving ? "Saving…" : "Save prompt"}
        </Button>
      </div>
    </form>
  );
}

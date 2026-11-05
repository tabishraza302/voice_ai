"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api } from "../../lib/api";
import type { AdminPolicy } from "./admin-types";
import {
  AdminLoadError,
  AdminLoading,
  AdminNotice,
  errorDetails,
  inputClassName,
  type LoadState,
  type Notice,
  primaryButtonClassName,
} from "./admin-ui";

type PolicyDraft = {
  max_call_duration_seconds: string;
  max_concurrent_calls_per_agent: string;
  max_concurrent_calls_overall: string;
};

function toDraft(policy: AdminPolicy): PolicyDraft {
  return {
    max_call_duration_seconds: String(policy.max_call_duration_seconds),
    max_concurrent_calls_per_agent: String(policy.max_concurrent_calls_per_agent),
    max_concurrent_calls_overall: String(policy.max_concurrent_calls_overall),
  };
}

function parseDraft(draft: PolicyDraft): AdminPolicy | null {
  const values = {
    max_call_duration_seconds: Number(draft.max_call_duration_seconds),
    max_concurrent_calls_per_agent: Number(draft.max_concurrent_calls_per_agent),
    max_concurrent_calls_overall: Number(draft.max_concurrent_calls_overall),
  };

  if (Object.values(values).some((value) => !Number.isInteger(value))) return null;
  if (values.max_call_duration_seconds < 60 || values.max_call_duration_seconds > 3600) return null;
  if (values.max_concurrent_calls_per_agent < 1 || values.max_concurrent_calls_per_agent > 10) return null;
  if (values.max_concurrent_calls_overall < 1 || values.max_concurrent_calls_overall > 50) return null;
  if (values.max_concurrent_calls_per_agent > values.max_concurrent_calls_overall) return null;

  return values;
}

export function PolicyEditor() {
  const [load, setLoad] = useState<LoadState<AdminPolicy>>({ kind: "loading" });
  const [draft, setDraft] = useState<PolicyDraft>({
    max_call_duration_seconds: "",
    max_concurrent_calls_per_agent: "",
    max_concurrent_calls_overall: "",
  });
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoad({ kind: "loading" });

    void api<AdminPolicy>("/admin/policy", { signal: controller.signal })
      .then((policy) => {
        setDraft(toDraft(policy));
        setLoad({ kind: "ready", data: policy });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({ kind: "failed", message: errorDetails(error, "Couldn't load the call policy.").message });
      });

    return () => controller.abort();
  }, [retryKey]);

  async function savePolicy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (load.kind !== "ready") return;

    const policy = parseDraft(draft);
    if (!policy) {
      setNotice({
        kind: "error",
        message:
          "Check the limits. Duration must be 60–3,600 seconds, per-agent calls 1–10, and overall calls 1–50. Per-agent calls cannot exceed the overall limit.",
      });
      return;
    }

    setSaving(true);
    setNotice(null);
    try {
      const saved = await api<AdminPolicy>("/admin/policy", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(policy),
      });
      setDraft(toDraft(saved));
      setLoad({ kind: "ready", data: saved });
      setNotice({ kind: "success", message: "Call policy saved for new calls." });
    } catch (error: unknown) {
      setNotice({ kind: "error", message: errorDetails(error, "Couldn't save the call policy.").message });
    } finally {
      setSaving(false);
    }
  }

  if (load.kind === "loading") return <AdminLoading label="Loading call policy…" />;
  if (load.kind === "failed") {
    return <AdminLoadError message={load.message} onRetry={() => setRetryKey((key) => key + 1)} />;
  }

  const hasChanges =
    draft.max_call_duration_seconds !== String(load.data.max_call_duration_seconds) ||
    draft.max_concurrent_calls_per_agent !== String(load.data.max_concurrent_calls_per_agent) ||
    draft.max_concurrent_calls_overall !== String(load.data.max_concurrent_calls_overall);

  return (
    <form
      onSubmit={savePolicy}
      className="rounded-lg border border-border bg-card p-5 sm:p-7"
      aria-busy={saving}
    >
      <div className="max-w-3xl">
        <p className="text-sm leading-6 text-muted-foreground">
          These limits are captured when a call starts. Changing them will not change calls already in progress.
        </p>
      </div>

      <fieldset className="mt-6 max-w-3xl space-y-6" disabled={saving}>
        <legend className="text-base font-semibold">Call limits</legend>
        <div>
          <label htmlFor="max-call-duration" className="block text-sm font-medium">
            Maximum call duration
          </label>
          <p id="max-call-duration-help" className="mt-1 text-sm text-muted-foreground">
            From 60 to 3,600 seconds.
          </p>
          <div className="mt-2 flex max-w-sm items-center gap-3">
            <input
              id="max-call-duration"
              type="number"
              min={60}
              max={3600}
              step={1}
              required
              inputMode="numeric"
              value={draft.max_call_duration_seconds}
              onChange={(event) => {
                setDraft((current) => ({ ...current, max_call_duration_seconds: event.currentTarget.value }));
                setNotice(null);
              }}
              className={inputClassName}
              aria-describedby="max-call-duration-help"
            />
            <span className="shrink-0 text-sm text-muted-foreground">seconds</span>
          </div>
        </div>

        <div>
          <label htmlFor="max-agent-calls" className="block text-sm font-medium">
            Maximum concurrent calls per agent
          </label>
          <p id="max-agent-calls-help" className="mt-1 text-sm text-muted-foreground">
            From 1 to 10 calls, and no higher than the overall limit.
          </p>
          <input
            id="max-agent-calls"
            type="number"
            min={1}
            max={10}
            step={1}
            required
            inputMode="numeric"
            value={draft.max_concurrent_calls_per_agent}
            onChange={(event) => {
              setDraft((current) => ({ ...current, max_concurrent_calls_per_agent: event.currentTarget.value }));
              setNotice(null);
            }}
            className={`${inputClassName} mt-2 max-w-sm`}
            aria-describedby="max-agent-calls-help"
          />
        </div>

        <div>
          <label htmlFor="max-overall-calls" className="block text-sm font-medium">
            Maximum concurrent calls overall
          </label>
          <p id="max-overall-calls-help" className="mt-1 text-sm text-muted-foreground">
            From 1 to 50 calls. This limit must be at least the per-agent limit.
          </p>
          <input
            id="max-overall-calls"
            type="number"
            min={1}
            max={50}
            step={1}
            required
            inputMode="numeric"
            value={draft.max_concurrent_calls_overall}
            onChange={(event) => {
              setDraft((current) => ({ ...current, max_concurrent_calls_overall: event.currentTarget.value }));
              setNotice(null);
            }}
            className={`${inputClassName} mt-2 max-w-sm`}
            aria-describedby="max-overall-calls-help"
          />
        </div>
      </fieldset>

      <div className="mt-7 flex flex-col-reverse gap-4 border-t border-border pt-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="sm:max-w-xl">
          <AdminNotice notice={notice} />
        </div>
        <button type="submit" className={primaryButtonClassName} disabled={saving || !hasChanges}>
          {saving ? "Saving…" : "Save policy"}
        </button>
      </div>
    </form>
  );
}

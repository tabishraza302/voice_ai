import { Clock3, RotateCcw } from "lucide-react";
import type { DraftValidation, VersionView } from "@/lib/platform-agent-client";
import { formatDate } from "@/lib/agent-workspace-constants";

export interface AgentPublishTabProps {
  validation: DraftValidation | null;
  currentValidation: boolean;
  eligibleToPublish: boolean;
  validationLoading: boolean;
  onValidate: () => void;
  validateDisabled: boolean;
  warningCodes: string[];
  acknowledgedWarnings: string[];
  onAcknowledgedWarningsChange: (warnings: string[]) => void;
  allWarningsAcknowledged: boolean;
  changelog: string;
  onChangelogChange: (value: string) => void;
  publishDisabled: boolean;
  publishLoading: boolean;
  onPublish: () => void;
  dirty: boolean;
  versions: VersionView[];
  versionsLoading: boolean;
  versionsError: string | null;
  onRetryVersions: () => void;
  restoringVersionId: string | null;
  onRestoreVersion: (version: VersionView) => void;
  restoreDisabled: boolean;
}

export function AgentPublishTab({
  validation,
  currentValidation,
  validationLoading,
  onValidate,
  validateDisabled,
  warningCodes,
  acknowledgedWarnings,
  onAcknowledgedWarningsChange,
  changelog,
  onChangelogChange,
  publishDisabled,
  publishLoading,
  onPublish,
  dirty,
  versions,
  versionsLoading,
  versionsError,
  onRetryVersions,
  restoringVersionId,
  onRestoreVersion,
  restoreDisabled,
}: AgentPublishTabProps) {
  return (
    <div role="tabpanel" id="tabpanel-publish" aria-labelledby="tab-publish" className="space-y-6">
      <section className="surface space-y-4 p-5 sm:p-6">
        <div className="border-b border-ink-800 pb-4">
          <h2 className="text-lg font-semibold text-limestone-100">Validate and publish</h2>
          <p className="mt-1 text-sm text-limestone-400">
            Validation uses the current schema. Publishing creates an immutable version; it never edits the saved source.
          </p>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <button
            type="button"
            className="button-secondary"
            onClick={onValidate}
            disabled={validateDisabled}
          >
            {validationLoading ? "Validating…" : "Validate current draft"}
          </button>
          {currentValidation && validation ? (
            <div
              role="status"
              className={`rounded-lg border px-3 py-1.5 text-sm ${
                validation.eligible_for_publish
                  ? "border-moss-800 bg-moss-950/30 text-moss-200"
                  : "border-rust-800 bg-rust-950/30 text-rust-200"
              }`}
            >
              {validation.eligible_for_publish
                ? "This draft is eligible for publishing."
                : "This draft is not eligible for publishing. Review the diagnostics above."}
              <span className="ml-2 text-xs text-limestone-400">
                Authoring hash {validation.authoring_hash.slice(0, 12)}…
              </span>
            </div>
          ) : null}
        </div>

        {currentValidation && warningCodes.length > 0 ? (
          <fieldset className="space-y-2 rounded-lg border border-copper-900/70 p-4">
            <legend className="px-1 text-sm font-semibold text-copper-200">Acknowledge publish warnings</legend>
            {warningCodes.map((code) => (
              <label key={code} className="flex min-h-11 items-center gap-3 text-sm text-limestone-300">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-copper-500"
                  checked={acknowledgedWarnings.includes(code)}
                  onChange={(event) =>
                    onAcknowledgedWarningsChange(
                      event.currentTarget.checked
                        ? [...acknowledgedWarnings, code]
                        : acknowledgedWarnings.filter((item) => item !== code)
                    )
                  }
                />
                <span>
                  Confirm warning <code className="font-mono text-copper-200">{code}</code>
                </span>
              </label>
            ))}
          </fieldset>
        ) : null}

        <div className="space-y-1">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
            <div className="flex-1">
              <label className="field-label" htmlFor="publish-changelog">
                Version notes
              </label>
              <input
                id="publish-changelog"
                className="field-control"
                value={changelog}
                onChange={(event) => onChangelogChange(event.currentTarget.value)}
                placeholder="Summarize what this version changes"
              />
            </div>
            <button
              type="button"
              className="button-primary shrink-0"
              onClick={onPublish}
              disabled={publishDisabled}
            >
              {publishLoading ? "Publishing…" : "Publish version"}
            </button>
          </div>
          {dirty ? <p className="text-xs text-limestone-500">Wait for the draft to save before publishing.</p> : null}
        </div>
      </section>

      <section className="surface space-y-4 p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-ink-800 pb-4">
          <div>
            <h2 className="text-lg font-semibold text-limestone-100">Version history</h2>
            <p className="mt-1 text-sm text-limestone-400">
              Restore a published version into the mutable draft without changing its original version.
            </p>
          </div>
          <span className="inline-flex items-center gap-1 text-xs text-limestone-500">
            <Clock3 className="h-3.5 w-3.5" />
            {versionsLoading ? "Loading…" : `${versions.length} versions`}
          </span>
        </div>
        {versionsError ? (
          <div role="alert" className="flex flex-wrap items-center justify-between gap-3 text-sm text-rust-200">
            <span>Could not load version history: {versionsError}</span>
            <button
              type="button"
              className="button-secondary"
              onClick={onRetryVersions}
              disabled={versionsLoading}
            >
              {versionsLoading ? "Retrying…" : "Retry version history"}
            </button>
          </div>
        ) : versionsLoading ? (
          <p role="status" className="text-sm text-limestone-400">Loading version history…</p>
        ) : versions.length === 0 ? (
          <p className="text-sm text-limestone-400">No published versions yet.</p>
        ) : (
          <ol className="divide-y divide-ink-800">
            {versions.map((version) => (
              <li
                key={version.version_id}
                className="flex flex-col gap-3 py-4 sm:flex-row sm:items-start sm:justify-between"
              >
                <div className="min-w-0">
                  <h3 className="font-medium text-limestone-100">Version {version.version}</h3>
                  <p className="mt-1 text-sm text-limestone-400">{version.changelog || "No version notes."}</p>
                  <p className="mt-1 text-xs text-limestone-500">
                    <time dateTime={version.created_at}>{formatDate(version.created_at)}</time> · compiler{" "}
                    {version.compiler_version}
                  </p>
                  <p className="mt-1 break-all font-mono text-xs text-limestone-500">
                    Artifact {version.artifact_hash}
                  </p>
                </div>
                <button
                  type="button"
                  className="button-secondary shrink-0"
                  onClick={() => onRestoreVersion(version)}
                  disabled={restoreDisabled}
                >
                  <RotateCcw className="h-4 w-4" />
                  {restoringVersionId === version.version_id ? "Restoring…" : "Restore to draft"}
                </button>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}

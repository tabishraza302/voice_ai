"use client";

import { useCallback, useEffect, useId, useState, type FormEvent } from "react";
import { RefreshCw } from "lucide-react";
import { useAuth } from "@/lib/auth-state";
import { PlatformApiError, platformApi } from "@/lib/platform-api-client";
import type { AdminOllamaModelItem } from "@/types/platform";

function modelKey(model: AdminOllamaModelItem) {
  return `${model.provider_id}:${model.model_id}`;
}

function modelActionKey(model: AdminOllamaModelItem, action: string) {
  return `${modelKey(model)}:${action}`;
}

function modelErrorMessage(error: unknown, fallback: string) {
  if (!(error instanceof PlatformApiError)) return fallback;

  switch (error.code) {
    case "STEP_UP_CANCELLED":
      return "Admin verification was cancelled. Try again if you still want to continue.";
    case "STEP_UP_REQUIRED":
      return "Admin verification is required for this change. Complete the prompt and try again.";
    case "CSRF_INVALID":
      return "Your admin session is out of date. Refresh the page and try again.";
    case "CONFLICT":
      return "A model with this provider model ID already exists. Use a different ID or refresh the list.";
    case "NOT_FOUND":
      return "This model is no longer registered. Refresh the list and try again.";
    case "VALIDATION":
      return "The model ID must be nonblank and 64 characters or fewer; the display name must be nonblank and 255 characters or fewer.";
    default:
      if (error.status === 0) return error.message;
      if (error.status >= 500) return fallback;
      return error.message || fallback;
  }
}

function statusBadge(enabled: boolean) {
  return enabled
    ? "border-moss-800 bg-moss-950/40 text-moss-200"
    : "border-ink-700 bg-ink-900 text-limestone-300";
}

function qualificationBadge(qualified: boolean) {
  return qualified
    ? "border-moss-800 bg-moss-950/40 text-moss-200"
    : "border-rust-800 bg-rust-950/30 text-rust-200";
}

type ModelProviderPanelProps = {
  provider: { id: string; name: string };
  providerBusy: boolean;
  onBusyChange: (busy: boolean) => void;
};

export default function ModelProviderPanel({
  provider,
  providerBusy,
  onBusyChange,
}: ModelProviderPanelProps) {
  const { executeWithStepUp } = useAuth();
  const [models, setModels] = useState<AdminOllamaModelItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [newModelId, setNewModelId] = useState("");
  const [newName, setNewName] = useState("");
  const [attestsQualified, setAttestsQualified] = useState(false);
  const modelIdInputId = useId();
  const nameInputId = useId();
  const qualificationInputId = useId();

  const loadModels = useCallback(async () => {
    if (providerBusy) return false;

    setLoading(true);
    setError(null);
    onBusyChange(true);
    try {
      const result = await executeWithStepUp(() => platformApi.adminGetOllamaModels(provider.id));
      setModels(result.models);
      return true;
    } catch (caught) {
      setError(modelErrorMessage(caught, `Could not load ${provider.name} models. Refresh to try again.`));
      return false;
    } finally {
      setLoading(false);
      onBusyChange(false);
    }
  }, [executeWithStepUp, onBusyChange, providerBusy, provider.id, provider.name]);

  useEffect(() => {
    if (!providerBusy) void loadModels();
  }, [loadModels, providerBusy]);

  const mutate = async (
    actionKey: string,
    action: () => Promise<unknown>,
    successMessage: string,
    failureMessage: string
  ) => {
    if (providerBusy || loading || savingKey !== null) return false;

    setSavingKey(actionKey);
    setError(null);
    setMessage(null);
    onBusyChange(true);
    try {
      await executeWithStepUp(action);
      const refreshed = await loadModels();
      setMessage(
        refreshed
          ? successMessage
          : `${successMessage} The list could not be refreshed; use Refresh to confirm its current state.`
      );
      return true;
    } catch (caught) {
      setError(modelErrorMessage(caught, failureMessage));
      return false;
    } finally {
      onBusyChange(false);
      setSavingKey(null);
    }
  };

  const handleAdd = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const added = await mutate(
      "add",
      () =>
        platformApi.adminCreateOllamaModel(provider.id, {
          model_id: newModelId,
          name: newName,
          enabled: false,
          qualified: attestsQualified,
        }),
      `Added ${newModelId} to the ${provider.name} model list.`,
      `Could not add this ${provider.name} model. Check the model ID and display name, then try again.`
    );
    if (added) {
      setNewModelId("");
      setNewName("");
      setAttestsQualified(false);
    }
  };

  const handleRename = (model: AdminOllamaModelItem, event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submittedName = new FormData(form).get("name");
    if (typeof submittedName !== "string") return;

    void mutate(
      modelActionKey(model, "name"),
      () => platformApi.adminUpdateOllamaModel(provider.id, model.model_id, { name: submittedName }),
      `Updated the display name for ${model.model_id}.`,
      `Could not rename ${model.model_id}. Check the display name and try again.`
    );
  };

  const handleToggleEnabled = (model: AdminOllamaModelItem) => {
    const enabled = !model.enabled;
    void mutate(
      modelActionKey(model, "enabled"),
      () => platformApi.adminUpdateOllamaModel(provider.id, model.model_id, { enabled }),
      `${enabled ? "Enabled" : "Disabled"} ${model.name}.`,
      `Could not ${enabled ? "enable" : "disable"} ${model.name}. Refresh the list and try again.`
    );
  };

  const handleToggleQualified = (model: AdminOllamaModelItem) => {
    const qualified = !model.qualified;
    void mutate(
      modelActionKey(model, "qualified"),
      () => platformApi.adminUpdateOllamaModel(provider.id, model.model_id, { qualified }),
      qualified
        ? `Recorded your operator attestation for ${model.name}.`
        : `Revoked the operator attestation for ${model.name}.`,
      `Could not update the qualification status for ${model.name}. Refresh the list and try again.`
    );
  };

  const handleRemove = (model: AdminOllamaModelItem) => {
    if (
      !window.confirm(
        `Remove ${model.name} (${model.model_id}) from the ${provider.name} model list? This cannot be undone from this page.`
      )
    ) {
      return;
    }

    void mutate(
      modelActionKey(model, "remove"),
      () => platformApi.adminDeleteOllamaModel(provider.id, model.model_id),
      `Removed ${model.name} from the ${provider.name} model list.`,
      `Could not remove ${model.name}. Refresh the list and try again.`
    );
  };

  const disabled = providerBusy || loading || savingKey !== null;

  return (
    <section aria-labelledby="ollama-models-title" className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 id="ollama-models-title" className="text-lg font-semibold text-limestone-100">
            {provider.name} models
          </h2>
          <p id="ollama-qualification-note" className="mt-1 max-w-3xl text-sm leading-6 text-limestone-400">
            Manage the model entries for {provider.name}. Qualified status is an operator attestation of
            availability and eligibility; it does not install or test a model.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {models ? (
            <span className="text-xs text-limestone-500">
              {models.length} {models.length === 1 ? "model" : "models"}
            </span>
          ) : null}
          <button
            type="button"
            onClick={() => {
              setMessage(null);
              void loadModels();
            }}
            disabled={disabled}
            className="button-secondary text-xs"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      {error ? (
        <div role="alert" className="surface border-rust-800 bg-rust-950/30 p-4 text-sm text-rust-200">
          {error}
        </div>
      ) : null}
      {message ? (
        <div role="status" className="surface border-moss-800 bg-moss-950/30 p-4 text-sm text-moss-200">
          {message}
        </div>
      ) : null}

      <form onSubmit={handleAdd} className="surface space-y-4 p-4 sm:p-5">
        <div>
          <h3 className="text-base font-semibold text-limestone-100">Add a {provider.name} model</h3>
          <p className="mt-1 text-sm text-limestone-400">New models start disabled.</p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label htmlFor={modelIdInputId} className="field-label">
              Model ID
            </label>
            <input
              id={modelIdInputId}
              name="model_id"
              type="text"
              required
              maxLength={64}
              value={newModelId}
              onChange={(event) => setNewModelId(event.currentTarget.value)}
              className="field-control mt-1"
              placeholder="e.g. llama3.1:8b"
              disabled={disabled}
            />
          </div>
          <div>
            <label htmlFor={nameInputId} className="field-label">
              Display name
            </label>
            <input
              id={nameInputId}
              name="name"
              type="text"
              required
              maxLength={255}
              value={newName}
              onChange={(event) => setNewName(event.currentTarget.value)}
              className="field-control mt-1"
              placeholder="Name shown to administrators"
              disabled={disabled}
            />
          </div>
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <label htmlFor={qualificationInputId} className="flex items-start gap-2 text-sm text-limestone-300">
            <input
              id={qualificationInputId}
              type="checkbox"
              checked={attestsQualified}
              aria-describedby="ollama-qualification-note"
              onChange={(event) => setAttestsQualified(event.currentTarget.checked)}
              className="mt-1 accent-copper-500"
              disabled={disabled}
            />
            <span>I attest this model is available and eligible for use.</span>
          </label>
          <button type="submit" className="button-primary shrink-0" disabled={disabled}>
            {savingKey === "add" ? "Adding…" : "Add model"}
          </button>
        </div>
      </form>

      {models === null ? (
        providerBusy ? (
          <p role="status" className="surface p-4 text-sm text-limestone-400">
            Waiting for the provider request to finish before loading models…
          </p>
        ) : loading ? (
          <p role="status" className="surface p-4 text-sm text-limestone-400">
            Loading {provider.name} models…
          </p>
        ) : null
      ) : models.length === 0 ? (
        <p role="status" className="surface p-4 text-sm text-limestone-400">
          No {provider.name} models are registered yet.
        </p>
      ) : (
        <ul className="surface divide-y divide-ink-800">
          {models.map((model) => {
            const identity = modelKey(model);
            const nameInputKey = `${identity}:${model.name}`;
            return (
              <li key={identity} className="space-y-4 px-4 py-4 sm:px-5">
                <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                  <div className="min-w-0 space-y-2">
                    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                      <h3 className="break-all text-base font-semibold text-limestone-100">
                        {model.model_id}
                      </h3>
                      <span className="text-xs text-limestone-500">provider: {model.provider_id}</span>
                    </div>
                    <p className="break-words text-sm text-limestone-300">{model.name}</p>
                    <div className="flex flex-wrap gap-2">
                      <span className={`rounded-md border px-2 py-0.5 text-xs font-medium ${statusBadge(model.enabled)}`}>
                        {model.enabled ? "Enabled" : "Disabled"}
                      </span>
                      <span className={`rounded-md border px-2 py-0.5 text-xs font-medium ${qualificationBadge(model.qualified)}`}>
                        {model.qualified ? "Qualified · operator attested" : "Not qualified"}
                      </span>
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => handleToggleEnabled(model)}
                      disabled={disabled}
                      className="button-secondary text-xs"
                    >
                      {savingKey === modelActionKey(model, "enabled")
                        ? "Saving…"
                        : model.enabled
                          ? "Disable"
                          : "Enable"}
                    </button>
                    <button
                      type="button"
                      onClick={() => handleToggleQualified(model)}
                      aria-label={model.qualified ? `Revoke qualification for ${model.name}` : `Attest qualification for ${model.name}`}
                      disabled={disabled}
                      className="button-secondary text-xs"
                    >
                      {savingKey === modelActionKey(model, "qualified")
                        ? "Saving…"
                        : model.qualified
                          ? "Revoke qualification"
                          : "Attest qualification"}
                    </button>
                    <button
                      type="button"
                      onClick={() => handleRemove(model)}
                      aria-label={`Remove ${model.name} (${model.model_id})`}
                      disabled={disabled}
                      className="button-secondary text-xs text-rust-300"
                    >
                      {savingKey === modelActionKey(model, "remove") ? "Removing…" : "Remove"}
                    </button>
                  </div>
                </div>
                <form
                  onSubmit={(event) => handleRename(model, event)}
                  className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end"
                >
                  <div>
                    <label
                      htmlFor={`ollama-name-${encodeURIComponent(identity)}`}
                      className="field-label"
                    >
                      Display name
                    </label>
                    <input
                      key={nameInputKey}
                      id={`ollama-name-${encodeURIComponent(identity)}`}
                      name="name"
                      type="text"
                      required
                      maxLength={255}
                      defaultValue={model.name}
                      className="field-control mt-1"
                      disabled={disabled}
                    />
                  </div>
                  <button type="submit" className="button-secondary" disabled={disabled}>
                    {savingKey === modelActionKey(model, "name") ? "Saving…" : "Save name"}
                  </button>
                </form>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

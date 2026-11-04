"use client";

import { useCallback, useEffect, useId, useState, type FormEvent } from "react";
import { RefreshCw } from "lucide-react";
import { useAuth } from "@/lib/auth-state";
import { platformApi } from "@/lib/platform-api-client";
import type {
  AdminProviderItem,
  AdminProviderSecretField,
} from "@/types/platform";
import ModelProviderPanel from "./ModelProviderPanel";

function secretFieldKey(provider: AdminProviderItem, field: AdminProviderSecretField) {
  return `${provider.kind}:${provider.id}:${field.name}`;
}

function ProviderSecretField({
  provider,
  field,
  saving,
  disabled,
  onSave,
}: {
  provider: AdminProviderItem;
  field: AdminProviderSecretField;
  saving: boolean;
  disabled: boolean;
  onSave: (
    provider: AdminProviderItem,
    field: AdminProviderSecretField,
    form: HTMLFormElement
  ) => Promise<void>;
}) {
  const inputId = useId();
  const hintId = `${inputId}-hint`;
  const statusLabel = `${field.required ? "Required" : "Optional"} · ${
    field.configured ? "configured · value hidden" : "not configured"
  }`;
  const statusClass = field.configured
    ? "border-moss-800 bg-moss-950/40 text-moss-200"
    : field.required
      ? "border-rust-800 bg-rust-950/30 text-rust-200"
      : "border-ink-700 bg-ink-900 text-limestone-300";

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void onSave(provider, field, event.currentTarget);
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="grid gap-3 border-t border-ink-800 py-4 first:border-t-0 first:pt-0 last:pb-0 sm:grid-cols-[minmax(12rem,0.8fr)_minmax(18rem,1.2fr)] sm:items-center"
    >
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor={inputId} className="text-sm font-medium text-limestone-200">
            {field.name}
          </label>
          <span className={`rounded-md border px-2 py-0.5 text-xs font-medium ${statusClass}`}>
            {statusLabel}
          </span>
        </div>
        <p id={hintId} className="text-xs leading-5 text-limestone-400">
          Existing values are never shown. Enter a new value to set or replace this secret.
        </p>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          id={inputId}
          name="secret"
          type="password"
          autoComplete="off"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          required
          aria-describedby={hintId}
          placeholder={field.configured ? "Enter replacement secret" : "Enter secret"}
          className="field-control min-w-0 flex-1"
          disabled={disabled || saving}
        />
        <button
          type="submit"
          className="button-primary shrink-0"
          disabled={disabled || saving}
        >
          {saving ? "Saving…" : field.configured ? "Replace secret" : "Save secret"}
        </button>
      </div>
    </form>
  );
}

function ProviderGroup({
  kind,
  title,
  providers,
  savingKey,
  disabled,
  onSave,
}: {
  kind: "stt" | "tts";
  title: string;
  providers: AdminProviderItem[];
  savingKey: string | null;
  disabled: boolean;
  onSave: (
    provider: AdminProviderItem,
    field: AdminProviderSecretField,
    form: HTMLFormElement
  ) => Promise<void>;
}) {
  const headingId = `provider-group-${kind}`;

  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id={headingId} className="text-lg font-semibold text-limestone-100">
          {title}
        </h2>
        <span className="text-xs text-limestone-500">
          {providers.length} {providers.length === 1 ? "provider" : "providers"}
        </span>
      </div>
      {providers.length === 0 ? (
        <p className="surface p-4 text-sm text-limestone-400">
          No registered {kind.toUpperCase()} providers.
        </p>
      ) : (
        <div className="surface divide-y divide-ink-800">
          {providers.map((provider) => (
            <article
              key={`${provider.kind}:${provider.id}`}
              aria-labelledby={`provider-${kind}-${provider.id}`}
              className="px-4 py-4 sm:px-5"
            >
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h3
                  id={`provider-${kind}-${provider.id}`}
                  className="text-base font-semibold text-limestone-100"
                >
                  {provider.name}
                </h3>
                <span className="break-all text-xs text-limestone-500">{provider.id}</span>
              </div>
              {provider.secret_fields.length === 0 ? (
                <p role="status" className="mt-3 text-sm text-limestone-400">
                  No credentials are required for this provider.
                </p>
              ) : (
                <div className="mt-4">
                  {provider.secret_fields.map((field) => {
                    const key = secretFieldKey(provider, field);
                    return (
                      <ProviderSecretField
                        key={key}
                        provider={provider}
                        field={field}
                        saving={savingKey === key}
                        disabled={disabled}
                        onSave={onSave}
                      />
                    );
                  })}
                </div>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function AdminProvidersContent() {
  const { executeWithStepUp } = useAuth();
  const [providers, setProviders] = useState<AdminProviderItem[] | null>(null);
  const [modelProviders, setModelProviders] = useState<{ id: string; name: string }[]>([]);
  const [selectedModelProvider, setSelectedModelProvider] = useState("");
  const [loading, setLoading] = useState(true);
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [modelRequestInFlight, setModelRequestInFlight] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await executeWithStepUp(() => platformApi.adminGetProviders());
      setProviders(result.providers);
      setModelProviders(result.model_providers);
      setSelectedModelProvider((current) =>
        result.model_providers.some((provider) => provider.id === current)
          ? current
          : result.model_providers[0]?.id ?? ""
      );
    } catch {
      setError("Could not load registered providers. Refresh the page and try again.");
      setProviders(null);
    } finally {
      setLoading(false);
    }
  }, [executeWithStepUp]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const saveSecret = async (
    provider: AdminProviderItem,
    field: AdminProviderSecretField,
    form: HTMLFormElement
  ) => {
    if (loading || savingKey !== null || modelRequestInFlight) return;

    const key = secretFieldKey(provider, field);
    const submitted = new FormData(form).get("secret");
    form.reset();
    if (typeof submitted !== "string" || submitted.length === 0) return;

    setSavingKey(key);
    setError(null);
    setMessage(null);
    try {
      await executeWithStepUp(() =>
        platformApi.adminSetProviderSecrets(provider.id, {
          [field.name]: submitted,
        })
      );
      setMessage(`Credential updated for ${provider.name}. The value remains hidden.`);
      await refresh();
    } catch {
      setError("Could not save this provider credential. Confirm your admin step-up and try again.");
    } finally {
      setSavingKey(null);
    }
  };

  const sttProviders = providers?.filter((provider) => provider.kind === "stt") ?? [];
  const ttsProviders = providers?.filter((provider) => provider.kind === "tts") ?? [];

  return (
    <div className="space-y-7">
      <header className="flex flex-col gap-4 border-b border-ink-800 pb-5 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Provider management</h1>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-limestone-400">
            Manage model entries and set credentials for registered speech providers. Saved credential values are never returned to or displayed on this page.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loading || savingKey !== null || modelRequestInFlight}
          className="button-secondary self-start text-xs sm:self-auto"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          Refresh providers
        </button>
      </header>

      {modelProviders.length > 1 ? (
        <label className="field-label">
          Model provider
          <select className="field-control mt-1" value={selectedModelProvider} disabled={modelRequestInFlight} onChange={(event) => setSelectedModelProvider(event.currentTarget.value)}>
            {modelProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}
          </select>
        </label>
      ) : null}
      {modelProviders.filter((provider) => provider.id === selectedModelProvider).map((provider) => (
        <ModelProviderPanel key={provider.id} provider={provider} providerBusy={loading || savingKey !== null} onBusyChange={setModelRequestInFlight} />
      ))}

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

      {providers === null ? (
        loading ? (
          <p role="status" className="surface p-4 text-sm text-limestone-400">
            Loading registered speech providers…
          </p>
        ) : null
      ) : (
        <div className="space-y-7">
          <ProviderGroup
            kind="stt"
            title="Speech recognition (STT)"
            providers={sttProviders}
            savingKey={savingKey}
            disabled={loading || modelRequestInFlight}
            onSave={saveSecret}
          />
          <ProviderGroup
            kind="tts"
            title="Speech synthesis (TTS)"
            providers={ttsProviders}
            savingKey={savingKey}
            disabled={loading || modelRequestInFlight}
            onSave={saveSecret}
          />
        </div>
      )}
    </div>
  );
}

export default function AdminProvidersPage() {
  return <AdminProvidersContent />;
}

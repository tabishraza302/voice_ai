"use client";

import type { JsonObject } from "@/lib/platform-agent-client";
import { summarizeRuntimeSettings } from "@/lib/platform-agent-model";
import {
  formatDate,
  getString,
  SETTINGS,
  type RuntimeSettingKey,
} from "@/lib/agent-workspace-constants";

export interface AgentRuntimeTabProps {
  createdAt: string;
  config: JsonObject;
  onRuntimeSettingChange: (key: RuntimeSettingKey, value: string) => void;
  textOnly: boolean;
  onTextOnlyChange: (checked: boolean) => void;
  agentName: string;
  onAgentNameChange: (value: string) => void;
  nameDisabled: boolean;
}

export function AgentRuntimeTab({
  createdAt,
  config,
  onRuntimeSettingChange,
  textOnly,
  onTextOnlyChange,
  agentName,
  onAgentNameChange,
  nameDisabled,
}: AgentRuntimeTabProps) {
  const runtimeSettings = summarizeRuntimeSettings(config);

  return (
    <div role="tabpanel" id="tabpanel-runtime" aria-labelledby="tab-runtime" className="space-y-6">
      {/* Agent Name Configuration */}
      <section className="surface p-5 sm:p-6 space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-3 border-b border-ink-800 pb-4">
          <div>
            <h2 className="text-base font-semibold text-limestone-100">Agent Identifier</h2>
            <p className="mt-0.5 text-xs text-limestone-400">
              The display name of the agent within your platform workspace.
            </p>
          </div>
          <span className="text-xs text-limestone-500">
            Created <time dateTime={createdAt}>{formatDate(createdAt)}</time>
          </span>
        </div>
        <div>
          <label className="field-label" htmlFor="runtime-agent-name">
            Agent Name
          </label>
          <input
            id="runtime-agent-name"
            className="field-control max-w-lg"
            value={agentName}
            onChange={(e) => onAgentNameChange(e.currentTarget.value)}
            disabled={nameDisabled}
            required
          />
        </div>
      </section>

      {/* Explicit Provider & Model Parameters */}
      <section className="surface space-y-4 p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-ink-800 pb-4">
          <div>
            <h2 className="text-base font-semibold text-limestone-100">Runtime Settings</h2>
            <p className="mt-0.5 text-xs text-limestone-400">
              Configure explicit local provider IDs or leave blank for platform defaults.
            </p>
          </div>
          {runtimeSettings.llmModel || runtimeSettings.sttProvider || runtimeSettings.voices ? (
            <span className="text-xs text-copper-300 font-mono">Explicit overrides active</span>
          ) : (
            <span className="text-xs text-limestone-500 font-mono">Platform defaults active</span>
          )}
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          {SETTINGS.slice(0, 2).map((setting) => (
            <div key={setting.key}>
              <label className="field-label" htmlFor={`setting-${setting.key}`}>
                {setting.label}
              </label>
              <input
                id={`setting-${setting.key}`}
                className="field-control font-mono text-sm"
                value={getString(config, setting.key)}
                onChange={(event) => onRuntimeSettingChange(setting.key, event.currentTarget.value)}
                maxLength={setting.maxLength}
                autoComplete="off"
              />
              <p className="mt-1 text-xs text-limestone-500">Blank uses the platform default.</p>
            </div>
          ))}
        </div>

        <label className="flex min-h-11 items-center gap-3 rounded-lg border border-ink-800 p-3 text-sm text-limestone-200 cursor-pointer hover:border-ink-700">
          <input
            type="checkbox"
            className="h-4 w-4 accent-copper-500"
            checked={textOnly}
            onChange={(event) => onTextOnlyChange(event.currentTarget.checked)}
          />
          <span>Publish as text-only (disable synthesized voice output)</span>
        </label>

        <div className="grid gap-4 sm:grid-cols-2">
          {SETTINGS.slice(2).map((setting) => (
            <div key={setting.key}>
              <label className="field-label" htmlFor={`setting-${setting.key}`}>
                {setting.label}
              </label>
              <input
                id={`setting-${setting.key}`}
                className="field-control font-mono text-sm"
                value={getString(config, setting.key)}
                onChange={(event) => onRuntimeSettingChange(setting.key, event.currentTarget.value)}
                maxLength={setting.maxLength}
                autoComplete="off"
                disabled={textOnly}
              />
            </div>
          ))}
        </div>

        <p className="text-xs leading-5 text-limestone-500">
          Configure both voice IDs, or leave both blank for platform defaults. The text-only switch explicitly stores empty voice IDs.
        </p>
      </section>
    </div>
  );
}

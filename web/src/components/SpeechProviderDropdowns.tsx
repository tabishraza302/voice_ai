"use client";

import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown, Mic, Volume2 } from "lucide-react";

export interface SttDropdownProps {
  providers: { id: string; name: string; models: { id: string; name: string; selectable: boolean }[] }[];
  providerId: string;
  modelId: string;
  onSelect: (providerId: string, modelId: string) => void;
}

export interface TtsDropdownProps {
  providers: {
    id: string;
    name: string;
    models: {
      id: string;
      name: string;
      selectable: boolean;
      voices: { id: string; name: string; language?: string | null; selectable: boolean }[];
    }[];
  }[];
  providerId: string;
  modelId: string;
  voiceEn: string;
  voiceHi: string;
  textOnly: boolean;
  onSelect: (providerId: string, modelId: string, voiceEn: string, voiceHi: string, textOnly: boolean) => void;
}

const triggerClass = "inline-flex items-center gap-2 rounded-lg border border-ink-700 bg-ink-900/80 px-3.5 py-2 text-xs font-medium text-limestone-200 hover:border-ink-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-copper-500 aria-expanded:border-copper-500 aria-expanded:bg-copper-950/30 aria-expanded:text-copper-200";
const optionClass = "flex w-full items-center justify-between rounded-lg border border-ink-800/80 bg-ink-900/50 px-3 py-2 text-left text-xs text-limestone-200 hover:border-ink-700 hover:bg-ink-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-copper-500 aria-pressed:border-copper-600/70 aria-pressed:bg-copper-950/30 disabled:cursor-not-allowed disabled:opacity-50";

function useDropdown() {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return { open, setOpen, root, trigger, panelId };
}

export function SttDropdown({ providers, providerId, modelId, onSelect }: SttDropdownProps) {
  const dropdown = useDropdown();
  const provider = providers.find((item) => item.id === providerId);
  const model = provider?.models.find((item) => item.id === modelId);
  const summary = model?.name || modelId || provider?.name || providerId || "Platform default";
  return (
    <div ref={dropdown.root} className="relative">
      <button ref={dropdown.trigger} type="button" className={triggerClass} aria-expanded={dropdown.open} aria-controls={dropdown.panelId} aria-label={`Configure speech-to-text. Current selection: ${summary}`} onClick={() => dropdown.setOpen(!dropdown.open)}>
        <Mic className="h-3.5 w-3.5 text-copper-400" aria-hidden="true" />
        <span>STT: <strong>{summary}</strong></span><ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
      {dropdown.open ? (
        <div id={dropdown.panelId} role="region" aria-label="Speech-to-text provider and model settings" className="surface absolute left-0 top-full z-40 mt-2 max-h-[60vh] w-[min(28rem,90vw)] overflow-y-auto rounded-xl border-ink-700 bg-ink-950 p-3 shadow-2xl">
          {providers.length === 0 ? <p className="p-3 text-sm text-limestone-400">No speech-to-text models are available.</p> : providers.map((item) => (
            <section key={item.id} className="space-y-1.5 py-1" aria-label={item.name}>
              <h3 className="px-1 py-2 text-xs font-semibold uppercase tracking-wider text-limestone-400">{item.name}</h3>
              {item.models.map((option) => (
                <button key={option.id} type="button" className={optionClass} disabled={!option.selectable} aria-pressed={providerId === item.id && modelId === option.id} onClick={() => { onSelect(item.id, option.id); dropdown.setOpen(false); dropdown.trigger.current?.focus(); }}>
                  <span>{option.name}</span><span className="ml-3 text-xs text-limestone-400">{!option.selectable ? "Not qualified" : providerId === item.id && modelId === option.id ? "Selected" : ""}</span>
                </button>
              ))}
            </section>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function languageMatches(language: string | null | undefined, code: "en" | "hi") {
  const value = language?.toLowerCase() ?? "";
  return value === code || value.startsWith(`${code}-`) || value.includes(code === "en" ? "english" : "hindi");
}

export function TtsDropdown({ providers, providerId, modelId, voiceEn, voiceHi, textOnly, onSelect }: TtsDropdownProps) {
  const dropdown = useDropdown();
  const [draft, setDraft] = useState({ providerId, modelId, voiceEn, voiceHi, textOnly });
  const selectedModel = providers.find((provider) => provider.id === draft.providerId)?.models.find((model) => model.id === draft.modelId);
  const englishValid = Boolean(selectedModel?.selectable && selectedModel.voices.some((voice) => voice.id === draft.voiceEn && voice.selectable && languageMatches(voice.language, "en")));
  const hindiValid = Boolean(selectedModel?.selectable && selectedModel.voices.some((voice) => voice.id === draft.voiceHi && voice.selectable && languageMatches(voice.language, "hi")));
  const canApply = draft.textOnly || (englishValid && hindiValid);
  const summary = textOnly ? "Text-only" : [voiceEn, voiceHi].filter(Boolean).join(" · ") || "Platform default";
  const chooseVoice = (provider: TtsDropdownProps["providers"][number], model: TtsDropdownProps["providers"][number]["models"][number], voice: TtsDropdownProps["providers"][number]["models"][number]["voices"][number], language: "en" | "hi") => {
    setDraft((current) => {
      const sameModel = current.providerId === provider.id && current.modelId === model.id;
      return {
        providerId: provider.id,
        modelId: model.id,
        voiceEn: language === "en" ? voice.id : sameModel ? current.voiceEn : "",
        voiceHi: language === "hi" ? voice.id : sameModel ? current.voiceHi : "",
        textOnly: false,
      };
    });
  };
  return (
    <div ref={dropdown.root} className="relative">
      <button ref={dropdown.trigger} type="button" className={triggerClass} aria-expanded={dropdown.open} aria-controls={dropdown.panelId} aria-label={`Configure text-to-speech. Current selection: ${summary}`} onClick={() => { if (!dropdown.open) setDraft({ providerId, modelId, voiceEn, voiceHi, textOnly }); dropdown.setOpen(!dropdown.open); }}>
        <Volume2 className="h-3.5 w-3.5 text-copper-400" aria-hidden="true" />
        <span>Voice: <strong>{summary}</strong></span><ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
      {dropdown.open ? (
        <div id={dropdown.panelId} role="region" aria-label="Text-to-speech provider, model, and voice settings" className="surface absolute left-0 top-full z-40 mt-2 max-h-[60vh] w-[min(32rem,90vw)] overflow-y-auto rounded-xl border-ink-700 bg-ink-950 p-3 shadow-2xl">
          <label className="flex items-center gap-3 px-3 py-2 text-sm text-limestone-200"><input type="checkbox" checked={draft.textOnly} onChange={(event) => { const checked = event.currentTarget.checked; setDraft((current) => ({ ...current, textOnly: checked })); }} /> Text-only</label>
          {providers.length === 0 ? <p className="p-3 text-sm text-limestone-400">No text-to-speech voices are available.</p> : providers.map((provider) => (
            <section key={provider.id} className="space-y-1.5 py-1" aria-label={provider.name}>
              <h3 className="px-1 py-2 text-xs font-semibold uppercase tracking-wider text-limestone-400">{provider.name}</h3>
              {provider.models.map((model) => (
                <div key={model.id} className="pb-2">
                  <p className="px-3 py-1 text-xs text-limestone-400">{model.name}{!model.selectable ? " · Not qualified" : ""}</p>
                  {(["en", "hi"] as const).map((language) => {
                    const voices = model.voices.filter((voice) => languageMatches(voice.language, language));
                    return voices.length ? <div key={language} role="group" aria-label={`${model.name} ${language === "en" ? "English" : "Hindi"} voices`}>
                      <p className="px-3 pt-1 text-xs text-limestone-500">{language === "en" ? "English" : "Hindi"}</p>
                      {voices.map((voice) => {
                        const active = !draft.textOnly && draft.providerId === provider.id && draft.modelId === model.id && (language === "en" ? draft.voiceEn : draft.voiceHi) === voice.id;
                        return <button key={voice.id} type="button" className={optionClass} disabled={!model.selectable || !voice.selectable} aria-pressed={active} onClick={() => chooseVoice(provider, model, voice, language)}><span>{voice.name}</span><span className="ml-3 text-xs text-limestone-400">{!model.selectable || !voice.selectable ? "Not qualified" : active ? "Selected" : ""}</span></button>;
                      })}
                    </div> : null;
                  })}
                  {model.voices.some((voice) => !languageMatches(voice.language, "en") && !languageMatches(voice.language, "hi")) ? <div role="group" aria-label={`${model.name} other voices`}>
                    <p className="px-3 pt-1 text-xs text-limestone-500">Other or unspecified languages</p>
                    {model.voices.filter((voice) => !languageMatches(voice.language, "en") && !languageMatches(voice.language, "hi")).map((voice) => <button key={voice.id} type="button" className={optionClass} disabled><span>{voice.name}</span><span className="ml-3 text-xs text-limestone-400">English or Hindi not listed</span></button>)}
                  </div> : null}
                  {model.voices.length === 0 ? <p className="px-3 py-1 text-xs text-limestone-500">No voices available.</p> : null}
                </div>
              ))}
            </section>
          ))}
          {!draft.textOnly && !canApply ? <p role="status" className="px-3 py-2 text-xs text-rust-300">Choose qualified English and Hindi voices from the same model.</p> : null}
          <div className="border-t border-ink-800 p-2"><button type="button" className="button-primary w-full text-xs" disabled={!canApply} onClick={() => { onSelect(draft.providerId, draft.modelId, draft.voiceEn, draft.voiceHi, draft.textOnly); dropdown.setOpen(false); dropdown.trigger.current?.focus(); }}>Apply voice settings</button></div>
        </div>
      ) : null}
    </div>
  );
}

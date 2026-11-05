"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "./utils";

export interface TtsVoiceOption {
  provider: string;
  providerLabel?: string;
  modelId: string;
  voiceId: string;
  name?: string | null;
  languages: string[];
  sampleUrl?: string | null;
  label?: string;
}

export interface TtsProviderOption {
  provider: string;
  label: string;
  available: boolean;
  error?: string | null;
  sampleUrl?: string | null;
  voices: TtsVoiceOption[];
}

export interface TtsVoiceSelectProps {
  id?: string;
  disabled?: boolean;
  value: string | null;
  selectedProvider?: string | null;
  providers: TtsProviderOption[];
  onSelect: (voice: TtsVoiceOption | null) => void;
  className?: string;
}

export function formatProviderName(provider: string): string {
  const lower = provider.toLowerCase();
  if (lower === "elevenlabs") return "ElevenLabs";
  if (lower === "cartesia") return "Cartesia";
  if (lower === "openai") return "OpenAI";
  if (lower === "deepgram") return "Deepgram";
  return provider.charAt(0).toUpperCase() + provider.slice(1);
}

const PAGE_SIZE = 24;

export function TtsVoiceSelect({
  id,
  disabled = false,
  value,
  selectedProvider,
  providers,
  onSelect,
  className,
}: TtsVoiceSelectProps) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playingUrl, setPlayingUrl] = useState<string | null>(null);

  // Pagination limit for infinite scroll
  const [visibleCount, setVisibleCount] = useState<number>(PAGE_SIZE);

  // Find currently selected voice
  const allVoices = providers.flatMap((p) => p.voices);
  const currentVoice = allVoices.find((v) => v.voiceId === value);

  // Active/hovered provider in the dropdown
  const initialProvider =
    selectedProvider ??
    currentVoice?.provider ??
    providers.find((p) => p.available)?.provider ??
    providers[0]?.provider ??
    null;
  const [hoveredProvider, setHoveredProvider] = useState<string | null>(initialProvider);

  // Sync hovered provider and reset visible count when menu opens
  useEffect(() => {
    if (isOpen) {
      const active =
        selectedProvider ??
        currentVoice?.provider ??
        providers.find((p) => p.available)?.provider ??
        providers[0]?.provider ??
        null;
      setHoveredProvider(active);
      setVisibleCount(PAGE_SIZE);
      if (scrollContainerRef.current) {
        scrollContainerRef.current.scrollTop = 0;
      }
    }
  }, [isOpen, selectedProvider, currentVoice, providers]);

  // Reset pagination and scroll position when switching providers
  function selectHoveredProvider(provId: string) {
    if (hoveredProvider !== provId) {
      setHoveredProvider(provId);
      setVisibleCount(PAGE_SIZE);
      if (scrollContainerRef.current) {
        scrollContainerRef.current.scrollTop = 0;
      }
    }
  }

  // Handle outside click & escape key
  useEffect(() => {
    if (!isOpen) return;

    function handleMouseDown(e: MouseEvent) {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setIsOpen(false);
        stopAudio();
      }
    }

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setIsOpen(false);
        stopAudio();
      }
    }

    document.addEventListener("mousedown", handleMouseDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleMouseDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen]);

  // Audio player cleanup
  function stopAudio() {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
      audioRef.current = null;
    }
    setPlayingUrl(null);
  }

  useEffect(() => {
    return () => {
      stopAudio();
    };
  }, []);

  function toggleAudio(url: string, e: React.MouseEvent) {
    e.stopPropagation();
    e.preventDefault();

    if (playingUrl === url) {
      stopAudio();
      return;
    }

    stopAudio();

    const audio = new Audio(url);
    audioRef.current = audio;
    setPlayingUrl(url);

    audio.play().catch(() => {
      setPlayingUrl(null);
    });

    audio.onended = () => {
      setPlayingUrl(null);
    };

    audio.onerror = () => {
      setPlayingUrl(null);
    };
  }

  // Infinite scroll trigger
  const handleScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const target = e.currentTarget;
    if (target.scrollHeight - target.scrollTop - target.clientHeight < 80) {
      setVisibleCount((prev) => {
        if (!activeProviderObj) return prev;
        if (prev < activeProviderObj.voices.length) {
          return Math.min(prev + PAGE_SIZE, activeProviderObj.voices.length);
        }
        return prev;
      });
    }
  };

  const activeProviderObj =
    providers.find((p) => p.provider === hoveredProvider) ?? providers[0];
  const allActiveVoices = activeProviderObj?.voices ?? [];
  const visibleVoices = allActiveVoices.slice(0, visibleCount);

  const triggerLabel = currentVoice
    ? `${currentVoice.providerLabel || formatProviderName(currentVoice.provider)}: ${currentVoice.name || currentVoice.voiceId}`
    : "Select Voice";

  return (
    <div ref={containerRef} className={cn("relative w-full", className)}>
      {/* Trigger Button */}
      <button
        id={id}
        type="button"
        disabled={disabled || providers.length === 0}
        onClick={() => {
          if (!disabled && providers.length > 0) {
            setIsOpen((prev) => !prev);
            if (isOpen) stopAudio();
          }
        }}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        className={cn(
          "flex min-h-11 w-full items-center justify-between rounded-xl border bg-background px-3.5 py-2.5 text-left text-sm text-foreground outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60",
          isOpen && "ring-2 ring-ring border-transparent"
        )}
      >
        <span className={cn("truncate", !currentVoice && "text-muted-foreground")}>
          {triggerLabel}
        </span>
        <div className="ml-2 flex items-center gap-1.5 shrink-0">
          {currentVoice && !disabled && (
            <button
              type="button"
              aria-label="Clear selected voice"
              onClick={(e) => {
                e.stopPropagation();
                onSelect(null);
                stopAudio();
              }}
              className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
            >
              <svg
                aria-hidden="true"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className="size-3.5"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
          <svg
            aria-hidden="true"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            className={cn("size-4 text-muted-foreground transition-transform duration-150", isOpen && "rotate-180")}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="m6 9 6 6 6-6" />
          </svg>
        </div>
      </button>

      {/* Two-section Dropdown Popover */}
      {isOpen && (
        <div className="absolute left-0 top-full z-50 mt-1.5 flex w-full min-w-[340px] sm:min-w-[620px] max-w-3xl flex-col sm:flex-row rounded-xl border border-border bg-card shadow-2xl text-card-foreground divide-y sm:divide-y-0 sm:divide-x divide-border overflow-hidden animate-in fade-in-0 zoom-in-95 duration-100">
          {/* Section 1: Dynamic Providers list */}
          <div className="sm:w-52 shrink-0 bg-muted/20 p-2 flex flex-col gap-1">
            <div className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              TTS Providers
            </div>
            {providers.map((p) => {
              const isHovered = hoveredProvider === p.provider;
              return (
                <div
                  key={p.provider}
                  onMouseEnter={() => selectHoveredProvider(p.provider)}
                  onClick={() => selectHoveredProvider(p.provider)}
                  className={cn(
                    "group flex items-center justify-between rounded-lg px-2.5 py-2 text-xs cursor-pointer transition-colors",
                    isHovered
                      ? "bg-accent text-accent-foreground font-medium shadow-sm"
                      : "text-foreground hover:bg-muted/70",
                    !p.available && "opacity-75"
                  )}
                >
                  <div className="flex flex-col min-w-0 pr-1">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate">{p.label || formatProviderName(p.provider)}</span>
                      {p.available ? (
                        <span className="text-[10px] text-muted-foreground">
                          ({p.voices.length})
                        </span>
                      ) : (
                        <span className="rounded bg-amber-500/15 px-1 py-0.2 text-[9px] font-medium text-amber-600 dark:text-amber-400">
                          Unavailable
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-1 shrink-0 ml-1">
                    {/* Sample audio button for provider if available */}
                    {p.sampleUrl && p.available && (
                      <button
                        type="button"
                        aria-label={`Sample audio for ${p.label}`}
                        title="Play provider audio sample"
                        onClick={(e) => toggleAudio(p.sampleUrl!, e)}
                        className={cn(
                          "rounded p-1 transition-colors",
                          playingUrl === p.sampleUrl
                            ? "bg-primary text-primary-foreground"
                            : "text-muted-foreground hover:bg-background hover:text-foreground"
                        )}
                      >
                        {playingUrl === p.sampleUrl ? (
                          <svg className="size-3 fill-current" viewBox="0 0 24 24">
                            <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" />
                          </svg>
                        ) : (
                          <svg className="size-3 fill-current" viewBox="0 0 24 24">
                            <path d="M8 5v14l11-7z" />
                          </svg>
                        )}
                      </button>
                    )}

                    <svg
                      aria-hidden="true"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      className={cn(
                        "size-3.5 text-muted-foreground transition-transform sm:block hidden",
                        isHovered && "text-foreground translate-x-0.5"
                      )}
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" d="m9 5 7 7-7 7" />
                    </svg>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Section 2: Voices list for hovered provider (2-column grid + infinite scroll) */}
          <div
            ref={scrollContainerRef}
            onScroll={handleScroll}
            className="flex-1 p-3 max-h-80 overflow-y-auto flex flex-col gap-2"
          >
            <div className="flex items-center justify-between text-[11px] font-semibold uppercase tracking-wider text-muted-foreground pb-1 border-b border-border/50">
              <span>
                {activeProviderObj
                  ? `${activeProviderObj.label || formatProviderName(activeProviderObj.provider)} Voices`
                  : "Voices"}
              </span>
              <span className="text-[10px] lowercase text-muted-foreground font-normal">
                {allActiveVoices.length > 0
                  ? `Showing ${Math.min(visibleCount, allActiveVoices.length)} of ${allActiveVoices.length}`
                  : activeProviderObj?.available ? "0 voices" : "Unavailable"}
              </span>
            </div>

            {/* If provider is unavailable, show reason */}
            {activeProviderObj && !activeProviderObj.available ? (
              <div className="rounded-lg border border-amber-500/20 bg-amber-500/10 p-4 text-xs text-amber-700 dark:text-amber-300">
                <div className="font-semibold mb-1">
                  {formatProviderName(activeProviderObj.provider)} is currently unavailable
                </div>
                <p className="text-[11px] opacity-90">
                  {activeProviderObj.error || "Please check server credentials and permissions."}
                </p>
              </div>
            ) : allActiveVoices.length === 0 ? (
              <div className="px-3 py-8 text-center text-xs text-muted-foreground">
                No voices available for this provider.
              </div>
            ) : (
              <>
                {/* 2-items-per-row grid layout */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {visibleVoices.map((voice) => {
                    const isSelected = voice.voiceId === value;
                    return (
                      <div
                        key={`${voice.provider}-${voice.voiceId}`}
                        onClick={() => {
                          onSelect(voice);
                          setIsOpen(false);
                          stopAudio();
                        }}
                        className={cn(
                          "flex flex-col justify-between rounded-lg border p-2 text-xs cursor-pointer transition-all hover:shadow-sm group text-left",
                          isSelected
                            ? "border-primary bg-primary/10 text-primary font-medium"
                            : "border-border/60 bg-background/50 hover:bg-accent/70 hover:border-accent text-foreground"
                        )}
                      >
                        <div className="flex items-start justify-between gap-1 mb-1">
                          <span className="truncate font-medium text-[12px] text-foreground leading-tight" title={voice.name || voice.voiceId}>
                            {voice.name || voice.voiceId}
                          </span>
                          {isSelected && (
                            <svg
                              aria-hidden="true"
                              viewBox="0 0 24 24"
                              fill="none"
                              stroke="currentColor"
                              strokeWidth="2.5"
                              className="size-3.5 text-primary shrink-0"
                            >
                              <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" />
                            </svg>
                          )}
                        </div>

                        {voice.name && (
                          <span className="truncate font-mono text-[10px] text-muted-foreground mb-1.5" title={voice.voiceId}>
                            {voice.voiceId}
                          </span>
                        )}

                        <div className="flex items-center justify-between gap-1 mt-auto pt-1">
                          <div className="flex items-center gap-1 flex-wrap">
                            {voice.languages.map((lang) => (
                              <span
                                key={lang}
                                className="inline-flex items-center rounded bg-muted px-1.5 py-0.2 text-[9px] text-muted-foreground uppercase"
                              >
                                {lang}
                              </span>
                            ))}
                          </div>

                          {/* Sample audio button */}
                          {voice.sampleUrl && (
                            <button
                              type="button"
                              aria-label={`Sample audio for ${voice.name || voice.voiceId}`}
                              title="Play sample audio"
                              onClick={(e) => toggleAudio(voice.sampleUrl!, e)}
                              className={cn(
                                "rounded p-1 transition-colors shrink-0",
                                playingUrl === voice.sampleUrl
                                  ? "bg-primary text-primary-foreground"
                                  : "text-muted-foreground hover:bg-background hover:text-foreground"
                              )}
                            >
                              {playingUrl === voice.sampleUrl ? (
                                <svg className="size-3 fill-current" viewBox="0 0 24 24">
                                  <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z" />
                                </svg>
                              ) : (
                                <svg className="size-3 fill-current" viewBox="0 0 24 24">
                                  <path d="M8 5v14l11-7z" />
                                </svg>
                              )}
                            </button>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>

                {/* Infinite scroll indicator */}
                {visibleCount < allActiveVoices.length && (
                  <div className="py-2 text-center text-[10px] text-muted-foreground animate-pulse">
                    Scroll down to load more voices ({allActiveVoices.length - visibleCount} remaining)...
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

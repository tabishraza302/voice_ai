"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { cn } from "./utils";
import { MarkdownPreview } from "./markdown-preview";

const EDITOR_MIN_HEIGHT = 480;

export interface MarkdownEditorProps {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
  "aria-describedby"?: string;
}

function ModeButton({
  title,
  active,
  onClick,
  children,
}: {
  title: string;
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
        active ? "bg-primary text-primary-foreground shadow-xs" : "text-muted-foreground hover:text-foreground"
      )}
    >
      {children}
    </button>
  );
}

export function MarkdownEditor({
  id = "markdown-editor",
  value,
  onChange,
  placeholder,
  className,
  "aria-describedby": ariaDescribedBy,
}: MarkdownEditorProps) {
  const [mode, setMode] = useState<"write" | "preview" | "split">("split");
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Auto-resize textarea in write mode
  useEffect(() => {
    const el = textareaRef.current;
    if (!el || mode === "preview") return;
    el.style.height = "auto";
    el.style.height = `${Math.max(EDITOR_MIN_HEIGHT, el.scrollHeight)}px`;
  }, [value, mode]);

  return (
    <div className={cn("rounded-2xl border border-border bg-card shadow-xs overflow-hidden", className)}>
      {/* Editor Header Bar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/80 bg-muted/40 px-3 py-2 sm:px-4">
        <div className="flex items-center rounded-lg border border-border/80 bg-background/80 p-0.5">
          <ModeButton title="Write" active={mode === "write"} onClick={() => setMode("write")}>
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
            </svg>
            Write
          </ModeButton>
          <ModeButton title="Split" active={mode === "split"} onClick={() => setMode("split")}>
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 4v16m6-16v16M4 4h16a1 1 0 011 1v14a1 1 0 01-1 1H4a1 1 0 01-1-1V5a1 1 0 011-1z" />
            </svg>
            Split
          </ModeButton>
          <ModeButton title="Preview" active={mode === "preview"} onClick={() => setMode("preview")}>
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
            </svg>
            Preview
          </ModeButton>
        </div>
      </div>

      {/* Editor Body */}
      <div className={cn("grid", mode === "split" ? "grid-cols-1 lg:grid-cols-2 divide-y lg:divide-y-0 lg:divide-x divide-border" : "grid-cols-1")}>
        {/* Write Pane */}
        {(mode === "write" || mode === "split") && (
          <div className="relative flex flex-col bg-background">
            {mode === "split" && (
              <div className="border-b border-border/60 bg-muted/10 px-4 py-1.5 text-[11px] font-mono uppercase tracking-wider text-muted-foreground">
                Markdown Source
              </div>
            )}
            <textarea
              ref={textareaRef}
              id={id}
              value={value}
              onChange={(e) => onChange(e.target.value)}
              placeholder={placeholder}
              aria-describedby={ariaDescribedBy}
              style={{ minHeight: EDITOR_MIN_HEIGHT }}
              className="w-full flex-1 resize-y bg-background p-4 font-mono text-sm leading-relaxed text-foreground placeholder:text-muted-foreground/60 focus:outline-none"
            />
          </div>
        )}

        {/* Preview Pane */}
        {(mode === "preview" || mode === "split") && (
          <div className="relative flex flex-col bg-card/40">
            {mode === "split" && (
              <div className="border-b border-border/60 bg-muted/10 px-4 py-1.5 text-[11px] font-mono uppercase tracking-wider text-muted-foreground flex items-center justify-between">
                <span>Rendered Preview</span>
                <span className="text-[10px] text-muted-foreground/70 lowercase">Live</span>
              </div>
            )}
            <div className="flex-1 p-4 sm:p-5 overflow-y-auto max-h-[800px]" style={{ minHeight: EDITOR_MIN_HEIGHT }}>
              <MarkdownPreview content={value} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Button } from "../ui/button";

export interface EnhancePromptModalProps {
  open: boolean;
  originalPrompt: string;
  enhancedPrompt: string;
  onApply: (editedEnhanced: string) => void;
  onClose: () => void;
}

export function EnhancePromptModal({
  open,
  originalPrompt,
  enhancedPrompt,
  onApply,
  onClose,
}: EnhancePromptModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const id = useId();
  const titleId = `${id}-title`;
  const descriptionId = `${id}-description`;

  const [editedText, setEditedText] = useState(enhancedPrompt);

  useEffect(() => {
    if (open) {
      setEditedText(enhancedPrompt);
    }
  }, [open, enhancedPrompt]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      onClose={onClose}
      className="w-[min(94vw,56rem)] rounded-2xl border bg-card p-0 text-card-foreground shadow-2xl backdrop:bg-foreground/35"
    >
      <div className="flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="border-b px-6 py-4 flex items-center justify-between">
          <div>
            <h2 id={titleId} className="text-xl font-semibold tracking-tight">
              Review Enhanced Prompt
            </h2>
            <p id={descriptionId} className="text-xs text-muted-foreground mt-0.5">
              Compare your original prompt with the structured AI version. You can edit the text before applying.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close modal"
            className="rounded-lg p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            <svg
              className="w-5 h-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Side-by-side columns */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 p-6 overflow-y-auto min-h-0">
          {/* Left: Original */}
          <div className="flex flex-col space-y-2">
            <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Original Prompt
            </span>
            <div className="flex-1 min-h-[16rem] md:min-h-[22rem] max-h-[48vh] rounded-xl border bg-muted/30 p-3.5 text-sm leading-relaxed overflow-y-auto whitespace-pre-wrap text-foreground/80 font-mono">
              {originalPrompt || "(empty)"}
            </div>
          </div>

          {/* Right: Enhanced & Editable */}
          <div className="flex flex-col space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-primary">
                Enhanced Prompt (Editable)
              </span>
              <span className="text-[11px] text-muted-foreground">
                You can tweak this directly
              </span>
            </div>
            <textarea
              value={editedText}
              onChange={(e) => setEditedText(e.target.value)}
              aria-label="Editable enhanced prompt"
              className="flex-1 min-h-[16rem] md:min-h-[22rem] max-h-[48vh] w-full rounded-xl border border-input bg-background p-3.5 text-sm leading-relaxed text-foreground shadow-sm focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary font-mono resize-none"
            />
          </div>
        </div>

        {/* Footer actions */}
        <div className="border-t px-6 py-4 flex flex-col-reverse sm:flex-row justify-end gap-2 bg-muted/10">
          <Button variant="outline" type="button" onClick={onClose}>
            Discard
          </Button>
          <Button
            type="button"
            disabled={!editedText.trim()}
            onClick={() => {
              onApply(editedText.trim());
              onClose();
            }}
          >
            Apply changes
          </Button>
        </div>
      </div>
    </dialog>
  );
}

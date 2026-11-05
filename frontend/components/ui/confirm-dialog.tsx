"use client";

import { useEffect, useId, useRef } from "react";
import { Button } from "./button";

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  pending = false,
  onConfirm,
  onOpenChange,
}: {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  pending?: boolean;
  onConfirm: () => void;
  onOpenChange: (open: boolean) => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const id = useId();
  const titleId = `${id}-title`;
  const descriptionId = `${id}-description`;

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
      onClose={() => onOpenChange(false)}
      className="w-[min(92vw,32rem)] rounded-2xl border bg-card p-0 text-card-foreground shadow-2xl backdrop:bg-foreground/35"
    >
      <div className="space-y-4 p-6">
        <div className="space-y-2">
          <h2 id={titleId} className="text-xl font-semibold tracking-tight">{title}</h2>
          <p id={descriptionId} className="text-sm leading-6 text-muted-foreground">{description}</p>
        </div>
        <div className="flex flex-col-reverse justify-end gap-2 sm:flex-row">
          <Button variant="outline" disabled={pending} autoFocus onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="destructive" disabled={pending} onClick={onConfirm}>
            {pending ? "Deleting…" : confirmLabel}
          </Button>
        </div>
      </div>
    </dialog>
  );
}

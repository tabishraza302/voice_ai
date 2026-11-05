"use client";

import { useRef, type KeyboardEvent, type ReactNode } from "react";
import { Button } from "./button";
import { cn } from "./utils";

export interface TabItem {
  value: string;
  label: string;
}

export function TabsBar({
  id,
  label,
  items,
  value,
  onValueChange,
  className,
}: {
  id: string;
  label: string;
  items: TabItem[];
  value: string;
  onValueChange: (value: string) => void;
  className?: string;
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (items.length === 0) return;
    const current = Math.max(0, items.findIndex((item) => item.value === value));
    let next = current;
    if (event.key === "ArrowRight") next = (current + 1) % items.length;
    else if (event.key === "ArrowLeft") next = (current - 1 + items.length) % items.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = items.length - 1;
    else return;
    event.preventDefault();
    const selected = items[next];
    if (selected) {
      onValueChange(selected.value);
      refs.current[next]?.focus();
    }
  }

  return (
    <div
      className={cn("flex gap-1 overflow-x-auto border-b", className)}
      role="tablist"
      aria-label={label}
      onKeyDown={onKeyDown}
    >
      {items.map((item, index) => (
        <Button
          key={item.value}
          ref={(node) => { refs.current[index] = node; }}
          id={`tab-${id}-${item.value}`}
          role="tab"
          aria-selected={value === item.value}
          aria-controls={`panel-${id}-${item.value}`}
          tabIndex={value === item.value ? 0 : -1}
          variant="ghost"
          size="sm"
          onClick={() => onValueChange(item.value)}
          className={cn(
            "min-h-12 shrink-0 rounded-none border-b-2 border-transparent px-3 text-muted-foreground hover:bg-transparent hover:text-foreground",
            value === item.value && "border-primary text-foreground",
          )}
        >
          {item.label}
        </Button>
      ))}
    </div>
  );
}

export function TabPanel({
  id,
  value,
  labelledBy,
  children,
  className,
}: {
  id: string;
  value: string;
  labelledBy: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      id={`panel-${id}-${value}`}
      role="tabpanel"
      aria-labelledby={labelledBy}
      tabIndex={0}
      className={cn("focus-visible:outline-none", className)}
    >
      {children}
    </div>
  );
}

import type { ReactNode } from "react";
import { cn } from "./utils";

function renderInlineMarkdown(text: string): ReactNode[] {
  const tokenRegex = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|_[^_]+_)/g;
  const parts = text.split(tokenRegex);
  return parts.map((part, index) => {
    if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
      return (
        <code
          key={index}
          className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs font-medium text-foreground"
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith("**") && part.endsWith("**") && part.length >= 4) {
      return (
        <strong key={index} className="font-semibold text-foreground">
          {part.slice(2, -2)}
        </strong>
      );
    }
    if (
      (part.startsWith("*") && part.endsWith("*") && part.length >= 2) ||
      (part.startsWith("_") && part.endsWith("_") && part.length >= 2)
    ) {
      return (
        <em key={index} className="italic text-foreground">
          {part.slice(1, -1)}
        </em>
      );
    }
    return part;
  });
}

export function MarkdownPreview({ content, className }: { content: string; className?: string }) {
  if (!content.trim()) {
    return (
      <div className={cn("flex flex-col items-center justify-center py-20 text-muted-foreground text-sm", className)}>
        <p className="font-medium">No instructions written yet</p>
        <p className="text-xs text-muted-foreground/80 mt-1">
          Switch to Write or Split mode to draft your agent prompt.
        </p>
      </div>
    );
  }

  const lines = content.split("\n");
  const blocks: ReactNode[] = [];
  let inCodeBlock = false;
  let codeBlockLines: string[] = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (line.trim().startsWith("```")) {
      if (inCodeBlock) {
        blocks.push(
          <pre
            key={`code-${i}`}
            className="my-3 overflow-x-auto rounded-xl bg-muted/80 p-3.5 font-mono text-xs text-foreground border border-border"
          >
            <code>{codeBlockLines.join("\n")}</code>
          </pre>
        );
        inCodeBlock = false;
        codeBlockLines = [];
      } else {
        inCodeBlock = true;
      }
      continue;
    }

    if (inCodeBlock) {
      codeBlockLines.push(line);
      continue;
    }

    if (line.startsWith("### ")) {
      blocks.push(
        <h3
          key={`h3-${i}`}
          className="mt-6 mb-2 text-base font-semibold tracking-tight text-foreground first:mt-0 pb-1.5 border-b border-border/50"
        >
          {renderInlineMarkdown(line.slice(4))}
        </h3>
      );
    } else if (line.startsWith("## ")) {
      blocks.push(
        <h2
          key={`h2-${i}`}
          className="mt-7 mb-2.5 text-lg font-semibold tracking-tight text-foreground first:mt-0 pb-1.5 border-b border-border/70"
        >
          {renderInlineMarkdown(line.slice(3))}
        </h2>
      );
    } else if (line.startsWith("# ")) {
      blocks.push(
        <h1
          key={`h1-${i}`}
          className="mt-8 mb-3 text-xl font-bold tracking-tight text-foreground first:mt-0 pb-2 border-b border-border"
        >
          {renderInlineMarkdown(line.slice(2))}
        </h1>
      );
    } else if (line.startsWith("> ")) {
      blocks.push(
        <blockquote
          key={`quote-${i}`}
          className="my-2 border-l-2 border-primary/70 pl-3.5 italic text-muted-foreground text-sm"
        >
          {renderInlineMarkdown(line.slice(2))}
        </blockquote>
      );
    } else if (line.trim().startsWith("- ") || line.trim().startsWith("* ")) {
      const indent = line.search(/\S/);
      const isNested = indent >= 2;
      const bulletContent = line.trim().slice(2);
      blocks.push(
        <div
          key={`bullet-${i}`}
          className={cn(
            "flex items-start gap-2 py-0.5 text-sm text-foreground leading-relaxed",
            isNested ? "ml-5 text-muted-foreground" : "ml-1"
          )}
        >
          <span className={cn("mt-2 h-1.5 w-1.5 shrink-0 rounded-full", isNested ? "bg-muted-foreground/60" : "bg-primary")} />
          <div className="flex-1">{renderInlineMarkdown(bulletContent)}</div>
        </div>
      );
    } else if (/^\s*\d+\.\s/.test(line)) {
      const match = line.match(/^(\s*)(\d+)\.\s(.*)$/);
      if (match) {
        const indent = match[1].length;
        const num = match[2];
        const itemContent = match[3];
        const isNested = indent >= 2;
        blocks.push(
          <div
            key={`num-${i}`}
            className={cn(
              "flex items-start gap-2 py-0.5 text-sm text-foreground leading-relaxed",
              isNested ? "ml-5 text-muted-foreground" : "ml-1"
            )}
          >
            <span className="shrink-0 font-mono text-xs text-muted-foreground font-semibold min-w-[1.25rem] pt-0.5">
              {num}.
            </span>
            <div className="flex-1">{renderInlineMarkdown(itemContent)}</div>
          </div>
        );
      }
    } else if (!line.trim()) {
      blocks.push(<div key={`blank-${i}`} className="h-2" />);
    } else {
      blocks.push(
        <p key={`p-${i}`} className="text-sm leading-relaxed text-foreground my-1">
          {renderInlineMarkdown(line)}
        </p>
      );
    }
  }

  return <div className={cn("space-y-0.5", className)}>{blocks}</div>;
}

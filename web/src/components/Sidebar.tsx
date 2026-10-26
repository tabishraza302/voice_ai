"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Bot,
  ScrollText,
  Mic2,
  Activity,
  Layers,
  ExternalLink,
} from "lucide-react";

const NAV_ITEMS = [
  { href: "/", label: "Overview", icon: Layers },
  { href: "/agents", label: "Agents", icon: Bot },
  { href: "/base-prompt", label: "Shared instructions", icon: ScrollText },
  { href: "/playground", label: "Call test", icon: Mic2 },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="w-full md:w-60 border-b md:border-b-0 md:border-r border-ink-800 bg-ink-950 flex flex-col shrink-0 md:h-screen md:sticky md:top-0 z-40">
      <div className="h-14 md:h-16 px-4 md:px-6 border-b border-ink-800/80 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-copper-950 border border-copper-800 flex items-center justify-center text-copper-300">
            <Mic2 className="w-5 h-5" />
          </div>
          <div>
            <span className="font-semibold text-sm text-white block">Voice AI</span>
            <span className="text-[11px] text-limestone-400 block">Local workspace</span>
          </div>
        </div>
      </div>

      <nav aria-label="Main navigation" className="px-3 py-2 md:p-3 flex md:block gap-1 md:gap-1.5 overflow-x-auto md:overflow-visible md:flex-1">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const isActive =
            item.href === "/"
              ? pathname === "/"
              : pathname.startsWith(item.href);

          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={isActive ? "page" : undefined}
              className={`flex items-center gap-2 md:gap-3 px-3 py-2 md:px-3.5 md:py-2.5 rounded-lg text-xs md:text-sm font-medium transition-colors whitespace-nowrap shrink-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-copper-400 ${
                isActive
                  ? "bg-ink-800 text-copper-200 border border-ink-700"
                  : "text-limestone-400 hover:text-limestone-100 hover:bg-ink-900"
              }`}
            >
              <Icon className={`w-4 h-4 ${isActive ? "text-copper-300" : "text-limestone-400"}`} />
              {item.label}
            </Link>
          );
        })}

        <div className="hidden md:block pt-6 px-3 py-2 text-xs font-medium text-limestone-400">
          Observability
        </div>
        <a
          aria-label="FastAPI documentation (opens in a new tab)"
          title="FastAPI Docs"
          href="http://localhost:7860/docs"
          target="_blank"
          rel="noreferrer"
          className="hidden md:flex items-center justify-between px-3.5 py-2 rounded-lg text-sm font-medium text-limestone-400 hover:text-limestone-100 hover:bg-ink-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-copper-400"
        >
          <span className="flex items-center gap-2.5">
            <Activity className="w-3.5 h-3.5 text-limestone-400" />
            FastAPI Docs
          </span>
          <ExternalLink className="w-3 h-3 opacity-60" />
        </a>
        <a
          aria-label="Prometheus metrics (opens in a new tab)"
          title="Prometheus Metrics"
          href="http://localhost:7860/metrics"
          target="_blank"
          rel="noreferrer"
          className="hidden md:flex items-center justify-between px-3.5 py-2 rounded-lg text-sm font-medium text-limestone-400 hover:text-limestone-100 hover:bg-ink-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-copper-400"
        >
          <span className="flex items-center gap-2.5">
            <Activity className="w-3.5 h-3.5 text-limestone-400" />
            Prometheus Metrics
          </span>
          <ExternalLink className="w-3 h-3 opacity-60" />
        </a>
      </nav>

      <div className="hidden md:block border-t border-ink-800 px-6 py-4 text-xs text-limestone-500">English · हिन्दी</div>
    </aside>
  );
}

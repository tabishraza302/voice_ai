"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  FocusEvent as ReactFocusEvent,
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  PointerEvent as ReactPointerEvent,
} from "react";

import { ApiError, apiErrorText } from "@/lib/api";
import type { AgentStep } from "@/lib/types";

import {
  agentStepsToCallFlow,
  callFlowToStepInput,
  getSampleHearingScript,
  resolveCallFlowRoute,
} from "./call-flow-adapter";
import type { CallFlowAnswer, CallFlowStep } from "./call-flow-adapter";
import { assignLanes, computeReachability, gutterFor, pathD } from "./call-flow-geometry";
import type { GutterConfig, LaneAssignment, MeasuredNode, RouteSpan } from "./call-flow-geometry";
import { saveCallFlow } from "./call-flow-save";
import "./call-flow-editor.css";

const SVG_NS = "http://www.w3.org/2000/svg";
const GAP = 14;

const SAVE_IDLE_MS = 1200;
const SAVE_MAX_MS = 10000;
const SAVE_COALESCE_MS = 250;
const SAVE_RETRY_MS = [2000, 10000, 30000];
const SAVED_FADE_MS = 2500;

const ICONS = {
  grip: (
    <svg width="12" height="18" viewBox="0 0 12 18" aria-hidden="true">
      <g fill="currentColor">
        <circle cx="3.5" cy="3" r="1.5" />
        <circle cx="8.5" cy="3" r="1.5" />
        <circle cx="3.5" cy="9" r="1.5" />
        <circle cx="8.5" cy="9" r="1.5" />
        <circle cx="3.5" cy="15" r="1.5" />
        <circle cx="8.5" cy="15" r="1.5" />
      </g>
    </svg>
  ),
  trash: (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2.5 4.5h11M6 4.5V3h4v1.5M4 4.5l.6 8.5h6.8l.6-8.5M6.7 7v4M9.3 7v4" />
    </svg>
  ),
  x: (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" aria-hidden="true">
      <path d="M3 3l8 8M11 3l-8 8" />
    </svg>
  ),
  plus: (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" aria-hidden="true">
      <path d="M7 2.5v9M2.5 7h9" />
    </svg>
  ),
  arrow: (
    <svg width="16" height="12" viewBox="0 0 16 12" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 6h11M9 2l4 4-4 4" />
    </svg>
  ),
};

interface WireElements {
  g: SVGGElement;
  line: SVGPathElement;
  arrow: SVGPathElement;
  dot: SVGCircleElement;
  hit: SVGPathElement;
}

interface Measurement {
  nodes: Record<string, MeasuredNode>;
  routes: RouteSpan[];
  width: number;
  height: number;
}

interface DragState {
  el: HTMLElement;
  grip: HTMLButtonElement;
  els: HTMLElement[];
  idx: number;
  target: number;
  tops: number[];
  hs: number[];
  startY: number;
  startScroll: number;
  y: number;
  settling: boolean;
  raf: number;
  lanes: LaneAssignment;
  controller: AbortController;
}

interface HighlightState {
  wire: string | null;
  row: string | null;
  node: string | null;
  focusRow: string | null;
  focusNode: string | null;
}

interface SaveState {
  status: "idle" | "saving" | "saved" | "error";
  message?: string;
}

function uid(prefix: string): string {
  return prefix + Math.random().toString(36).slice(2, 8);
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function routeOf(stepId: string, answerId: string, flowSteps: CallFlowStep[]): string {
  const step = flowSteps.find((candidate) => candidate.id === stepId);
  if (!step) return "next";
  if (answerId === "else") return step.other;
  const answer = step.answers.find((candidate) => candidate.id === answerId);
  return answer ? answer.to : "next";
}

function routeKeyOf(row: Element): string {
  return `${row.getAttribute("data-step-id") ?? ""}:${row.getAttribute("data-ans-id") ?? ""}`;
}

/** Rewrites a working model through the temp-id -> server-id mapping of a save. */
function applyIdMap(
  idMap: Map<string, string>,
  createdById: Map<string, AgentStep>
): (step: CallFlowStep) => CallFlowStep {
  return (step) => {
    const created = createdById.get(step.id);
    return {
      ...step,
      id: idMap.get(step.id) ?? step.id,
      rawStep: created ?? step.rawStep,
      answers: step.answers.map((answer) => ({
        ...answer,
        to: idMap.get(answer.to) ?? answer.to,
      })),
      other: idMap.get(step.other) ?? step.other,
    };
  };
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function saveErrorMessage(reason: unknown): string {
  if (reason instanceof ApiError) return apiErrorText(reason, "Could not save the flow. Try again.");
  if (reason instanceof Error && reason.message) return reason.message;
  return "Could not save the flow. Try again.";
}

/** Only network failures, 429s and server errors are worth a timed auto-retry. */
function isRetryableSaveError(reason: unknown): boolean {
  return (
    reason instanceof ApiError &&
    (reason.status === 0 || reason.status === 429 || reason.status >= 500)
  );
}

export function CallFlowEditor({
  agentId,
  agentName,
  steps,
  onStepsChange,
}: {
  agentId: string;
  agentName: string;
  steps: AgentStep[];
  onStepsChange: (steps: AgentStep[]) => void;
}) {
  const [flowSteps, setFlowSteps] = useState<CallFlowStep[]>(() => agentStepsToCallFlow(steps).flowSteps);
  const [title, setTitle] = useState(agentName);
  const [showExport, setShowExport] = useState(false);
  const [copyNote, setCopyNote] = useState("");
  const [sampleArmed, setSampleArmed] = useState(false);
  const [armedDeleteId, setArmedDeleteId] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>({ status: "idle" });

  const flowRef = useRef<HTMLElement | null>(null);
  const stepsRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const liveRef = useRef<HTMLParagraphElement | null>(null);
  const jsonRef = useRef<HTMLTextAreaElement | null>(null);

  const flowStepsRef = useRef(flowSteps);
  const prevFlowStepsRef = useRef(flowSteps);
  const serverStepsRef = useRef(steps);
  const signaturesRef = useRef<Map<string, string>>(new Map());
  const signaturesPrimedRef = useRef(false);
  const dirtyRef = useRef(false);
  const editCounterRef = useRef(0);
  const savingRef = useRef(false);
  const suppressDirtyRef = useRef(false);
  const idleTimerRef = useRef(0);
  const maxTimerRef = useRef(0);
  const retryTimerRef = useRef(0);
  const retryAttemptRef = useRef(0);
  const savedTimerRef = useRef(0);
  const mountedRef = useRef(true);
  const beforeUnloadRef = useRef<(event: BeforeUnloadEvent) => void>((event) => {
    if (!dirtyRef.current) return;
    event.preventDefault();
    event.returnValue = "";
  });
  const wiresRef = useRef<Map<string, WireElements>>(new Map());
  const lastRoutesRef = useRef<RouteSpan[]>([]);
  const curGRef = useRef<GutterConfig>({ g: 96, gap: 10, lead: 20, rad: 8 });
  const hlRef = useRef<HighlightState>({ wire: null, row: null, node: null, focusRow: null, focusNode: null });
  const dragRef = useRef<DragState | null>(null);
  const layoutQueuedRef = useRef(false);
  const layoutRafRef = useRef(0);
  const settleTimerRef = useRef(0);
  const announceTimerRef = useRef(0);
  const pendingFocusAnswerRef = useRef<string | null>(null);
  const pendingFocusGripRef = useRef<string | null>(null);
  const pendingAddStepRef = useRef<string | null>(null);
  const pendingFlashRef = useRef<string | null>(null);

  useEffect(() => {
    setTitle(agentName);
  }, [agentName]);

  // External reloads replace the server snapshot; the working model is only
  // re-derived from the prop when no local edit or save is in flight. A clean
  // mount/sync also seeds the payload signatures from the derived model (same
  // computation as the save module) so untouched steps are not rewritten on
  // the first save. The prime runs once per mounted baseline; post-save
  // propagations keep the authoritative signatures returned by the save.
  useEffect(() => {
    const changed = steps !== serverStepsRef.current;
    serverStepsRef.current = steps;
    if (dirtyRef.current || savingRef.current) return;
    if (!changed && signaturesPrimedRef.current) return;
    signaturesPrimedRef.current = true;
    const derived = agentStepsToCallFlow(steps).flowSteps;
    derived.forEach((step, index) => {
      signaturesRef.current.set(step.id, JSON.stringify(callFlowToStepInput(step, derived, index, {})));
    });
    if (!changed) return;
    suppressDirtyRef.current = true;
    setFlowSteps(derived);
  }, [steps]);

  // Every model change marks the flow dirty and (re)schedules the debounced
  // save. Programmatic updates (id remaps, canonical adoption, prop re-derive)
  // set the suppress flag so they are never treated as edits.
  useEffect(() => {
    const previous = prevFlowStepsRef.current;
    prevFlowStepsRef.current = flowSteps;
    flowStepsRef.current = flowSteps;
    if (previous === flowSteps) return; // initial model (and StrictMode replay)
    if (suppressDirtyRef.current) {
      suppressDirtyRef.current = false;
      return;
    }
    dirtyRef.current = true;
    editCounterRef.current += 1;
    guardUnload();
    if (!savingRef.current) scheduleIdleSave();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flowSteps]);

  // Re-runs after every render: keeps the imperative geometry code on the
  // latest flow, re-sizes the "Agent says" boxes, and applies deferred actions
  // that need the DOM from the fresh render (focus, scroll, flash).
  useEffect(() => {
    flowStepsRef.current = flowSteps;
    autosizeAll();
    processPendingActions();
    layoutSoon();
  });

  useEffect(() => {
    const flowEl = flowRef.current;
    const stepsEl = stepsRef.current;
    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined" && flowEl && stepsEl) {
      observer = new ResizeObserver(() => {
        autosizeAll();
        layoutSoon();
      });
      observer.observe(flowEl);
      observer.observe(stepsEl);
    }
    const handleResize = () => layoutSoon();
    window.addEventListener("resize", handleResize);
    const fonts = document.fonts;
    if (fonts && fonts.ready) {
      void fonts.ready.then(() => {
        autosizeAll();
        layoutSoon();
      });
    }
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", handleResize);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!armedDeleteId) return;
    const timer = window.setTimeout(() => setArmedDeleteId(null), 3200);
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      const button = target?.closest(".del-step") ?? null;
      const nodeId = button?.closest("[data-node-id]")?.getAttribute("data-node-id") ?? null;
      if (nodeId !== armedDeleteId) setArmedDeleteId(null);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [armedDeleteId]);

  useEffect(() => {
    if (!sampleArmed) return;
    const timer = window.setTimeout(() => setSampleArmed(false), 3200);
    return () => window.clearTimeout(timer);
  }, [sampleArmed]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      clearTimer(idleTimerRef);
      clearTimer(maxTimerRef);
      clearTimer(retryTimerRef);
      clearTimer(savedTimerRef);
      unguardUnload();
      if (layoutRafRef.current) cancelAnimationFrame(layoutRafRef.current);
      if (settleTimerRef.current) window.clearTimeout(settleTimerRef.current);
      if (announceTimerRef.current) window.clearTimeout(announceTimerRef.current);
      const drag = dragRef.current;
      if (drag) {
        drag.controller.abort();
        cancelAnimationFrame(drag.raf);
        dragRef.current = null;
      }
      // SPA navigation before the debounce fires: hand the pending model to
      // the save module fire-and-forget. Refs only — no component state and no
      // parent sync from this path; errors are swallowed.
      if (dirtyRef.current && !savingRef.current) {
        dirtyRef.current = false;
        void saveCallFlow({
          agentId,
          model: flowStepsRef.current,
          serverSteps: serverStepsRef.current,
          signatures: signaturesRef.current,
          onIdsMapped: (idMap, createdById) => {
            const apply = applyIdMap(idMap, createdById);
            flowStepsRef.current = flowStepsRef.current.map(apply);
          },
        }).catch(() => {});
      }
    };
  }, []);

  const stats = useMemo(() => {
    const ids = flowSteps.map((step) => step.id);
    const reachable = computeReachability(ids, (id) => {
      const index = ids.indexOf(id);
      const step = flowSteps[index];
      if (!step) return [];
      return [...step.answers.map((answer) => answer.to), step.other].map((target) =>
        resolveCallFlowRoute(target, index, flowSteps)
      );
    });
    let totalRoutes = 0;
    let routesToEnd = 0;
    flowSteps.forEach((step, index) => {
      [...step.answers.map((answer) => answer.to), step.other].forEach((target) => {
        totalRoutes += 1;
        const resolved = resolveCallFlowRoute(target, index, flowSteps);
        if (resolved === "end") routesToEnd += 1;
      });
    });
    const unreachedIds = new Set(
      flowSteps.filter((step) => !reachable.has(step.id)).map((step) => step.id)
    );
    return { totalRoutes, routesToEnd, unreachedIds };
  }, [flowSteps]);

  const endCountText = stats.routesToEnd
    ? `${plural(stats.routesToEnd, "route ends", "routes end")} here`
    : "No routes end here";

  /* ---------- Autosize, announcements, pending DOM actions ---------- */

  function autosize(el: HTMLTextAreaElement) {
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`;
  }

  function autosizeAll() {
    stepsRef.current?.querySelectorAll<HTMLTextAreaElement>(".say").forEach((el) => autosize(el));
  }

  function announce(message: string) {
    const el = liveRef.current;
    if (!el) return;
    el.textContent = "";
    window.clearTimeout(announceTimerRef.current);
    announceTimerRef.current = window.setTimeout(() => {
      el.textContent = message;
    }, 30);
  }

  function flashNode(id: string) {
    const el = stepsRef.current?.querySelector<HTMLElement>(`.node[data-node-id="${id}"]`);
    if (!el) return;
    el.classList.remove("flash");
    void el.offsetWidth;
    el.classList.add("flash");
  }

  function processPendingActions() {
    const answerId = pendingFocusAnswerRef.current;
    if (answerId) {
      pendingFocusAnswerRef.current = null;
      stepsRef.current
        ?.querySelector<HTMLInputElement>(`.answer[data-ans-id="${answerId}"] .ans-input`)
        ?.focus();
    }
    const gripId = pendingFocusGripRef.current;
    if (gripId) {
      pendingFocusGripRef.current = null;
      stepsRef.current?.querySelector<HTMLButtonElement>(`.node[data-node-id="${gripId}"] .grip`)?.focus();
    }
    const addedId = pendingAddStepRef.current;
    if (addedId) {
      pendingAddStepRef.current = null;
      const el = stepsRef.current?.querySelector<HTMLElement>(`.node[data-node-id="${addedId}"]`);
      if (el) {
        el.scrollIntoView({ block: "center", behavior: prefersReducedMotion() ? "auto" : "smooth" });
        const titleInput = el.querySelector<HTMLInputElement>(".title");
        titleInput?.focus();
        titleInput?.select();
        flashNode(addedId);
      }
    }
    const flashId = pendingFlashRef.current;
    if (flashId) {
      pendingFlashRef.current = null;
      flashNode(flashId);
    }
  }

  /* ---------- Route geometry ---------- */

  function measure(): Measurement {
    const flowEl = flowRef.current;
    const stepsEl = stepsRef.current;
    if (!flowEl || !stepsEl) return { nodes: {}, routes: [], width: 0, height: 0 };
    const flowRect = flowEl.getBoundingClientRect();
    const nodes: Record<string, MeasuredNode> = {};
    flowEl.querySelectorAll<HTMLElement>("[data-node-id]").forEach((el) => {
      const id = el.dataset.nodeId;
      if (!id) return;
      const rect = el.getBoundingClientRect();
      const head = el.querySelector<HTMLElement>(".node-head") ?? el;
      const headRect = head.getBoundingClientRect();
      nodes[id] = { right: rect.right - flowRect.left, y: headRect.top + headRect.height / 2 - flowRect.top };
    });
    const current = flowStepsRef.current;
    const routes: RouteSpan[] = [];
    stepsEl.querySelectorAll<HTMLElement>("[data-route-key]").forEach((row) => {
      const stepId = row.dataset.stepId;
      const answerId = row.dataset.ansId;
      const key = row.dataset.routeKey;
      const index = stepId ? current.findIndex((step) => step.id === stepId) : -1;
      const node = stepId ? nodes[stepId] : undefined;
      const sourceKey = key ?? (stepId && answerId ? `${stepId}:${answerId}` : null);
      if (!stepId || !answerId || !sourceKey || index < 0 || !node) return;
      const to = resolveCallFlowRoute(routeOf(stepId, answerId, current), index, current);
      const targetNode = nodes[to];
      if (!targetNode) return;
      const rowRect = row.getBoundingClientRect();
      routes.push({
        key: sourceKey,
        from: stepId,
        to,
        x0: node.right,
        y0: rowRect.top + rowRect.height / 2 - flowRect.top,
        x1: targetNode.right,
        y1: 0,
        exit: to === "end",
      });
    });
    const byTarget: Record<string, RouteSpan[]> = {};
    routes.forEach((route) => {
      const list = byTarget[route.to];
      if (list) list.push(route);
      else byTarget[route.to] = [route];
    });
    Object.keys(byTarget).forEach((target) => {
      const list = byTarget[target];
      const targetNode = nodes[target];
      if (!list || !targetNode) return;
      list.sort((a, b) => a.y0 - b.y0);
      const count = list.length;
      const step = count > 1 ? Math.min(7, 24 / (count - 1)) : 0;
      list.forEach((route, index) => {
        route.y1 = targetNode.y + (index - (count - 1) / 2) * step;
      });
    });
    return { nodes, routes, width: flowRect.width, height: flowRect.height };
  }

  function ensureWire(key: string): WireElements | null {
    const existing = wiresRef.current.get(key);
    if (existing) return existing;
    const svg = svgRef.current;
    if (!svg) return null;
    const g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("class", "wire");
    const line = document.createElementNS(SVG_NS, "path");
    line.setAttribute("class", "line");
    const arrow = document.createElementNS(SVG_NS, "path");
    arrow.setAttribute("class", "arrow");
    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("class", "dot");
    dot.setAttribute("r", "3.5");
    const hit = document.createElementNS(SVG_NS, "path");
    hit.setAttribute("class", "hit");
    hit.setAttribute("data-key", key);
    g.appendChild(line);
    g.appendChild(arrow);
    g.appendChild(dot);
    g.appendChild(hit);
    svg.appendChild(g);
    const wire: WireElements = { g, line, arrow, dot, hit };
    wiresRef.current.set(key, wire);
    return wire;
  }

  function draw(m: Measurement, lanes: LaneAssignment, gutter: GutterConfig) {
    const svg = svgRef.current;
    if (!svg) return;
    svg.setAttribute("width", String(m.width));
    svg.setAttribute("height", String(m.height));
    const live: Record<string, true> = {};
    m.routes.forEach((route) => {
      live[route.key] = true;
      const wire = ensureWire(route.key);
      if (!wire) return;
      const lx = route.x0 + gutter.lead + (lanes.map[route.key] ?? 0) * gutter.gap;
      const d = pathD(route.x0, route.y0, lx, route.y1, route.x1, gutter.rad);
      wire.line.setAttribute("d", d);
      wire.hit.setAttribute("d", d);
      wire.arrow.setAttribute("d", `M${route.x1 + 1} ${route.y1}l7 -3.5v7z`);
      wire.dot.setAttribute("cx", String(route.x0));
      wire.dot.setAttribute("cy", String(route.y0));
      wire.g.classList.toggle("exit", route.exit);
    });
    wiresRef.current.forEach((wire, key) => {
      if (!live[key]) {
        wire.g.remove();
        wiresRef.current.delete(key);
      }
    });
    lastRoutesRef.current = m.routes;
    applyHighlight();
  }

  function setGutter(gutter: GutterConfig) {
    curGRef.current = gutter;
    flowRef.current?.style.setProperty("--gutter", `${gutter.g}px`);
  }

  function layout() {
    if (dragRef.current) return;
    let m = measure();
    let lanes = assignLanes(m.routes);
    for (let pass = 0; pass < 3; pass++) {
      const want = gutterFor(lanes.count, m.width);
      if (want.g === curGRef.current.g && want.gap === curGRef.current.gap) break;
      setGutter(want);
      m = measure();
      lanes = assignLanes(m.routes);
    }
    draw(m, lanes, curGRef.current);
  }

  function layoutSoon() {
    if (layoutQueuedRef.current) return;
    layoutQueuedRef.current = true;
    layoutRafRef.current = requestAnimationFrame(() => {
      layoutQueuedRef.current = false;
      layout();
    });
  }

  function drawFrozen(lanes: LaneAssignment) {
    const m = measure();
    draw(m, lanes, curGRef.current);
  }

  /* ---------- Tracing (hover and focus) ---------- */

  function activeKeys(): string[] | null {
    const hl = hlRef.current;
    if (hl.wire) return [hl.wire];
    const row = hl.row ?? hl.focusRow;
    if (row) return [row];
    const node = hl.node ?? hl.focusNode;
    if (node) {
      return lastRoutesRef.current
        .filter((route) => route.from === node || route.to === node)
        .map((route) => route.key);
    }
    return null;
  }

  function applyHighlight() {
    const keys = activeKeys();
    const highlighted = keys ? new Set(keys) : null;
    const svg = svgRef.current;
    if (svg) {
      svg.classList.toggle("has-hl", Boolean(highlighted && keys && keys.length > 0));
    }
    wiresRef.current.forEach((wire, key) => {
      wire.g.classList.toggle("hl", Boolean(highlighted && highlighted.has(key)));
    });
    stepsRef.current?.querySelectorAll<HTMLElement>(".answer").forEach((row) => {
      row.classList.toggle("hl", Boolean(highlighted && highlighted.has(routeKeyOf(row))));
    });
    const targets = new Set<string>();
    if (highlighted) {
      lastRoutesRef.current.forEach((route) => {
        if (highlighted.has(route.key)) targets.add(route.to);
      });
    }
    flowRef.current?.querySelectorAll<HTMLElement>("[data-node-id]").forEach((el) => {
      el.classList.toggle("hl-to", Boolean(el.dataset.nodeId && targets.has(el.dataset.nodeId)));
    });
  }

  function clearHover() {
    const hl = hlRef.current;
    hl.wire = null;
    hl.row = null;
    hl.node = null;
  }

  function handleMouseOver(event: ReactMouseEvent<HTMLElement>) {
    if (dragRef.current) return;
    const target = event.target instanceof Element ? event.target : null;
    clearHover();
    if (target) {
      const hit = target.closest(".hit");
      if (hit) {
        hlRef.current.wire = hit.getAttribute("data-key");
      } else {
        const row = target.closest(".answer");
        if (row) {
          hlRef.current.row = routeKeyOf(row);
        } else {
          const node = target.closest("[data-node-id]");
          hlRef.current.node = node?.getAttribute("data-node-id") ?? null;
        }
      }
    }
    applyHighlight();
  }

  function handleMouseLeave() {
    clearHover();
    applyHighlight();
  }

  function handleFocusIn(event: ReactFocusEvent<HTMLElement>) {
    const target = event.target instanceof Element ? event.target : null;
    const hl = hlRef.current;
    hl.focusRow = null;
    hl.focusNode = null;
    if (target) {
      const row = target.closest(".answer");
      if (row) hl.focusRow = routeKeyOf(row);
      const node = target.closest("[data-node-id]");
      if (node) hl.focusNode = node.getAttribute("data-node-id");
    }
    applyHighlight();
  }

  function handleFocusOut() {
    const hl = hlRef.current;
    hl.focusRow = null;
    hl.focusNode = null;
    applyHighlight();
  }

  /* ---------- Editing ---------- */

  function updateStep(stepId: string, patch: Partial<CallFlowStep>) {
    setFlowSteps((current) =>
      current.map((step) => (step.id === stepId ? { ...step, ...patch } : step))
    );
  }

  function updateAnswerLabel(stepId: string, answerId: string, label: string) {
    setFlowSteps((current) =>
      current.map((step) =>
        step.id === stepId
          ? {
              ...step,
              answers: step.answers.map((answer) => (answer.id === answerId ? { ...answer, label } : answer)),
            }
          : step
      )
    );
  }

  function handleRouteChange(stepId: string, answerId: string, value: string) {
    setFlowSteps((current) =>
      current.map((step) => {
        if (step.id !== stepId) return step;
        if (answerId === "else") return { ...step, other: value };
        return {
          ...step,
          answers: step.answers.map((answer) => (answer.id === answerId ? { ...answer, to: value } : answer)),
        };
      })
    );
  }

  function addAnswer(stepId: string) {
    const answer: CallFlowAnswer = { id: uid("a"), label: "", to: "next" };
    pendingFocusAnswerRef.current = answer.id;
    setFlowSteps((current) =>
      current.map((step) => (step.id === stepId ? { ...step, answers: [...step.answers, answer] } : step))
    );
  }

  function removeAnswer(stepId: string, answerId: string) {
    setFlowSteps((current) =>
      current.map((step) =>
        step.id === stepId
          ? { ...step, answers: step.answers.filter((answer) => answer.id !== answerId) }
          : step
      )
    );
  }

  function addStep() {
    const step: CallFlowStep = {
      id: uid("s"),
      title: "New step",
      say: "",
      answers: [],
      other: "next",
    };
    pendingAddStepRef.current = step.id;
    setFlowSteps((current) => [...current, step]);
  }

  function deleteStep(stepId: string) {
    const current = flowStepsRef.current;
    if (current.length <= 1) return;
    const index = current.findIndex((step) => step.id === stepId);
    if (index < 0) return;
    const following = current[index + 1] ? current[index + 1].id : "end";
    const next = current
      .filter((step) => step.id !== stepId)
      .map((step) => ({
        ...step,
        answers: step.answers.map((answer) => (answer.to === stepId ? { ...answer, to: following } : answer)),
        other: step.other === stepId ? following : step.other,
      }));
    setFlowSteps(next);
    announce("Step deleted");
  }

  function handleDeleteStepClick(stepId: string) {
    if (armedDeleteId === stepId) {
      setArmedDeleteId(null);
      deleteStep(stepId);
      return;
    }
    setArmedDeleteId(stepId);
  }

  function moveStep(stepId: string, to: number) {
    const current = flowStepsRef.current;
    const from = current.findIndex((step) => step.id === stepId);
    if (from < 0 || to < 0 || to >= current.length || to === from) return;
    const moved = current[from];
    const next = current.filter((step) => step.id !== stepId);
    next.splice(to, 0, moved);
    pendingFocusGripRef.current = stepId;
    setFlowSteps(next);
    announce(`Step moved to position ${to + 1} of ${next.length}`);
  }

  function handleGripKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, stepId: string) {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const index = flowStepsRef.current.findIndex((step) => step.id === stepId);
    if (index < 0) return;
    moveStep(stepId, event.key === "ArrowUp" ? index - 1 : index + 1);
  }

  /* ---------- Drag to reorder ---------- */

  function handleGripPointerDown(event: ReactPointerEvent<HTMLButtonElement>) {
    const grip = event.currentTarget;
    const card = grip.closest<HTMLElement>(".node");
    if (!card) return;
    startDrag(event, grip, card);
  }

  function startDrag(event: ReactPointerEvent<HTMLButtonElement>, grip: HTMLButtonElement, card: HTMLElement) {
    if (event.button !== 0) return;
    event.preventDefault();
    const stepsEl = stepsRef.current;
    if (!stepsEl || dragRef.current) return;
    const els = Array.from(stepsEl.querySelectorAll<HTMLElement>(":scope > .node"));
    const scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;
    const controller = new AbortController();
    dragRef.current = {
      el: card,
      grip,
      els,
      idx: els.indexOf(card),
      target: els.indexOf(card),
      tops: els.map((node) => node.getBoundingClientRect().top + scrollY),
      hs: els.map((node) => node.offsetHeight),
      startY: event.clientY,
      startScroll: scrollY,
      y: event.clientY,
      settling: false,
      raf: 0,
      lanes: assignLanes(measure().routes),
      controller,
    };
    try {
      grip.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture can be unavailable; drag still works through the listeners.
    }
    card.classList.add("dragging");
    flowRef.current?.classList.add("is-dragging");
    clearHover();
    hlRef.current.focusRow = null;
    hlRef.current.focusNode = null;
    applyHighlight();
    const { signal } = controller;
    grip.addEventListener("pointermove", onDragMove, { signal });
    grip.addEventListener("pointerup", onDragUp, { signal });
    grip.addEventListener("pointercancel", onDragCancel, { signal });
    document.addEventListener("keydown", onDragKey, { signal });
    tick();
  }

  function onDragMove(event: PointerEvent) {
    const drag = dragRef.current;
    if (drag) drag.y = event.clientY;
  }

  function onDragUp() {
    endDrag(true);
  }

  function onDragCancel() {
    endDrag(false);
  }

  function onDragKey(event: KeyboardEvent) {
    if (event.key === "Escape") endDrag(false);
  }

  function tick() {
    const drag = dragRef.current;
    if (!drag) return;
    if (!drag.settling) {
      const viewportHeight = window.innerHeight;
      if (drag.y < 64) window.scrollBy(0, -14);
      else if (drag.y > viewportHeight - 64) window.scrollBy(0, 14);
      updateDrag();
    } else {
      drawFrozen(drag.lanes);
    }
    drag.raf = requestAnimationFrame(tick);
  }

  function updateDrag() {
    const drag = dragRef.current;
    if (!drag) return;
    const scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;
    let dy = drag.y - drag.startY + (scrollY - drag.startScroll);
    const last = drag.els.length - 1;
    const minDy = drag.tops[0] - drag.tops[drag.idx];
    const maxDy = drag.tops[last] + drag.hs[last] - (drag.tops[drag.idx] + drag.hs[drag.idx]);
    dy = Math.max(minDy, Math.min(maxDy, dy));
    drag.el.style.transform = `translateY(${dy}px)`;
    const center = drag.tops[drag.idx] + drag.hs[drag.idx] / 2 + dy;
    let target = 0;
    drag.els.forEach((_, index) => {
      if (index === drag.idx) return;
      if (drag.tops[index] + drag.hs[index] / 2 < center) target += 1;
    });
    drag.target = target;
    const shift = drag.hs[drag.idx] + GAP;
    drag.els.forEach((node, index) => {
      if (index === drag.idx) return;
      let offset = 0;
      if (index < drag.idx && index >= target) offset = shift;
      else if (index > drag.idx && index <= target) offset = -shift;
      node.style.transform = offset ? `translateY(${offset}px)` : "";
    });
    drawFrozen(drag.lanes);
  }

  function endDrag(commit: boolean) {
    const drag = dragRef.current;
    if (!drag || drag.settling) return;
    drag.controller.abort();
    const reorder = commit && drag.target !== drag.idx;
    drag.settling = true;
    drag.el.classList.add("settling");
    if (reorder) {
      const order = drag.els.map((_, index) => index);
      order.splice(drag.idx, 1);
      order.splice(drag.target, 0, drag.idx);
      let y = drag.tops[0];
      let finalTop = drag.tops[drag.idx];
      order.forEach((index) => {
        if (index === drag.idx) finalTop = y;
        y += drag.hs[index] + GAP;
      });
      drag.el.style.transform = `translateY(${finalTop - drag.tops[drag.idx]}px)`;
    } else {
      drag.el.style.transform = "";
      drag.els.forEach((node) => {
        if (node !== drag.el) node.style.transform = "";
      });
    }
    settleTimerRef.current = window.setTimeout(() => finishDrag(drag, reorder), 190);
  }

  function finishDrag(drag: DragState, reorder: boolean) {
    cancelAnimationFrame(drag.raf);
    drag.els.forEach((node) => {
      node.style.transform = "";
      node.classList.remove("dragging", "settling");
    });
    flowRef.current?.classList.remove("is-dragging");
    dragRef.current = null;
    if (reorder) {
      const current = flowStepsRef.current;
      const moved = current[drag.idx];
      if (moved) {
        const next = [...current];
        next.splice(drag.idx, 1);
        next.splice(drag.target, 0, moved);
        pendingFlashRef.current = moved.id;
        setFlowSteps(next);
        announce(`Step moved to position ${drag.target + 1} of ${next.length}`);
      }
    } else {
      layoutSoon();
    }
  }

  /* ---------- Export and sample ---------- */

  function toExport() {
    const name = (target: string, index: number): string => {
      const resolved = resolveCallFlowRoute(target, index, flowSteps);
      if (resolved === "end") return resolved;
      return `step_${flowSteps.findIndex((step) => step.id === resolved) + 1}`;
    };
    return {
      name: title,
      start: flowSteps.length ? "step_1" : null,
      steps: flowSteps.map((step, index) => ({
        id: `step_${index + 1}`,
        title: step.title,
        say: step.say,
        routes: step.answers
          .map((answer) => ({ when: answer.label, go: name(answer.to, index) }))
          .concat([{ when: "*", go: name(step.other, index) }]),
      })),
    };
  }

  function toggleExport() {
    const next = !showExport;
    setShowExport(next);
    if (next) {
      setCopyNote('"when" is what the caller says; "*" is the anything-else route.');
    }
  }

  function handleCopy() {
    const json = JSON.stringify(toExport(), null, 2);
    const fail = () => {
      jsonRef.current?.focus();
      jsonRef.current?.select();
      setCopyNote("Copy was blocked here. The text is selected, press Ctrl+C or Cmd+C.");
    };
    try {
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(json).then(() => {
          setCopyNote("Copied to clipboard.");
        }, fail);
      } else {
        fail();
      }
    } catch {
      fail();
    }
  }

  function handleLoadSample() {
    if (!sampleArmed) {
      setSampleArmed(true);
      return;
    }
    setSampleArmed(false);
    setFlowSteps(getSampleHearingScript());
    announce("Sample script loaded");
  }

  /* ---------- Auto-save ---------- */

  function clearTimer(ref: { current: number }) {
    if (ref.current) {
      window.clearTimeout(ref.current);
      ref.current = 0;
    }
  }

  function guardUnload() {
    window.addEventListener("beforeunload", beforeUnloadRef.current);
  }

  function unguardUnload() {
    window.removeEventListener("beforeunload", beforeUnloadRef.current);
  }

  function scheduleIdleSave() {
    clearTimer(idleTimerRef);
    idleTimerRef.current = window.setTimeout(() => {
      idleTimerRef.current = 0;
      void runSave();
    }, SAVE_IDLE_MS);
    if (!maxTimerRef.current) {
      maxTimerRef.current = window.setTimeout(() => {
        maxTimerRef.current = 0;
        void runSave();
      }, SAVE_MAX_MS);
    }
  }

  function scheduleNextPass() {
    clearTimer(idleTimerRef);
    idleTimerRef.current = window.setTimeout(() => {
      idleTimerRef.current = 0;
      void runSave();
    }, SAVE_COALESCE_MS);
  }

  function showSaved() {
    clearTimer(savedTimerRef);
    setSaveState({ status: "saved" });
    savedTimerRef.current = window.setTimeout(() => {
      savedTimerRef.current = 0;
      if (mountedRef.current && !dirtyRef.current && !savingRef.current) {
        setSaveState({ status: "idle" });
      }
    }, SAVED_FADE_MS);
  }

  function flush() {
    retryAttemptRef.current = 0;
    void runSave();
  }

  // Single-flight save: clears pending timers, then diffs and persists the
  // current model. Edits made mid-save are carried into the next pass, and
  // transient failures back off (2 s / 10 s / 30 s) while edits remain.
  async function runSave() {
    clearTimer(idleTimerRef);
    clearTimer(maxTimerRef);
    clearTimer(retryTimerRef);
    if (savingRef.current || !dirtyRef.current) return;
    clearTimer(savedTimerRef);
    const captured = editCounterRef.current;
    savingRef.current = true;
    setSaveState({ status: "saving" });
    let autoRetryBlocked = false;
    try {
      const result = await saveCallFlow({
        agentId,
        model: flowStepsRef.current,
        serverSteps: serverStepsRef.current,
        signatures: signaturesRef.current,
        onIdsMapped: (idMap, createdById) => {
          suppressDirtyRef.current = true;
          const apply = applyIdMap(idMap, createdById);
          // Functional update: composes the id remap with any keystrokes that
          // are queued but not yet rendered, so they are never clobbered.
          setFlowSteps((current) => {
            const remapped = current.map(apply);
            flowStepsRef.current = remapped;
            return remapped;
          });
        },
      });
      serverStepsRef.current = result.serverSteps;
      signaturesRef.current = result.signatures;
      retryAttemptRef.current = 0;
      onStepsChange(result.serverSteps);
      if (!mountedRef.current) return;
      if (editCounterRef.current === captured) {
        dirtyRef.current = false;
        unguardUnload();
        suppressDirtyRef.current = true;
        const canonical = agentStepsToCallFlow(result.serverSteps).flowSteps;
        flowStepsRef.current = canonical;
        setFlowSteps(canonical);
        showSaved();
      } else {
        scheduleNextPass();
      }
    } catch (reason) {
      if (!mountedRef.current) return;
      autoRetryBlocked = !isRetryableSaveError(reason);
      setSaveState({ status: "error", message: `Couldn't save — ${saveErrorMessage(reason)}` });
      if (!autoRetryBlocked && dirtyRef.current) {
        const attempt = retryAttemptRef.current;
        const delay = SAVE_RETRY_MS[Math.min(attempt, SAVE_RETRY_MS.length - 1)];
        retryAttemptRef.current = attempt + 1;
        retryTimerRef.current = window.setTimeout(() => {
          retryTimerRef.current = 0;
          void runSave();
        }, delay);
      }
    } finally {
      savingRef.current = false;
      if (
        mountedRef.current &&
        dirtyRef.current &&
        !autoRetryBlocked &&
        !idleTimerRef.current &&
        !maxTimerRef.current &&
        !retryTimerRef.current
      ) {
        scheduleIdleSave();
      }
    }
  }

  /* ---------- Rendering helpers ---------- */

  function renderRouteOptions(selfId: string) {
    return (
      <>
        <optgroup label="Flow">
          <option value="next">Next step</option>
          <option value="end">End call</option>
        </optgroup>
        <optgroup label="Go to step">
          {flowSteps.map((step, index) =>
            step.id === selfId ? null : (
              <option key={step.id} value={step.id}>
                {`Step ${index + 1} · ${step.title || "Untitled"}`}
              </option>
            )
          )}
        </optgroup>
      </>
    );
  }

  const exportJson = JSON.stringify(toExport(), null, 2);

  return (
    <div className="call-flow-app">
      <header className="top">
        <div className="top-main">
          <label className="eyebrow" htmlFor="scriptName">
            Voice call script
          </label>
          <input
            id="scriptName"
            className="script-name"
            type="text"
            autoComplete="off"
            spellCheck={false}
            aria-label="Script name"
            value={title}
            onChange={(event) => setTitle(event.currentTarget.value)}
          />
          <p className="stats" aria-live="polite">
            <span>
              <b>{flowSteps.length}</b> {flowSteps.length === 1 ? "step" : "steps"}
            </span>
            <span>
              <b>{stats.totalRoutes}</b> routes
            </span>
            {stats.unreachedIds.size > 0 ? (
              <span className="warn">
                {plural(stats.unreachedIds.size, "step has", "steps have")} no route leading to{" "}
                {stats.unreachedIds.size === 1 ? "it" : "them"}
              </span>
            ) : null}
          </p>
        </div>
        <div className="top-actions">
          <button
            type="button"
            className="flow-btn"
            aria-expanded={showExport}
            aria-controls="exportPanel"
            onClick={toggleExport}
          >
            Export JSON
          </button>
          <button
            type="button"
            className={`flow-btn${sampleArmed ? " armed" : ""}`}
            onClick={handleLoadSample}
          >
            {sampleArmed ? "Replace script?" : "Load sample"}
          </button>
          <button
            type="button"
            className={`save-status ${saveState.status}`}
            onClick={flush}
          >
            {saveState.status === "idle" ? "Auto-saves as you edit" : null}
            {saveState.status === "saving" ? "Saving…" : null}
            {saveState.status === "saved" ? "Saved" : null}
            {saveState.status === "error" ? (
              <>
                {saveState.message}
                <span className="retry">Retry</span>
              </>
            ) : null}
          </button>
        </div>
      </header>

      <div className="guide">
        <ul className="legend" aria-label="Line key">
          <li>
            <svg width="34" height="10" viewBox="0 0 34 10" aria-hidden="true">
              <path className="lg-line" d="M2 5H25" />
              <path className="lg-arrow" d="M31 5l-6 -3v6z" />
            </svg>
            Goes to another step
          </li>
          <li>
            <svg width="34" height="10" viewBox="0 0 34 10" aria-hidden="true">
              <path className="lg-line lg-dash" d="M2 5H25" />
              <path className="lg-arrow" d="M31 5l-6 -3v6z" />
            </svg>
            Ends the call
          </li>
        </ul>
        <p className="hint">Drag the handle to reorder. Hover an answer or a line to trace it. Sample content, edit freely.</p>
      </div>

      <section className="export-panel" id="exportPanel" hidden={!showExport} aria-label="Script as JSON">
        <div className="export-head">
          <strong>Script as JSON</strong>
          <button type="button" className="flow-btn" onClick={handleCopy}>
            Copy
          </button>
        </div>
        <textarea ref={jsonRef} value={exportJson} readOnly spellCheck={false} aria-label="Script JSON" />
        <p className="export-note">{copyNote}</p>
      </section>

      <main
        className="flow"
        ref={flowRef}
        onMouseOver={handleMouseOver}
        onMouseLeave={handleMouseLeave}
        onFocus={handleFocusIn}
        onBlur={handleFocusOut}
      >
        <svg className="wires" ref={svgRef} aria-hidden="true" />
        <div id="steps" ref={stepsRef}>
          {flowSteps.map((flowStep, index) => {
            const unreached = stats.unreachedIds.has(flowStep.id);
            const deleteArmed = armedDeleteId === flowStep.id;
            return (
              <article key={flowStep.id} className="node" data-node-id={flowStep.id} role="group" aria-label={`Step ${index + 1}`}>
                <div className="node-head">
                  <button
                    type="button"
                    className="grip"
                    aria-label={`Reorder step ${index + 1}. Drag, or press arrow up or down.`}
                    onPointerDown={handleGripPointerDown}
                    onKeyDown={(event) => handleGripKeyDown(event, flowStep.id)}
                  >
                    {ICONS.grip}
                  </button>
                  <span className="num">{index + 1}</span>
                  <input
                    className="title"
                    type="text"
                    value={flowStep.title}
                    placeholder="Step name"
                    aria-label={`Step ${index + 1} name`}
                    autoComplete="off"
                    onChange={(event) => updateStep(flowStep.id, { title: event.currentTarget.value })}
                  />
                  {index === 0 ? <span className="flag flag-start">Start</span> : null}
                  {unreached ? <span className="flag flag-warn">Not reached</span> : null}
                  <button
                    type="button"
                    className={`icon-btn del-step${deleteArmed ? " armed" : ""}`}
                    aria-label={`Delete step ${index + 1}`}
                    disabled={flowSteps.length <= 1}
                    onClick={() => handleDeleteStepClick(flowStep.id)}
                  >
                    {ICONS.trash}
                    <span className="del-text">Delete?</span>
                  </button>
                </div>
                <div className="node-body">
                  <label className="field-label" htmlFor={`say-${flowStep.id}`}>
                    Agent says
                  </label>
                  <textarea
                    id={`say-${flowStep.id}`}
                    className="say"
                    rows={2}
                    spellCheck={false}
                    value={flowStep.say}
                    onChange={(event) => {
                      autosize(event.currentTarget);
                      updateStep(flowStep.id, { say: event.currentTarget.value });
                    }}
                  />
                  <div className="answers">
                    <div className="answers-head" aria-hidden="true">
                      <span>If the caller says</span>
                      <span></span>
                      <span>Go to</span>
                      <span></span>
                    </div>
                    {flowStep.answers.map((answer) => (
                      <div
                        key={answer.id}
                        className="answer"
                        data-step-id={flowStep.id}
                        data-ans-id={answer.id}
                        data-route-key={`${flowStep.id}:${answer.id}`}
                      >
                        <input
                          className="ans-input"
                          type="text"
                          value={answer.label}
                          placeholder="e.g. Yes, interested"
                          aria-label="When the caller says"
                          autoComplete="off"
                          onChange={(event) => updateAnswerLabel(flowStep.id, answer.id, event.currentTarget.value)}
                        />
                        <span className="to" aria-hidden="true">
                          {ICONS.arrow}
                        </span>
                        <span className="route">
                          <select
                            value={answer.to}
                            aria-label="Then go to"
                            onChange={(event) => handleRouteChange(flowStep.id, answer.id, event.currentTarget.value)}
                          >
                            {renderRouteOptions(flowStep.id)}
                          </select>
                        </span>
                        <button
                          type="button"
                          className="icon-btn"
                          aria-label="Remove this answer"
                          onClick={() => removeAnswer(flowStep.id, answer.id)}
                        >
                          {ICONS.x}
                        </button>
                      </div>
                    ))}
                    <div
                      className="answer is-else"
                      data-step-id={flowStep.id}
                      data-ans-id="else"
                      data-route-key={`${flowStep.id}:else`}
                    >
                      <span className="ans-fixed">Anything else</span>
                      <span className="to" aria-hidden="true">
                        {ICONS.arrow}
                      </span>
                      <span className="route">
                        <select
                          value={flowStep.other}
                          aria-label="When the caller says anything else, go to"
                          onChange={(event) => handleRouteChange(flowStep.id, "else", event.currentTarget.value)}
                        >
                          {renderRouteOptions(flowStep.id)}
                        </select>
                      </span>
                      <span className="icon-spacer"></span>
                    </div>
                    <button type="button" className="add-answer" onClick={() => addAnswer(flowStep.id)}>
                      {ICONS.plus} Add answer
                    </button>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
        <div className="add-row">
          <button type="button" className="add-step" onClick={addStep}>
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" aria-hidden="true">
              <path d="M7 2v10M2 7h10" />
            </svg>
            Add step
          </button>
        </div>

        <div className="eyebrow exits-label">Exits</div>
        <div className="exits">
          <article className="node term term-end" data-node-id="end" aria-label="End call">
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M2.5 12.2c4.2-3.6 10.8-3.6 15 0l-1.8 2.6-3-1.2v-2.2a8.6 8.6 0 0 0-5.4 0v2.2l-3 1.2z" />
            </svg>
            <div className="term-main">
              <span className="term-title">End call</span>
              <span className="term-sub">{endCountText}</span>
            </div>
          </article>
        </div>
      </main>
      <p className="sr-only" aria-live="polite" ref={liveRef}></p>
    </div>
  );
}

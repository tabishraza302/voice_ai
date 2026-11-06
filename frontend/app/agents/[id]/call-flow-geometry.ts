export interface MeasuredNode {
  right: number;
  y: number;
}

export interface RouteSpan {
  key: string;
  from: string;
  to: string;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  exit: boolean;
}

export interface LaneAssignment {
  map: Record<string, number>;
  count: number;
}

export interface GutterConfig {
  g: number;
  gap: number;
  lead: number;
  rad: number;
}

const PAD = 7;

/**
 * Assigns non-overlapping horizontal lane offsets in the right gutter to avoid line collisions.
 */
export function assignLanes(routes: RouteSpan[], pad: number = PAD): LaneAssignment {
  const items = routes.map((r) => ({
    r,
    a: Math.min(r.y0, r.y1),
    b: Math.max(r.y0, r.y1),
  }));

  // Shortest spans first, then top-most
  items.sort((p, q) => p.b - p.a - (q.b - q.a) || p.a - q.a);

  const lanes: Array<typeof items> = [];
  const map: Record<string, number> = {};

  items.forEach((it) => {
    let lane = 0;
    for (;; lane++) {
      if (!lanes[lane]) {
        lanes[lane] = [];
      }
      const spans = lanes[lane];
      const clash = spans.some((s) => it.a < s.b + pad && s.a < it.b + pad);
      if (!clash) {
        spans.push(it);
        break;
      }
    }
    map[it.r.key] = lane;
  });

  return { map, count: lanes.length };
}

/**
 * Calculates dynamic gutter width based on lane count and container width.
 */
export function gutterFor(count: number, width: number): GutterConfig {
  const narrow = width < 600;
  const lead = narrow ? 14 : 20;
  const maxG = narrow ? 88 : 180;
  const tail = 10;
  const n = Math.max(count, 1);
  const gap = Math.max(narrow ? 4 : 5, Math.min(11, (maxG - lead - tail) / n));
  const g = Math.round(lead + (n - 1) * gap + tail + 4);

  return {
    g: Math.max(narrow ? 48 : 72, g),
    gap,
    lead,
    rad: narrow ? 5 : 8,
  };
}

/**
 * Generates an SVG path string for a rounded connector line with quadratic beziers.
 */
export function pathD(
  x0: number,
  y0: number,
  lx: number,
  y1: number,
  x1: number,
  rad: number
): string {
  const dy = y1 - y0;
  if (Math.abs(dy) < 2) {
    return `M ${x0} ${y0} H ${x1 + 7}`;
  }
  const dir = dy > 0 ? 1 : -1;
  const r = Math.min(rad, Math.abs(dy) / 2);

  return (
    `M ${x0} ${y0} ` +
    `H ${lx - r} ` +
    `Q ${lx} ${y0} ${lx} ${y0 + dir * r} ` +
    `V ${y1 - dir * r} ` +
    `Q ${lx} ${y1} ${lx - r} ${y1} ` +
    `H ${x1 + 7}`
  );
}

/**
 * BFS graph reachability: computes all step IDs reachable starting from the entry step (first step).
 */
export function computeReachability(
  stepIds: string[],
  getTargets: (id: string) => string[]
): Set<string> {
  const reachable = new Set<string>();
  if (stepIds.length === 0) return reachable;

  const queue: string[] = [stepIds[0]];
  reachable.add(stepIds[0]);

  const stepSet = new Set(stepIds);

  while (queue.length > 0) {
    const current = queue.shift()!;
    const targets = getTargets(current);
    for (const target of targets) {
      if (target !== "end" && target !== "transfer" && stepSet.has(target) && !reachable.has(target)) {
        reachable.add(target);
        queue.push(target);
      }
    }
  }

  return reachable;
}

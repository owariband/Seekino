import { GROUND, type Cursor, type PoseName } from "./engine";

/**
 * There is only one Kino in a conversation. When it leaves one perch (an older chat bubble) and a new
 * perch mounts in the same commit, the new one takes the old position and Kino leaps across.
 * Positions are in the stage's scroll-content coordinates, so scrolling between the two does not matter.
 */
export interface Perch {
  stage: Element;
  /** The Kino that stood here; a perch only counts once that Kino has really left the page. */
  svg: SVGSVGElement;
  /** Content point under Kino's feet. */
  x: number;
  y: number;
  /** Pose to continue from at the old perch. */
  cursor: Pick<Cursor, "facing" | "lying">;
  at: number;
}

/** Marks the scroll container Kino can leap within. */
export const KINO_STAGE = "data-kino-stage";
/** A perch is only taken by a Kino that mounts right after the old one leaves. */
const HANDOFF_MS = 1_000;

let vacated: Perch | null = null;

export function stageOf(element: Element): Element | null {
  return element.closest(`[${KINO_STAGE}]`);
}

/** Viewport point to the stage's scroll-content coordinates. */
export function contentPoint(stage: Element, clientX: number, clientY: number): { x: number; y: number } {
  const rect = stage.getBoundingClientRect();
  return { x: clientX - rect.left + stage.scrollLeft, y: clientY - rect.top + stage.scrollTop };
}

/** Records where a rendered Kino stands; call it while the SVG is still in the document. */
export function vacatePerch(svg: SVGSVGElement | null): void {
  if (!svg?.isConnected) return;
  const stage = stageOf(svg);
  const viewWidth = Number(svg.getAttribute("viewBox")?.split(" ")[2]);
  const x = Number(svg.dataset.x);
  if (!stage || !viewWidth || !Number.isFinite(x)) return;
  const rect = svg.getBoundingClientRect();
  const scale = rect.width / viewWidth;
  const pose = svg.dataset.pose as PoseName | undefined;
  const facing = pose === "front" || pose === "center" ? "front" : svg.dataset.flip === "true" ? "right" : "left";
  vacated = {
    stage,
    svg,
    ...contentPoint(stage, rect.left + x * scale, rect.top + GROUND * scale),
    cursor: { facing, lying: !!pose?.startsWith("sleep") },
    at: performance.now(),
  };
}

/** Takes the perch Kino just left on this stage, if any. */
export function takePerch(stage: Element | null): Perch | null {
  const perch = vacated;
  vacated = null;
  if (!perch || !stage || perch.stage !== stage || performance.now() - perch.at > HANDOFF_MS) return null;
  // StrictMode's rehearsal unmount also records a perch, but that Kino is still on the page.
  if (perch.svg.isConnected) return null;
  return perch;
}

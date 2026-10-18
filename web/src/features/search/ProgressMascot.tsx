import { useLayoutEffect, useRef, useState } from "react";

import type { AnswerPhase } from "../../api/contracts";
import { Kino, type KinoReaction, type KinoState } from "../../shared/kino/Kino";
import { GROUND, MIN_WIDTH, bounds, type Cursor } from "../../shared/kino/engine";
import { contentPoint, stageOf, takePerch } from "../../shared/kino/handoff";
import styles from "./search.module.css";

const SCALE = 2;
/** What Kino does while the assistant works through each phase. */
const PHASE_STATE: Record<AnswerPhase, KinoState> = {
  QUEUED: "wander",
  RETRIEVING: "run",
  EVALUATING: "think",
  RESPONDING: "loading",
};

interface Arrival {
  start: Partial<Cursor>;
  leapFrom: { x: number; y: number } | null;
}

/**
 * Kino's perch on top of the latest assistant bubble. While the answer is live Kino acts out each phase;
 * once it settles, `farewell` plays and Kino falls asleep here until the next turn. A perch that mounts
 * for a new turn (`arriving`) takes over the spot Kino just left and Kino leaps down onto it.
 */
export function ProgressMascot({ phase, settled = false, awake = false, arriving = false, farewell = null, onFarewellDone }: {
  phase?: AnswerPhase | null;
  /** The answer has finished: Kino sleeps here. */
  settled?: boolean;
  /** Something is about to happen (a new question is on its way): a settled Kino wakes up and looks. */
  awake?: boolean;
  /** Mounted for a new turn: leap over from the previous perch if Kino just left one. */
  arriving?: boolean;
  farewell?: KinoReaction | null;
  onFarewellDone?: () => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | null>(null);
  const [arrival, setArrival] = useState<Arrival | null>(null);
  const arrivingRef = useRef(arriving);
  // StrictMode replays mount effects, but a perch can only be taken once: keep what the first run planned.
  const plannedRef = useRef<Arrival | null>(null);

  useLayoutEffect(() => {
    const track = trackRef.current;
    if (!track) return undefined;
    const measure = () => setWidth(Math.max(MIN_WIDTH, Math.floor(track.clientWidth / SCALE)));
    measure();
    plannedRef.current ??= planArrival(track, arrivingRef.current);
    setArrival(plannedRef.current);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(track);
    return () => observer?.disconnect();
  }, []);

  const state: KinoState = settled ? (awake ? "look" : "sleep") : phase ? PHASE_STATE[phase] : "wander";
  const reaction = farewell ? { name: farewell, key: `farewell:${farewell}` }
    : arrival?.leapFrom ? { name: "leap" as const, key: "leap" } : null;

  return <div className={styles.progressWalkway} ref={trackRef} aria-hidden="true" data-pixel-mascot="">
    {width !== null && arrival ? (
      <Kino state={state} width={width} scale={SCALE} ground={false} perch start={arrival.start}
        leapFrom={arrival.leapFrom} reaction={reaction}
        onReactionEnd={(name) => {
          if (name === "leap") setArrival({ start: {}, leapFrom: null });
          else onFarewellDone?.();
        }} />
    ) : null}
  </div>;
}

/** Takes the perch Kino just left, if this bubble arrived for a new turn, and picks where to land. */
function planArrival(track: HTMLElement, arriving: boolean): Arrival {
  const stage = stageOf(track);
  const perch = arriving ? takePerch(stage) : null;
  if (!perch || !stage) return { start: {}, leapFrom: null };
  // Land somewhere along the new bubble, on the even grid the walk cycle uses.
  const { left, right } = bounds(Math.max(MIN_WIDTH, Math.floor(track.clientWidth / SCALE)));
  const x = left + 2 * Math.floor(Math.random() * ((right - left) / 2 + 1));
  const rect = track.getBoundingClientRect();
  const landing = contentPoint(stage, rect.left + x * SCALE, rect.top + GROUND * SCALE);
  return { start: { x, ...perch.cursor }, leapFrom: { x: perch.x - landing.x, y: perch.y - landing.y } };
}

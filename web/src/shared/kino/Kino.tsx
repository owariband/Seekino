import clsx from "clsx";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  COLOURS, GROUND, MascotPlayer, MIN_WIDTH, PALETTE, SCENE_H, STATE_FPS,
  groundPath, isAirborne, rasterize, staticFrame, toPaths, type Cursor, type Frame, type MascotState, type Reaction,
} from "./engine";
import { vacatePerch } from "./handoff";
import styles from "./kino.module.css";

/**
 * Kino, the Seekino pixel whale shark.
 * idle: faces the screen, breathes, blinks and glances — resting chat.
 * wander: walks to each end, faces the screen, heads back — idle pages.
 * look: turns to the screen, glances and hops — someone is paying attention.
 * think: looks up at a question mark — evaluating, or nothing found.
 * loading: walks in place with three dots — composing an answer.
 * run: sprints between the ends and skids — searching in a hurry.
 * sleep: lies down and floats Zs — nobody around.
 * jump / happy / oops: one-shot reactions (a big jump, a cheer, a startle); see `reaction`.
 * leap: one-shot jump from another perch to this one; see `leapFrom`.
 * Switching state continues from the current pose: it turns, lands or stands up first.
 */
export type KinoState = MascotState;
export type KinoReaction = Reaction;

/** Poking Kino cycles these; every fifth poke in a row startles it. */
const POKE_REACTIONS: readonly KinoReaction[] = ["jump", "happy", "jump", "happy"];
const POKE_STREAK_MS = 2_000;
/** Hovering an interactive Kino makes it turn and look at you, unless it is busy. */
const LOOKS_WHEN_HOVERED: ReadonlySet<KinoState> = new Set(["idle", "wander"]);
/** Leap physics in CSS pixels: a snappy gravity, and how far Kino springs above the higher perch. */
const LEAP_GRAVITY = 4200;
const LEAP_APEX = 22;

export function Kino({
  state = "idle", width = 64, scale = 3, ground = true, title, className, delayed = false,
  reaction, onReactionEnd, interactive = false, dozeAfterMs, start, leapFrom, perch = false,
}: {
  state?: KinoState;
  /** Scene width in sprite pixels; Kino paces inside it. */
  width?: number;
  /** CSS pixels per sprite pixel. */
  scale?: number;
  /** Dashed ground line and contact shadow. */
  ground?: boolean;
  /** Omit for decorative use next to visible text; interactive Kino falls back to "Kino". */
  title?: string;
  className?: string;
  /** Fade in after 300ms so fast loads never flash Kino. */
  delayed?: boolean;
  /** Plays once whenever `key` changes, then returns to `state`. */
  reaction?: { name: KinoReaction; key: string | number } | null;
  onReactionEnd?: (name: KinoReaction) => void;
  /** Kino becomes a button: hover to get a look, click or tap to make it react. */
  interactive?: boolean;
  /** While resting in `idle`, doze off after this long without pointer or keyboard activity. */
  dozeAfterMs?: number;
  /** Where Kino starts in the scene and the pose it continues from; read once on mount. */
  start?: Partial<Cursor>;
  /**
   * CSS-pixel offset from Kino's feet here to the perch it is leaping from. While a `leap` reaction plays,
   * Kino crouches at that perch, then falls along a ballistic arc to its own position at 60 fps.
   */
  leapFrom?: { x: number; y: number } | null;
  /** Leave this position behind on unmount so the next Kino in the same stage can leap from it. */
  perch?: boolean;
}) {
  const sceneWidth = Math.max(MIN_WIDTH, Math.round(width));
  const reducedMotion = useReducedMotion();
  // A reaction handed over on mount plays from the very first frame, so a leap never flashes at its landing spot.
  const [playing, setPlaying] = useState<KinoReaction | null>(() => reaction && !reducedMotion ? reaction.name : null);
  const [hovered, setHovered] = useState(false);
  const dozing = useDozing(dozeAfterMs, state === "idle" && !playing && !hovered && !reducedMotion);
  const current: KinoState = playing ?? (interactive && hovered && LOOKS_WHEN_HOVERED.has(state) ? "look" : dozing ? "sleep" : state);

  const onEndRef = useRef(onReactionEnd);
  onEndRef.current = onReactionEnd;
  const fromPropRef = useRef(false);
  const pokes = useRef({ count: 0, at: 0 });

  const play = (name: KinoReaction, fromProp: boolean) => {
    if (reducedMotion) {
      if (fromProp) onEndRef.current?.(name);
      return;
    }
    fromPropRef.current = fromProp;
    setPlaying(name);
  };

  const reactionKey = reaction?.key;
  useEffect(() => {
    if (reaction) play(reaction.name, true);
    // Replays only when the caller hands over a new key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reactionKey]);

  const startRef = useRef(start);
  const playerRef = useRef<MascotPlayer | null>(null);
  const [frame, setFrame] = useState<Frame>(() => staticFrame(current, sceneWidth, start));
  const svgRef = useRef<SVGSVGElement>(null);
  /** The running flight; while set, the airborne pose is held. */
  const flightRef = useRef<{ frame: number; safety: number } | null>(null);

  useLayoutEffect(() => {
    if (!perch) return undefined;
    const svg = svgRef.current;
    return () => vacatePerch(svg);
  }, [perch]);

  useEffect(() => {
    if (reducedMotion) {
      playerRef.current = null;
      setFrame(staticFrame(current, sceneWidth, startRef.current));
      return undefined;
    }
    const player = playerRef.current ??= new MascotPlayer(current, sceneWidth, startRef.current);
    player.set(current, sceneWidth);
    setFrame(player.next());
    // Hidden tabs pause instead of piling up frames.
    const timer = window.setInterval(() => {
      if (document.hidden || flightRef.current !== null) return;
      setFrame(player.next());
      if (playing && player.loopsCompleted >= 1) {
        window.clearInterval(timer);
        setPlaying(null);
        if (fromPropRef.current) onEndRef.current?.(playing);
      }
    }, 1000 / STATE_FPS[current]);
    return () => window.clearInterval(timer);
  }, [current, sceneWidth, reducedMotion, playing]);

  const raster = useMemo(() => rasterize(frame, sceneWidth), [frame, sceneWidth]);
  const paths = useMemo(() => toPaths(raster), [raster]);
  const ink = useMemo(() => groundPath(sceneWidth, raster.groundShift), [sceneWidth, raster.groundShift]);
  const label = title ?? (interactive ? "Kino" : undefined);
  const leaping = playing === "leap" && !!leapFrom && (frame.leap ?? 0) < 1;

  // The sprite is moved directly on the SVG so the flight runs at display rate, between the 15 fps pose frames.
  useLayoutEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    if (!leaping || !leapFrom) {
      svg.style.transform = "";
      return;
    }
    if (!isAirborne(frame)) {
      svg.style.transform = translate(leapFrom.x, leapFrom.y);
      return;
    }
    if (flightRef.current !== null) return;
    const flight = ballistic(leapFrom);
    const land = () => {
      if (flightRef.current) {
        cancelAnimationFrame(flightRef.current.frame);
        window.clearTimeout(flightRef.current.safety);
      }
      flightRef.current = null;
      svg.style.transform = "";
      // Touch down on the very next pose frame instead of waiting for the frame timer.
      if (playerRef.current) setFrame(playerRef.current.next());
    };
    // Hidden pages get no animation frames: skip straight to the landing rather than hanging in the air.
    if (document.hidden) {
      land();
      return;
    }
    let startedAt: number | null = null;
    const step = (now: number) => {
      startedAt ??= now;
      const t = (now - startedAt) / 1000;
      if (t >= flight.duration) return land();
      const at = flight.at(t);
      svg.style.transform = translate(at.x, at.y);
      if (flightRef.current) flightRef.current.frame = requestAnimationFrame(step);
    };
    flightRef.current = {
      frame: requestAnimationFrame(step),
      // If animation frames stop (the tab is hidden mid-flight), land anyway.
      safety: window.setTimeout(land, flight.duration * 1000 + 250),
    };
  }, [frame, leaping, leapFrom]);

  useEffect(() => () => {
    if (!flightRef.current) return;
    cancelAnimationFrame(flightRef.current.frame);
    window.clearTimeout(flightRef.current.safety);
    flightRef.current = null;
  }, []);

  const svg = (
    <svg ref={svgRef} className={clsx(styles.kino, !interactive && className)} data-kino="" data-kino-state={current} data-pose={frame.pose}
      data-x={frame.x} data-flip={frame.flip} data-delayed={delayed || undefined} data-leaping={leaping ? "" : undefined}
      viewBox={`0 0 ${sceneWidth} ${SCENE_H}`} width={sceneWidth * scale} height={SCENE_H * scale}
      shapeRendering="crispEdges" role={label && !interactive ? "img" : undefined} aria-label={interactive ? undefined : label}
      aria-hidden={label && !interactive ? undefined : true} focusable="false">
      {ground ? <g>
        <path d={ink} fill="var(--pixel-mascot-ground, currentColor)" opacity="var(--pixel-mascot-ground-opacity, 0.18)" />
        {raster.shadow ? <path fill="var(--pixel-mascot-shadow, #0b3b33)" opacity="var(--pixel-mascot-shadow-opacity, 0.3)"
          d={shadowPath(raster.shadow.x, raster.shadow.width)} /> : null}
      </g> : null}
      {COLOURS.map(c => paths[c] ? <path key={c} d={paths[c]} fill={`var(--pixel-mascot-${c}, ${PALETTE[c]})`} /> : null)}
    </svg>
  );
  if (!interactive) return svg;

  return (
    <button type="button" className={clsx(styles.poke, className)} aria-label={label}
      onPointerEnter={() => setHovered(true)} onPointerLeave={() => setHovered(false)}
      onFocus={() => setHovered(true)} onBlur={() => setHovered(false)}
      onClick={() => {
        const now = Date.now(), streak = pokes.current;
        streak.count = now - streak.at < POKE_STREAK_MS ? streak.count + 1 : 0;
        streak.at = now;
        play(streak.count % 5 === 4 ? "oops" : POKE_REACTIONS[streak.count % POKE_REACTIONS.length]!, false);
      }}>
      {svg}
    </button>
  );
}

const translate = (x: number, y: number) => `translate(${Math.round(x)}px, ${Math.round(y)}px)`;

/**
 * A jump under gravity from `from` (offset of the old perch) to (0, 0): Kino springs a little above the
 * higher perch and accelerates as it falls, so longer drops take longer but never feel floaty.
 */
function ballistic(from: { x: number; y: number }): { duration: number; at: (t: number) => { x: number; y: number } } {
  const g = LEAP_GRAVITY, drop = -from.y;
  const rise = Math.max(0, -drop) + LEAP_APEX;
  const vy = -Math.sqrt(2 * g * rise);
  const duration = (-vy + Math.sqrt(vy * vy + 2 * g * drop)) / g;
  return { duration, at: t => ({ x: from.x * (1 - t / duration), y: from.y + vy * t + g * t * t / 2 }) };
}

/** Two-row contact shadow: full width on the ground row, two pixels narrower each side below it. */
function shadowPath(x: number, width: number): string {
  return `M${x} ${GROUND}h${width}v1h${-width}z` + `M${x + 2} ${GROUND + 1}h${width - 4}v1h${4 - width}z`;
}

const ACTIVITY_EVENTS = ["pointerdown", "pointermove", "keydown", "wheel"] as const;

/** True once nothing has happened on the page for `afterMs`; any pointer or key activity wakes it again. */
function useDozing(afterMs: number | undefined, enabled: boolean): boolean {
  const [dozing, setDozing] = useState(false);
  useEffect(() => {
    if (!afterMs || !enabled) {
      setDozing(false);
      return undefined;
    }
    let timer = window.setTimeout(() => setDozing(true), afterMs);
    const wake = () => {
      setDozing(false);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setDozing(true), afterMs);
    };
    ACTIVITY_EVENTS.forEach(type => window.addEventListener(type, wake, { passive: true }));
    return () => {
      window.clearTimeout(timer);
      ACTIVITY_EVENTS.forEach(type => window.removeEventListener(type, wake));
    };
  }, [afterMs, enabled]);
  return dozing;
}

function useReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(() => typeof window !== "undefined" && !!window.matchMedia?.(query).matches);
  useEffect(() => {
    const media = window.matchMedia?.(query);
    if (!media) return undefined;
    const sync = () => setReduced(media.matches);
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);
  return reduced;
}

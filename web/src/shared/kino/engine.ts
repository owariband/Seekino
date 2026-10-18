/**
 * Kino, the Seekino pixel whale shark (direction A): pure sprite data, timelines and rasterising.
 * Nothing here touches the DOM, so the React component, the gallery and the video
 * export all draw from the same frames. Ported from docs/ui-design/seekino-mascot-pixel/pixel-mascot.
 */

export const PALETTE = {
  ink: "#0b3b33",
  body: "#74c5ba",
  shade: "#349489",
  belly: "#fcfaf1",
} as const;
export type Colour = keyof typeof PALETTE;
export const COLOURS = Object.keys(PALETTE) as Colour[];

export type MascotState = "idle" | "wander" | "look" | "jump" | "leap" | "happy" | "think" | "oops" | "loading" | "run" | "sleep";
export const STATES: readonly MascotState[] = ["idle", "wander", "look", "jump", "leap", "happy", "think", "oops", "loading", "run", "sleep"];
/** Playback rate per state; jumping and running are quicker, idling and sleeping breathe slowly. All divide 30 so video frames stay even. */
export const STATE_FPS: Record<MascotState, number> = {
  idle: 6, wander: 10, look: 10, jump: 15, leap: 15, happy: 15, think: 6, oops: 10, loading: 10, run: 15, sleep: 6,
};
export type Facing = "left" | "right" | "front";
export type PoseName = "side" | "run" | "q34" | "front" | "center" | "sleep" | "sleepIn";

type Leg = readonly [x: number, colour: Colour, gaitOffset: number];
interface Pose {
  /**
   * b body · s shade · w belly · e ink · o eye (blinks and can glance ±1px). All poses face left.
   * The two short shade strokes behind the eye are the gill slits; the one-frame centre pose has none.
   */
  rows: readonly string[];
  /** Far legs first so the near legs overlap them. */
  legs: readonly Leg[];
  /** Column under the feet; a frame's x is where this column lands in the scene. */
  anchor: number;
  shadow: number;
  /** Lying poses rest this many rows lower and hide their legs. */
  sink: number;
}

const SIDE_LEGS: readonly Leg[] = [[5, "shade", 2], [14, "shade", 0], [2, "belly", 0], [11, "belly", 2]];

export const POSES: Record<PoseName, Pose> = {
  side: {
    rows: [
      "..........b............",
      ".........bb............",
      "....bbbbbbbbb........bb",
      "..bbbbbbbbbbbbbb....bbb",
      ".bbbbbbsbbbbbbbbbs.bbb.",
      "bbobbbbsbsbbbbbbbssbb..",
      "bbbbbbbsbsbbbbbbbssb...",
      "eeeeewwbbbbbbbbbbssbb..",
      "wwwwwwwwwwwbbbbbss..bb.",
      ".wwwwwwwwswwwwwss....bb",
      "..wwwwwwwwsswwss.......",
    ],
    legs: SIDE_LEGS, anchor: 8, shadow: 14, sink: 0,
  },
  // Leaning into the sprint: head and back pushed forward, tail swept flat behind.
  run: {
    rows: [
      ".........b.............",
      "........bb.............",
      "...bbbbbbbbb..........b",
      ".bbbbbbbbbbbbbb......bb",
      "bbbbbbsbbbbbbbbbs...bbb",
      "bobbbbsbsbbbbbbbssbbbb.",
      "bbbbbbsbsbbbbbbbsssbb..",
      "eeeeewwbbbbbbbbbbsssbbb",
      "wwwwwwwwwwwbbbbbss...bb",
      ".wwwwwwwwswwwwwss......",
      "..wwwwwwwwsswwss.......",
    ],
    legs: SIDE_LEGS, anchor: 8, shadow: 14, sink: 0,
  },
  q34: {
    rows: [
      "..........b.........",
      ".........bb......bb.",
      "....bbbbbbbbb...bbb.",
      "..bbbbbbbbbbbbb.bbb.",
      ".bbbbbbbbbbsbbbbbb..",
      "bbobbbbbbobsbsbbbs..",
      "bbbbbbbbbbbsbsbbss..",
      "eeeeeeeewwwwbbbbssb.",
      "wwwwwwwwwwwwwbbss..b",
      ".wwwwwwwwwwwwwss....",
      "..wwwwwwwwwwwss.....",
    ],
    legs: [[6, "shade", 2], [12, "shade", 0], [3, "belly", 0], [10, "belly", 2]], anchor: 8, shadow: 13, sink: 0,
  },
  // Same reading as assets/mascot/seekino-logo.svg: both eyes, a straight mouth, dorsal fin and tail top-right.
  front: {
    rows: [
      "...............b..",
      "..............bb..",
      ".........b...bbb..",
      "........bb..bbbb..",
      "....bbbbbbbbbbbb..",
      "..bbbbbbbbbbbsbbb.",
      ".bbbobbbbbbbbsbss.",
      ".bbwwwwwwwwobsbss.",
      "bwwwwwwwwwwwwbbss.",
      "bwwweeeeeeeewwbssb",
      ".wwwwwwwwwwwwwbss.",
      ".wwwwwwwwwwwwwss..",
      "..wwwwwwwwwwwss...",
    ],
    legs: [[4, "belly", 0], [10, "belly", 2]], anchor: 8, shadow: 12, sink: 0,
  },
  // Dead-centre in-between: tail hidden behind the body, one frame while turning.
  center: {
    rows: [
      "........b........",
      ".......bbb.......",
      "...bbbbbbbbbbb...",
      "..bbbbbbbbbbbbb..",
      ".bbbbbbbbbbbbbbs.",
      ".bbbobbbbbbbobbs.",
      ".bbwwwwwwwwwwwbs.",
      "bwwwwwwwwwwwwwwsb",
      "bwwweeeeeeeeewwsb",
      ".wwwwwwwwwwwwwws.",
      "..wwwwwwwwwwwws..",
    ],
    legs: [[4, "belly", 0], [11, "belly", 2]], anchor: 8, shadow: 12, sink: 0,
  },
  // Loaf on the ground: eye shut into a dash, tail drooped.
  sleep: {
    rows: [
      "..........b............",
      ".........bb............",
      "....bbbbbbbbb..........",
      "..bbbbbbbbbbbbbb.......",
      ".bbbbbbsbbbbbbbbbs.....",
      "bbbbbbbsbsbbbbbbbssb...",
      "beebbbbsbsbbbbbbbssbb..",
      "eeeeewwbbbbbbbbbbssbbb.",
      "wwwwwwwwwwwbbbbbss.bbbb",
      ".wwwwwwwwswwwwwss...bbb",
      "..wwwwwwwwsswwss.......",
    ],
    legs: [], anchor: 8, shadow: 18, sink: 2,
  },
  sleepIn: { rows: [], legs: [], anchor: 8, shadow: 18, sink: 2 },
};
// Breathing in: the back rises one row while the belly stays on the ground.
POSES.sleepIn.rows = [...POSES.sleep.rows.slice(0, 5), POSES.sleep.rows[4]!, ...POSES.sleep.rows.slice(5)];

const CHAR_COLOUR: Record<string, Colour> = { b: "body", s: "shade", w: "belly", e: "ink" };

/** Scene geometry in sprite pixels. The ground line sits on row GROUND; effects use the rows above the head. */
export const SCENE_H = 28;
export const GROUND = 25;
const SPRITE_H = 16;
const BODY_BOTTOM = 13;
const FOOT_ROW = 15;
/** Keeps every pose inside the scene while it turns at either end. */
export const EDGE = 15;
export const MIN_WIDTH = 2 * EDGE + 8;

export type Cell = readonly [x: number, y: number];
export interface Effect { colour: Colour; cells: readonly Cell[] }

export interface Frame {
  state: MascotState;
  pose: PoseName;
  /** true when facing right; sprites are drawn facing left and mirrored. */
  flip: boolean;
  x: number;
  /** Walk-cycle index; omitted while standing still. */
  gait?: number;
  stride?: 1 | 2;
  /** Body only, negative is up. */
  bob?: number;
  /** Whole sprite including legs, positive is up. */
  lift?: number;
  /** Extra rows lowered while lying down or getting up. */
  sink?: number;
  look?: -1 | 0 | 1;
  blink?: boolean;
  groundShift?: number;
  fx?: readonly Effect[];
  /** Marks the frame that starts a sound-worthy beat, for the video export. */
  cue?: Cue;
  /**
   * Flight progress from the old perch (0) to the new one (1) while leaping. The engine only poses Kino;
   * whoever renders it moves the whole sprite between the two perches.
   */
  leap?: number;
}
export type Cue = "turn" | "hop" | "blink" | "dot" | "dash" | "skid" | "lie" | "z" | "wake" | "step" | "land" | "sparkle" | "startle";

/**
 * Foot offset for frame f of a 4-frame cycle. Half the cycle is stance: the foot slides back
 * exactly `stride` px per frame while the body moves forward by the same amount, so it stays planted.
 */
export function gaitOffset(f: number, stride: number, lift: number, offset: number): { dx: number; dy: number } {
  const half = 2, reach = half * stride / 2, l = (f + offset) % 4;
  if (l <= half) return { dx: Math.round(-reach + l * stride), dy: 0 };
  const j = l - half;
  return { dx: Math.round(reach - j * stride), dy: -Math.round(lift * Math.sin(Math.PI * j / half)) };
}

export const poseWidth = (pose: PoseName): number => POSES[pose].rows[0]!.length;

/** Scene column for a sprite-local column, honouring the mirror. */
export function sceneX(frame: Pick<Frame, "pose" | "flip" | "x">, local: number): number {
  const p = POSES[frame.pose], w = poseWidth(frame.pose);
  return frame.flip ? frame.x - (w - 1 - p.anchor) + (w - 1 - local) : frame.x - p.anchor + local;
}

/** Horizontal extent [left, right] of the sprite in scene columns. */
export function extent(frame: Pick<Frame, "pose" | "flip" | "x">): [number, number] {
  const a = sceneX(frame, 0), b = sceneX(frame, poseWidth(frame.pose) - 1);
  return [Math.min(a, b), Math.max(a, b)];
}

export interface Raster {
  width: number;
  height: number;
  cells: (Colour | null)[];
  /** Two-row contact shadow on the ground, or null while lifted out of contact. */
  shadow: { x: number; width: number } | null;
  groundShift: number;
}

export function rasterize(frame: Frame, width: number): Raster {
  const cells: (Colour | null)[] = new Array(width * SCENE_H).fill(null);
  const set = (x: number, y: number, c: Colour) => {
    if (x >= 0 && x < width && y >= 0 && y < SCENE_H) cells[y * width + x] = c;
  };
  const p = POSES[frame.pose], w = poseWidth(frame.pose);
  const lift = frame.lift ?? 0, bob = frame.bob ?? 0, sink = p.sink + (frame.sink ?? 0);
  const y0 = GROUND - SPRITE_H - lift;
  const at = (local: number) => sceneX(frame, local);

  for (const [lx, colour, offset] of p.legs) {
    const g = frame.gait == null ? { dx: 0, dy: 0 } : gaitOffset(frame.gait, frame.stride ?? 1, frame.stride === 2 ? 2 : 1, offset);
    // Legs start at the body's resting bottom row, so a bobbing body never leaves a gap above them.
    const top = BODY_BOTTOM + sink, bottom = FOOT_ROW + g.dy;
    for (let y = top; y <= bottom; y++) for (let i = 0; i < 2; i++) set(at(lx + g.dx + i), y0 + y, colour);
  }
  const top = y0 + BODY_BOTTOM - (p.rows.length - 1) + bob + sink;
  p.rows.forEach((row, y) => {
    for (let x = 0; x < w; x++) {
      const ch = row[x]!;
      if (ch === ".") continue;
      if (ch === "o") {
        set(at(x), top + y, "body");
        if (!frame.blink) set(at(x + (frame.flip ? -1 : 1) * (frame.look ?? 0)), top + y, "ink");
        continue;
      }
      set(at(x), top + y, CHAR_COLOUR[ch]!);
    }
  });
  for (const fx of frame.fx ?? []) for (const [x, y] of fx.cells) set(x, y, fx.colour);

  const shadowWidth = p.shadow - 2 * lift;
  const centre = Math.round((extent(frame)[0] + extent(frame)[1]) / 2);
  return {
    width, height: SCENE_H, cells,
    shadow: shadowWidth >= 6 ? { x: centre - Math.floor(shadowWidth / 2), width: shadowWidth } : null,
    groundShift: frame.groundShift ?? 0,
  };
}

/** One SVG path per colour, merging horizontal runs so a frame costs a handful of nodes. */
export function toPaths(raster: Raster): Partial<Record<Colour, string>> {
  const out: Partial<Record<Colour, string>> = {};
  for (let y = 0; y < raster.height; y++) {
    let x = 0;
    while (x < raster.width) {
      const c = raster.cells[y * raster.width + x];
      if (!c) { x++; continue; }
      let end = x + 1;
      while (end < raster.width && raster.cells[y * raster.width + end] === c) end++;
      out[c] = `${out[c] ?? ""}M${x} ${y}h${end - x}v1h${x - end}z`;
      x = end;
    }
  }
  return out;
}

/** Ground dashes, 2 on / 2 off, scrolled by `shift`. */
export function groundPath(width: number, shift: number): string {
  let d = "";
  for (let x = 0; x < width; x++) if ((((x - shift) % 4) + 4) % 4 < 2) d += `M${x} ${GROUND}h1v1h-1z`;
  return d;
}

// ——— Timelines ———

export interface Cursor { x: number; facing: Facing; lying: boolean; /** Mid-sprint: the next turn skids. */ running?: boolean }
export interface Timeline { intro: Frame[]; loop: Frame[] }

export function cursorOf(frame: Frame): Cursor {
  const lying = POSES[frame.pose].sink + (frame.sink ?? 0) > 0;
  const facing: Facing = frame.pose === "front" || frame.pose === "center" ? "front" : frame.flip ? "right" : "left";
  return { x: frame.x, facing, lying, running: frame.stride === 2 && frame.gait != null };
}

interface HoldBeats { length: number; left: readonly [number, number]; right: readonly [number, number]; blink: number; hop: number }
/** Facing the screen: glance left, glance right, blink, then a small hop. */
const HOLD_LONG: HoldBeats = { length: 40, left: [8, 14], right: [16, 22], blink: 27, hop: 32 };
const HOLD_SHORT: HoldBeats = { length: 20, left: [3, 7], right: [9, 13], blink: 14, hop: 16 };
const HOP_LIFT = [1, 2, 1];
const SLEEP_CYCLE = 24;
const LOADING_CYCLE = 12;
// The small Z is one row taller than wide so its diagonal reads as a Z, not an I.
const Z_SMALL = ["####", "...#", "..#.", ".#..", "####"];
const Z_LARGE = ["#####", "...#.", "..#..", ".#...", "#####"];
const QUESTION = [".##.", "#..#", "...#", "..#.", "....", "..#."];
const BANG = ["##", "##", "##", "..", "##"];
const DROP = [".#..", ".##.", "####", "####", ".##."];
/** Crouch, stretch, arc up seven rows and land; the shadow shrinks as the body lifts. */
const JUMP_ARC = [1, 3, 5, 6, 7, 7, 7, 6, 5, 3, 1];
const LEAP_FLIGHT = 10;
const IDLE_CYCLE = 36;
const THINK_CYCLE = 36;
const WORRY_CYCLE = 24;

class Builder {
  frames: Frame[] = [];
  state: MascotState;
  x: number;
  facing: Facing;
  lying: boolean;
  /** Set after a sprint, so the next quick turn is a skid rather than a standing start. */
  running = false;

  constructor(state: MascotState, from: Cursor) {
    this.state = state;
    this.x = from.x;
    this.facing = from.facing;
    this.lying = from.lying;
    this.running = from.running ?? false;
  }

  push(frame: Omit<Frame, "state" | "x"> & { x?: number }) {
    this.frames.push({ state: this.state, x: this.x, ...frame });
  }

  stand() {
    if (!this.lying) return;
    const flip = this.facing === "right";
    this.push({ pose: "side", flip, sink: 2, cue: "wake" });
    this.push({ pose: "side", flip, sink: 1 });
    this.lying = false;
  }

  lie() {
    if (this.lying) return;
    if (this.facing === "front") this.turn("left");
    const flip = this.facing === "right";
    this.push({ pose: "side", flip, sink: 1, cue: "lie" });
    this.push({ pose: "side", flip, sink: 2, blink: true });
    this.lying = true;
  }

  /** Turns pivot on the feet; `quick` is the one-frame-per-pose skid used while running. */
  turn(to: Facing, quick = false) {
    const from = this.facing;
    if (from === to) return;
    const n = quick ? 1 : 3;
    const q34L = { pose: "q34", flip: false } as const, q34R = { pose: "q34", flip: true } as const;
    const front = { pose: "front", flip: false } as const, mirror = { pose: "front", flip: true } as const;
    const center = { pose: "center", flip: false } as const;
    const rep = <T>(f: T, k: number) => Array.from({ length: k }, () => f);
    let seq: { pose: PoseName; flip: boolean }[];
    if (from === "left" && to === "front") seq = rep(q34L, n);
    else if (from === "front" && to === "left") seq = rep(q34L, n);
    else if (from === "front" && to === "right") seq = [center, mirror, ...rep(q34R, n)];
    else if (from === "right" && to === "front") seq = [...rep(q34R, n), mirror, center];
    else if (from === "left") seq = quick ? [q34L, center, q34R] : [q34L, q34L, front, center, mirror, q34R, q34R];
    else seq = quick ? [q34R, center, q34L] : [q34R, q34R, mirror, center, front, q34L, q34L];
    // Any turn straight out of a sprint skids; starting a run from a standstill does not.
    const skid = this.running;
    this.running = false;
    seq.forEach((f, i) => this.push({
      ...f, cue: i === 0 ? (skid ? "skid" : "turn") : undefined,
      fx: skid ? skidDust(this.x, from === "left" ? -1 : 1, i) : undefined,
    }));
    this.facing = to;
  }

  /** Walks (stride 1) or runs (stride 2) to `toX`; the supporting foot never slides. */
  move(toX: number, stride: 1 | 2) {
    if (toX === this.x) return;
    const dir = Math.sign(toX - this.x);
    this.turn(dir < 0 ? "left" : "right", stride === 2);
    const flip = dir > 0, pose: PoseName = stride === 2 ? "run" : "side";
    const puffs: { x: number; at: number }[] = [];
    for (let i = 0; this.x !== toX; i++) {
      this.x += dir * Math.min(stride, Math.abs(toX - this.x));
      const f = i % 4;
      if (stride === 2 && f % 2 === 0) puffs.push({ x: this.x - dir * 8, at: i });
      const fx = stride === 2 ? puffs.filter(p => i - p.at <= 2).map(p => puff(p.x, -dir, i - p.at)) : undefined;
      // Running has a flight phase: the whole body leaves the ground on odd frames.
      this.push({
        pose, flip, gait: f, stride, fx,
        bob: stride === 1 && f % 2 ? -1 : 0, lift: stride === 2 && f % 2 ? 1 : 0,
        blink: stride === 1 && i % 40 === 20,
        cue: i === 0 && stride === 2 ? "dash" : f % 2 === 0 ? "step" : undefined,
      });
    }
    this.running = stride === 2;
  }

  hold(beats: HoldBeats) {
    const within = (i: number, [a, b]: readonly [number, number]) => i >= a && i < b;
    for (let i = 0; i < beats.length; i++) {
      const look = within(i, beats.left) ? -1 : within(i, beats.right) ? 1 : 0;
      this.push({
        pose: "front", flip: false, look, lift: HOP_LIFT[i - beats.hop] ?? 0, blink: i === beats.blink,
        cue: i === beats.blink ? "blink" : i === beats.hop ? "hop" : undefined,
      });
    }
  }

  loading() {
    const flip = this.facing === "right", dir = flip ? 1 : -1;
    const probe = { pose: "side" as const, flip, x: this.x };
    // Dots light up left to right whichever way the mascot faces.
    const dots = [7, 10, 13].map(c => sceneX(probe, c)).sort((a, b) => a - b);
    for (let i = 0; i < LOADING_CYCLE; i++) {
      const f = i % 4, lit = Math.floor(i / 4);
      const fx = dots.map((x, k): Effect => k === lit
        ? { colour: "body", cells: [[x, 8]] }
        : { colour: "shade", cells: [[x, 9]] });
      this.push({
        pose: "side", flip, gait: f, stride: 1, bob: f % 2 ? -1 : 0, groundShift: -dir * i, fx,
        cue: i % 4 === 0 ? "dot" : undefined,
      });
    }
  }

  sleep() {
    const flip = this.facing === "right";
    const probe = { pose: "sleep" as const, flip, x: this.x };
    for (let i = 0; i < SLEEP_CYCLE; i++) {
      const fx = [zGlyph(probe, i, 0), zGlyph(probe, i, SLEEP_CYCLE / 2)].filter((e): e is Effect => e !== null);
      this.push({ pose: i < SLEEP_CYCLE / 2 ? "sleep" : "sleepIn", flip, fx, cue: i % (SLEEP_CYCLE / 2) === 0 ? "z" : undefined });
    }
  }

  /** Facing the screen at rest: slow breathing, a blink, then a glance each way. */
  idle() {
    for (let i = 0; i < IDLE_CYCLE; i++) {
      const look = i >= 20 && i < 24 ? -1 : i >= 26 && i < 30 ? 1 : 0;
      this.push({ pose: "front", flip: false, bob: i % 12 >= 6 ? -1 : 0, look, blink: i === 14, cue: i === 14 ? "blink" : undefined });
    }
  }

  /** Crouch, stretch, a full arc, a dusty landing, then a short rest before the next jump. */
  jump() {
    const x = this.x;
    for (let i = 0; i < 3; i++) this.push({ pose: "front", flip: false, bob: 1, cue: i === 0 ? "hop" : undefined });
    JUMP_ARC.forEach((lift, i) => this.push({
      pose: "front", flip: false, lift, bob: i < 2 || i >= JUMP_ARC.length - 2 ? -1 : 0,
    }));
    for (let age = 0; age < 3; age++) this.push({
      pose: "front", flip: false, bob: age < 2 ? 1 : 0, cue: age === 0 ? "land" : undefined,
      fx: [puff(x - 9, -1, age), puff(x + 10, 1, age)],
    });
    for (let i = 0; i < 12; i++) this.push({ pose: "front", flip: false, blink: i === 6 });
  }

  /** Crouch, fly to the new perch while the renderer carries the sprite, land with dust, and catch a breath. */
  leap() {
    const x = this.x;
    for (let i = 0; i < 3; i++) this.push({ pose: "front", flip: false, bob: 1, leap: 0, cue: i === 0 ? "hop" : undefined });
    for (let i = 0; i < LEAP_FLIGHT; i++) this.push({
      pose: "front", flip: false, bob: i < 2 || i >= LEAP_FLIGHT - 2 ? -1 : 0, leap: (i + 1) / (LEAP_FLIGHT + 1),
    });
    for (let age = 0; age < 3; age++) this.push({
      pose: "front", flip: false, bob: age < 2 ? 1 : 0, leap: 1, cue: age === 0 ? "land" : undefined,
      fx: [puff(x - 9, -1, age), puff(x + 10, 1, age)],
    });
    for (let i = 0; i < 6; i++) this.push({ pose: "front", flip: false, leap: 1, blink: i === 3 });
  }

  /** Two bouncy hops with squinting eyes while sparkles twinkle on either side. */
  happy() {
    const x = this.x;
    const hop = [1, 2, 3, 3, 2, 1];
    let i = 0;
    const sparkle = (): Effect[] => {
      if (i >= 22) return [];
      const left = Math.floor(i / 4) % 2 === 0, big = i % 4 < 2;
      return [twinkle(left ? x - 11 : x + 12, left ? 5 : 3, big)];
    };
    const push = (frame: Omit<Frame, "state" | "x">) => { this.push({ ...frame, fx: sparkle() }); i++; };
    for (let round = 0; round < 2; round++) {
      for (let k = 0; k < 2; k++) push({ pose: "front", flip: false, bob: 1, cue: round === 0 && k === 0 ? "sparkle" : undefined });
      hop.forEach((lift, k) => push({ pose: "front", flip: false, lift, blink: lift >= 2, cue: k === 0 ? "hop" : undefined }));
    }
    push({ pose: "front", flip: false, bob: 1 });
    while (i < 30) push({ pose: "front", flip: false });
  }

  /** Looks up at a floating question mark, blinks, and looks back at it. */
  think() {
    const x = this.x;
    for (let i = 0; i < THINK_CYCLE; i++) {
      const look = i < 16 || i >= 28 ? -1 : 0;
      this.push({
        pose: "front", flip: false, look, blink: i === 20, cue: i === 20 ? "blink" : undefined,
        fx: [{ colour: "shade", cells: glyphCells(QUESTION, x - 12, 2 + (Math.floor(i / 6) % 2)) }],
      });
    }
  }

  /** A startled hop with an exclamation mark and a quick head shake. */
  startle() {
    const bang = (): Effect[] => [{ colour: "ink", cells: glyphCells(BANG, this.x + 11, 3) }];
    this.push({ pose: "front", flip: false, lift: 2, fx: bang(), cue: "startle" });
    this.push({ pose: "front", flip: false, lift: 1, fx: bang() });
    this.push({ pose: "front", flip: false, bob: 1, fx: bang() });
    for (const dx of [1, -1, 1, -1, 1, 0]) this.push({ pose: "front", flip: false, x: this.x + dx, fx: bang() });
  }

  /** After the startle: a double blink while a sweat drop slides down beside the head. */
  worry() {
    const x = this.x;
    for (let i = 0; i < WORRY_CYCLE; i++) {
      const fx: Effect[] = i < 16 ? [{ colour: "shade", cells: glyphCells(DROP, x - 12, 12 + Math.floor(i / 4)) }] : [];
      this.push({ pose: "front", flip: false, blink: i === 8 || i === 10, fx, cue: i === 8 ? "blink" : undefined });
    }
  }
}

function glyphCells(glyph: readonly string[], left: number, top: number): Cell[] {
  const cells: Cell[] = [];
  glyph.forEach((row, y) => [...row].forEach((ch, x) => { if (ch === "#") cells.push([left + x, top + y]); }));
  return cells;
}

/** A plus-shaped sparkle that shrinks to a single pixel between twinkles. */
function twinkle(cx: number, cy: number, big: boolean): Effect {
  return big
    ? { colour: "body", cells: [[cx, cy], [cx - 1, cy], [cx + 1, cy], [cx, cy - 1], [cx, cy + 1]] }
    : { colour: "shade", cells: [[cx, cy]] };
}

function puff(x: number, out: number, age: number): Effect {
  const g = GROUND - 1;
  const cells: Cell[][] = [
    [[x, g], [x + out, g]],
    [[x + out, g - 1], [x + 2 * out, g]],
    [[x + 2 * out, g - 2]],
  ];
  return { colour: "shade", cells: cells[age] ?? [] };
}

/** Dust kicks up ahead of the feet, in the direction the mascot was running. */
function skidDust(x: number, ahead: number, i: number): Effect[] {
  return [puff(x + ahead * 9, ahead, i), puff(x - ahead * 2, ahead, Math.max(0, i - 1))];
}

/** A Z that floats up from the head towards the tail for 14 frames, growing halfway and fading at the end. */
function zGlyph(probe: Pick<Frame, "pose" | "flip" | "x">, i: number, start: number): Effect | null {
  const age = (i - start + SLEEP_CYCLE) % SLEEP_CYCLE;
  if (age >= 14) return null;
  const glyph = age < 7 ? Z_SMALL : Z_LARGE, w = glyph[0]!.length, h = glyph.length;
  // Glyphs are never mirrored, so a right-facing sleeper still floats a readable Z.
  const head = sceneX(probe, 4), drift = Math.floor(age / 3);
  const left = probe.flip ? head - drift - (w - 1) : head + drift;
  const top = 12 - h - Math.floor(age / 2);
  const cells: Cell[] = [];
  glyph.forEach((row, y) => [...row].forEach((ch, x) => { if (ch === "#") cells.push([left + x, top + y]); }));
  return { colour: age < 10 ? "body" : "shade", cells };
}

export function bounds(width: number): { left: number; right: number } {
  const w = Math.max(width, MIN_WIDTH), left = EDGE;
  let right = w - EDGE;
  if ((right - left) % 2) right -= 1;
  return { left, right };
}

export function buildTimeline(state: MascotState, from: Cursor, width: number): Timeline {
  const { left, right } = bounds(width);
  const b = new Builder(state, from);
  const split = () => { const intro = b.frames; b.frames = []; return intro; };
  // Keep heading the way the mascot faces; from the front, take the longer side so the walk is visible.
  // Already standing on that end means heading back the other way.
  const firstEnd = (): "left" | "right" => {
    const ahead = b.facing === "front" ? (b.x - left >= right - b.x ? "left" : "right") : b.facing;
    const atEnd = ahead === "left" ? b.x <= left : b.x >= right;
    return atEnd ? (ahead === "left" ? "right" : "left") : ahead;
  };

  switch (state) {
    case "idle":
    case "jump":
    case "happy":
    case "think": {
      b.stand(); b.turn("front");
      const intro = split();
      b[state]();
      return { intro, loop: b.frames };
    }
    case "leap": {
      b.stand(); b.turn("front");
      // Getting up and turning still happen on the old perch.
      const intro = split().map(f => ({ ...f, leap: 0 }));
      b.leap();
      return { intro, loop: b.frames };
    }
    case "oops": {
      b.stand(); b.turn("front"); b.startle();
      const intro = split();
      b.worry();
      return { intro, loop: b.frames };
    }
    case "look": {
      b.stand(); b.turn("front");
      const intro = split();
      b.hold(HOLD_LONG);
      return { intro, loop: b.frames };
    }
    case "loading": {
      b.stand();
      if (b.facing === "front") b.turn("left");
      const intro = split();
      b.loading();
      return { intro, loop: b.frames };
    }
    case "sleep": {
      b.lie();
      const intro = split();
      b.sleep();
      return { intro, loop: b.frames };
    }
    case "wander":
    case "run": {
      const stride = state === "run" ? 2 : 1;
      b.stand();
      const first = firstEnd();
      const ends = { left, right };
      // Runs only cover even distances, so snap the starting column onto the stride grid.
      if (stride === 2 && (b.x - left) % 2) b.move(b.x + (b.x < right ? 1 : -1), 1);
      b.move(ends[first], stride);
      const intro = split();
      const other = first === "left" ? "right" : "left";
      for (const end of [other, first] as const) {
        if (stride === 1) { b.turn("front"); b.hold(HOLD_SHORT); }
        b.move(ends[end], stride);
      }
      return { intro, loop: b.frames };
    }
  }
}

/** States that are played once as a reaction and then hand back to whatever Kino was doing. */
export const REACTIONS = ["jump", "leap", "happy", "oops"] as const satisfies readonly MascotState[];
export type Reaction = (typeof REACTIONS)[number];
const FACING_STATES: ReadonlySet<MascotState> = new Set(["idle", "look", "jump", "happy", "think", "oops"]);

/**
 * A sensible start for a fresh mascot: sleepers begin asleep, screen-facing states already face the screen.
 * `from` carries over a pose Kino already had elsewhere, such as the perch it leaps from.
 */
export function startCursor(state: MascotState, width: number, from?: Partial<Cursor>): Cursor {
  const { left, right } = bounds(width);
  const x = from?.x ?? left + Math.round((right - left) / 2 / 2) * 2;
  return {
    x: Math.min(right, Math.max(left, x)),
    facing: from?.facing ?? (FACING_STATES.has(state) ? "front" : "left"),
    lying: from?.lying ?? state === "sleep",
  };
}

/** The single frame shown when motion is reduced, or before the first animated frame. */
export function staticFrame(state: MascotState, width: number, from?: Partial<Cursor>): Frame {
  const cursor = startCursor(state, width, from);
  if (state === "leap") {
    const flip = cursor.facing === "right";
    const pose: PoseName = cursor.lying ? "sleep" : cursor.facing === "front" ? "front" : "side";
    return { state, pose, flip: pose === "front" ? false : flip, x: cursor.x, leap: 0 };
  }
  if (state === "sleep") return { state, pose: "sleep", flip: false, x: cursor.x };
  if (state === "run") return { state, pose: "run", flip: false, x: cursor.x };
  if (state === "loading") return buildTimeline("loading", cursor, width).loop[4]!;
  if (state === "think") return buildTimeline("think", cursor, width).loop[0]!;
  if (state === "oops") return buildTimeline("oops", cursor, width).intro.at(-1)!;
  return { state, pose: "front", flip: false, x: cursor.x };
}

/** Steps through a state's intro once, then its loop; retargeting continues from the current frame. */
export class MascotPlayer {
  state: MascotState;
  width: number;
  private timeline: Timeline;
  private index = -1;
  private current: Frame;

  constructor(state: MascotState, width: number, from?: Partial<Cursor>) {
    this.state = state;
    this.width = width;
    this.timeline = buildTimeline(state, startCursor(state, width, from), width);
    this.current = this.timeline.intro[0] ?? this.timeline.loop[0]!;
  }

  get frame(): Frame { return this.current; }

  /** How many times the current state's loop has played through; reactions end after the first. */
  get loopsCompleted(): number {
    const played = this.index - this.timeline.intro.length + 1;
    return played <= 0 ? 0 : Math.floor(played / this.timeline.loop.length);
  }

  set(state: MascotState, width: number) {
    if (state === this.state && width === this.width) return;
    const cursor = cursorOf(this.current);
    const { left, right } = bounds(width);
    cursor.x = Math.min(right, Math.max(left, cursor.x));
    this.state = state; this.width = width;
    this.timeline = buildTimeline(state, cursor, width);
    this.index = -1;
  }

  next(): Frame {
    this.index++;
    const { intro, loop } = this.timeline;
    this.current = this.index < intro.length ? intro[this.index]! : loop[(this.index - intro.length) % loop.length]!;
    return this.current;
  }
}

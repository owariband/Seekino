import { describe, expect, it } from "vitest";

import {
  GROUND, MascotPlayer, POSES, REACTIONS, isAirborne, SCENE_H, STATES, bounds, buildTimeline, cursorOf, extent, gaitOffset,
  poseWidth, rasterize, startCursor, staticFrame, toPaths, type Frame, type MascotState, type PoseName,
} from "./engine";

const WIDTH = 64;
const frames = (state: MascotState, width = WIDTH) => {
  const t = buildTimeline(state, startCursor(state, width), width);
  return [...t.intro, ...t.loop];
};
const lowestRow = (frame: Frame, width = WIDTH) => {
  const r = rasterize(frame, width);
  for (let y = r.height - 1; y >= 0; y--) if (r.cells.slice(y * width, (y + 1) * width).some(Boolean)) return y;
  return -1;
};

describe("poses", () => {
  it("keeps every row of a pose the same width", () => {
    for (const [name, pose] of Object.entries(POSES)) {
      expect(new Set(pose.rows.map(r => r.length)).size, name).toBe(1);
    }
  });

  it("mirrors around the anchor so turning does not shift the feet", () => {
    for (const pose of Object.keys(POSES) as PoseName[]) {
      const left = extent({ pose, flip: false, x: 30 }), right = extent({ pose, flip: true, x: 30 });
      expect(left[1] - left[0]).toBe(poseWidth(pose) - 1);
      expect(right[1] - right[0]).toBe(poseWidth(pose) - 1);
      expect(left[0] + right[1]).toBe(right[0] + left[1]);
    }
  });
});

describe("gait", () => {
  it("moves a stance foot back by exactly the stride each frame", () => {
    for (const stride of [1, 2]) {
      for (const offset of [0, 2]) {
        let checked = 0;
        for (let f = 0; f < 4; f++) {
          const l = (f + offset) % 4, next = (f + 1 + offset) % 4;
          // Stance spans phases 0..2, so 0→1 and 1→2 are the steps with both frames on the ground.
          if (l >= 2 || next !== l + 1) continue;
          const a = gaitOffset(f, stride, 1, offset), b = gaitOffset(f + 1, stride, 1, offset);
          expect(b.dx - a.dx).toBe(stride);
          expect([a.dy, b.dy]).toEqual([0, 0]);
          checked++;
        }
        expect(checked).toBe(2);
      }
    }
  });
});

describe("timelines", () => {
  it.each(STATES)("%s stays inside the scene and on the ground", state => {
    for (const frame of frames(state)) {
      const [l, r] = extent(frame);
      expect(l).toBeGreaterThanOrEqual(0);
      expect(r).toBeLessThan(WIDTH);
      if (!frame.lift) expect(lowestRow(frame)).toBe(GROUND - 1);
      for (const fx of frame.fx ?? []) for (const [x, y] of fx.cells) {
        expect(x >= 0 && x < WIDTH && y >= 0 && y < SCENE_H, `${state} effect at ${x},${y}`).toBe(true);
      }
    }
  });

  it.each(STATES)("%s loops back to where it started", state => {
    const { loop } = buildTimeline(state, startCursor(state, WIDTH), WIDTH);
    const first = loop[0]!, last = loop.at(-1)!;
    expect(Math.abs(first.x - last.x)).toBeLessThanOrEqual(2);
    expect(cursorOf(last).facing === cursorOf(first).facing || first.pose !== last.pose).toBe(true);
  });

  it("walks one pixel and runs two pixels per frame", () => {
    const step = (state: MascotState) => {
      const moving = frames(state).filter(f => f.gait != null);
      return Math.max(...moving.slice(1).map((f, i) => Math.abs(f.x - moving[i]!.x)));
    };
    expect(step("wander")).toBe(1);
    expect(step("run")).toBe(2);
  });

  it("wanders to both ends and faces the screen at each one", () => {
    const { left, right } = bounds(WIDTH);
    const { loop } = buildTimeline("wander", startCursor("wander", WIDTH), WIDTH);
    const xs = loop.map(f => f.x);
    expect(Math.min(...xs)).toBe(left);
    expect(Math.max(...xs)).toBe(right);
    const fronts = new Set(loop.filter(f => f.pose === "front" && !f.flip).map(f => f.x));
    expect([...fronts].sort((a, b) => a - b)).toEqual([left, right]);
  });

  it("only uses each state's own poses once settled", () => {
    const loopPoses = (state: MascotState) => new Set(buildTimeline(state, startCursor(state, WIDTH), WIDTH).loop.map(f => f.pose));
    for (const state of ["idle", "look", "jump", "leap", "happy", "think", "oops"] as const) {
      expect(loopPoses(state), state).toEqual(new Set(["front"]));
    }
    expect(loopPoses("loading")).toEqual(new Set(["side"]));
    expect(loopPoses("sleep")).toEqual(new Set(["sleep", "sleepIn"]));
    expect(loopPoses("run").has("run")).toBe(true);
    expect(loopPoses("run").has("side")).toBe(false);
  });

  it("skids only when reversing mid-run", () => {
    const cues = (t: { intro: Frame[]; loop: Frame[] }) => [...t.intro, ...t.loop].map(f => f.cue).filter(Boolean);
    const fromRest = buildTimeline("run", { x: bounds(WIDTH).left, facing: "left", lying: false }, WIDTH);
    expect(cues({ intro: fromRest.intro, loop: [] })).not.toContain("skid");
    expect(cues({ intro: [], loop: fromRest.loop }).filter(c => c === "skid")).toHaveLength(2);
    // Stopping a sprint to face the screen skids too.
    const player = new MascotPlayer("run", WIDTH);
    while (player.next().pose !== "run" || player.frame.gait !== 1) { /* advance into the sprint */ }
    player.set("look", WIDTH);
    expect(player.next().cue).toBe("skid");
  });

  it("floats Zs while sleeping and lights one loading dot at a time", () => {
    const sleep = buildTimeline("sleep", startCursor("sleep", WIDTH), WIDTH).loop;
    expect(sleep.every(f => (f.fx ?? []).length > 0)).toBe(true);
    const loading = buildTimeline("loading", startCursor("loading", WIDTH), WIDTH).loop;
    for (const f of loading) expect(f.fx?.filter(e => e.colour === "body")).toHaveLength(1);
  });
});

describe("kino moves", () => {
  const loopOf = (state: MascotState) => buildTimeline(state, startCursor(state, WIDTH), WIDTH);

  it("jumps seven rows high and lands with dust on both sides", () => {
    const { loop } = loopOf("jump");
    expect(Math.max(...loop.map(f => f.lift ?? 0))).toBe(7);
    const landing = loop.find(f => f.cue === "land")!;
    const x = landing.x;
    const dust = landing.fx!.flatMap(e => e.cells);
    expect(dust.some(([cx]) => cx < x - 8)).toBe(true);
    expect(dust.some(([cx]) => cx > x + 9)).toBe(true);
    // The shadow shrinks away at the top of the arc.
    const apex = loop.find(f => f.lift === 7)!;
    expect(rasterize(apex, WIDTH).shadow).toBeNull();
  });

  it("wakes on the old perch, then flies to the new one and lands with dust", () => {
    const from = { x: 30, facing: "left" as const, lying: true };
    const { intro, loop } = buildTimeline("leap", startCursor("leap", WIDTH, from), WIDTH);
    expect(intro[0]).toMatchObject({ pose: "side", sink: 2, leap: 0 });
    expect(intro.every(f => f.leap === 0)).toBe(true);
    const progress = loop.map(f => f.leap!);
    expect(progress.every((p, i) => i === 0 || p >= progress[i - 1]!)).toBe(true);
    // One stretched airborne pose; the renderer holds it for the whole flight.
    expect(loop.filter(isAirborne)).toEqual([expect.objectContaining({ bob: -1 })]);
    // Short on both ends: a two-frame crouch, then a landing and recovery of five frames.
    expect(loop.findIndex(isAirborne)).toBe(2);
    expect(loop.length - loop.findIndex(isAirborne) - 1).toBe(5);
    const landing = loop.find(f => f.cue === "land")!;
    expect(landing.leap).toBe(1);
    expect(landing.fx?.length).toBe(2);
    expect(loop.every(f => f.x === 30)).toBe(true);
  });

  it("starts from a carried-over pose", () => {
    expect(startCursor("leap", WIDTH, { x: 999, lying: true })).toEqual({ x: bounds(WIDTH).right, facing: "left", lying: true });
    expect(staticFrame("leap", WIDTH, { x: 30, facing: "left", lying: true })).toMatchObject({ pose: "sleep", x: 30, leap: 0 });
    expect(staticFrame("leap", WIDTH, { x: 30, facing: "front", lying: false })).toMatchObject({ pose: "front", leap: 0 });
  });

  it("hops twice with squinting eyes and sparkles when happy", () => {
    const { loop } = loopOf("happy");
    expect(loop.filter(f => f.cue === "hop")).toHaveLength(2);
    expect(loop.filter(f => (f.lift ?? 0) >= 2).every(f => f.blink)).toBe(true);
    expect(loop.slice(0, 20).every(f => (f.fx ?? []).length === 1)).toBe(true);
  });

  it("keeps a question mark over the head while thinking", () => {
    const { loop } = loopOf("think");
    expect(loop.every(f => f.fx?.[0]?.cells.length === 7)).toBe(true);
    expect(loop.some(f => f.look === -1)).toBe(true);
  });

  it("startles with an exclamation mark, shakes in place, then worries", () => {
    const { intro, loop } = loopOf("oops");
    expect(intro[0]!.cue).toBe("startle");
    expect(new Set(intro.map(f => f.x)).size).toBe(3);
    expect(intro.at(-1)!.x).toBe(intro[0]!.x);
    expect(intro.every(f => f.fx?.some(e => e.colour === "ink"))).toBe(true);
    expect(loop.some(f => f.fx?.length)).toBe(true);
  });

  it("breathes and glances while idle", () => {
    const { loop } = loopOf("idle");
    expect(new Set(loop.map(f => f.bob ?? 0))).toEqual(new Set([0, -1]));
    expect(new Set(loop.map(f => f.look ?? 0))).toEqual(new Set([-1, 0, 1]));
    expect(loop.filter(f => f.blink)).toHaveLength(1);
  });

  it.each(REACTIONS)("counts a finished %s reaction after one loop", state => {
    const player = new MascotPlayer("idle", WIDTH);
    player.next();
    player.set(state, WIDTH);
    let frames = 0;
    while (player.loopsCompleted === 0 && frames < 500) { player.next(); frames++; }
    expect(player.loopsCompleted).toBe(1);
    expect(frames).toBeLessThan(80);
  });
});

describe("player", () => {
  it("gets up before walking away from a nap", () => {
    const player = new MascotPlayer("sleep", WIDTH);
    for (let i = 0; i < 10; i++) player.next();
    player.set("wander", WIDTH);
    const first = player.next();
    expect(first.pose).toBe("side");
    expect(first.sink).toBe(2);
    expect(player.next().sink).toBe(1);
  });

  it("turns to the screen from wherever it is when asked to look", () => {
    const player = new MascotPlayer("wander", WIDTH);
    for (let i = 0; i < 7; i++) player.next();
    const x = player.frame.x;
    player.set("look", WIDTH);
    const turning = Array.from({ length: 6 }, () => player.next());
    expect(turning.every(f => f.x === x)).toBe(true);
    expect(turning.at(-1)!.pose).toBe("front");
  });

  it("clamps into a narrower scene", () => {
    const player = new MascotPlayer("wander", 120);
    for (let i = 0; i < 200; i++) player.next();
    player.set("wander", 48);
    for (let i = 0; i < 400; i++) {
      const [l, r] = extent(player.next());
      expect(l).toBeGreaterThanOrEqual(0);
      expect(r).toBeLessThan(48);
    }
  });
});

describe("rendering", () => {
  it("merges runs into one path per colour", () => {
    const paths = toPaths(rasterize(staticFrame("look", WIDTH), WIDTH));
    expect(Object.keys(paths).sort()).toEqual(["belly", "body", "ink", "shade"]);
    expect(paths.ink).toMatch(/^M\d+ \d+h\d+v1h-\d+z/);
  });
});

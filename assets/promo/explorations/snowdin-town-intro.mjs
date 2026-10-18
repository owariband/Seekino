// Seekino promo, Part 1 intro animatic: Kino wakes up and walks through a snowy town, synced to Kino's Theme.
// 1080x1350 (4:5, for the X feed), 30 fps, about 29.9 s. Pixel art is drawn on a 135x169 grid and scaled 8x;
// text boxes and signs are typeset once in Chromium (Menlo) and composited at full resolution.
// Earlier direction (kept for reference): a snowy town with four shops, 30 s.
// Usage (from the repo root, after rendering the theme): node assets/promo/explorations/snowdin-town-intro.mjs
// It plays the first 30 s of the 60 s theme, so bar 13 is now the soft "play" hit rather than the original ta-da.
//
// Storyboard (times come from the theme's bar map: 104 BPM, 16th swing):
//   bars 1-2   0.00-4.62   Kino asleep by its house; its back bumps on every bass hit; "* You remember a scene..."
//   bars 3-4   4.62-9.23   wakes, glances around, big hop on the "boing" (8.08); "* Kino will find it for you!"
//   bars 5-8   9.23-18.46  walks right through the snowy town past the SEEKINO TOWN sign
//   bars 9-12  18.46-25.38 trots past four shops; each sign pops on its glockenspiel ding with a plain-words caption
//   bar 12     25.38-27.69 skids to a stop at the THEATER, faces you, crouches on the bass-only break
//   bar 13     27.69-29.89 ta-da: the door opens, Kino jumps in, white flash, THEATER title card
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import * as E from "../../../web/src/shared/kino/engine.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "../../..");
const OUT = join(HERE, "../out");
const WORK = join(OUT, ".work/intro");
mkdirSync(WORK, { recursive: true });
mkdirSync(join(OUT, "explorations"), { recursive: true });
const THEME = join(OUT, "kino-theme-60s.wav");
if (!existsSync(THEME)) throw new Error(`render the theme first: python3 assets/promo/music/kino_theme.py ${THEME}`);

// 8x keeps Kino and the shops large on a phone; the 2 spare rows of the 169-row grid are cropped at encode.
const FPS = 30, PX = 8, LW = 135, LH = 169, W = LW * PX, H = LH * PX, OUT_H = 1350;
const DURATION = 29.89;
const FRAMES = Math.round(DURATION * FPS);

// ——— The theme's clock (same swing mapping as kino_theme.py) ———
const BEAT = 60 / 104, SWING = 0.58;
function at(bar, beat) {
  const total = (bar - 1) * 4 + beat, q = Math.floor(total + 1e-9), frac = total - q;
  const e = Math.floor(frac * 2 + 1e-9) / 2;
  let g = (frac - e) * 2;
  g = g < 0.5 ? g * 2 * SWING : SWING + (g - 0.5) * 2 * (1 - SWING);
  return (q + e + g / 2) * BEAT;
}
const BAR = b => at(b, 0);
const BOING = at(4, 2), BREAK = at(12, 3), TADA = at(13, 0);
// Non-ghost bass hits in the solo hook (16ths), for Kino's sleepy bumps.
const HOOK_HITS = [[1, [0, 4, 6, 8, 10, 12, 14]], [2, [0, 4, 6, 8, 10, 12, 13, 14]]]
  .flatMap(([bar, s16]) => s16.map(s => at(bar, s / 4)));
const QUOTE_HITS = [12, 14, 15].map(s => at(12, s / 4));

// ——— Palette ———
const hex = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
const C = {
  sky: hex("#0c1412"), far: hex("#13302a"), farSnow: hex("#5f7f7a"),
  snow: hex("#c9dadb"), snowShade: hex("#a3bcbe"), snowDeep: hex("#8aa7aa"),
  pine: hex("#1f5a4e"), pineDark: hex("#16443b"), trunk: hex("#4a3328"),
  wood: hex("#7a5640"), woodDark: hex("#5c3f2f"), plank: hex("#a07a55"), plankDark: hex("#7f5e40"),
  door: hex("#3a2a22"), warm: hex("#f2c46d"), warmDeep: hex("#d99a3e"), frame: hex("#4a3328"),
  black: [0, 0, 0], white: [255, 255, 255],
  roofs: [hex("#349489"), hex("#a5533f"), hex("#6b4f7a"), hex("#b98a2e")],
};
const KINO = Object.fromEntries(Object.entries(E.PALETTE).map(([k, v]) => [k, hex(v)]));
const mixRGB = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));

// ——— World layout (logical px; x grows to the right, the ground surface is y = 108) ———
const GROUND_Y = 108, GY0 = GROUND_Y - E.GROUND, WORLD_W = 600;
const START_X = 70;
const WALK_FROM = BAR(5), RUN_FROM = BAR(9), STOP_AT = BAR(12);
const walkFrames = Math.round((RUN_FROM - WALK_FROM) * 15);
const runFrames = Math.round((STOP_AT - RUN_FROM) * 15);
const RUN_X = START_X + walkFrames, STOP_X = RUN_X + 2 * runFrames;
const kinoXAt = t => t < WALK_FROM ? START_X
  : t < RUN_FROM ? START_X + Math.min(walkFrames, Math.floor((t - WALK_FROM) * 15))
    : t < STOP_AT ? RUN_X + 2 * Math.min(runFrames, Math.floor((t - RUN_FROM) * 15)) : STOP_X;
const SHOPS = ["LIBRARY", "ARCHIVE", "LOST&FOUND", "THEATER"].map((name, k) => ({
  name, roof: C.roofs[k], popAt: BAR(9 + k), x: kinoXAt(BAR(9 + k) + 1e-3),
}));
const LAMPS = [104, 168];
const PINES = [[8, 22], [92, 18], [128, 24], [470, 22], [492, 17], [530, 24], [566, 20]];
const SIGN_X = 146;
const cameraAt = t => Math.max(0, Math.min(WORLD_W - LW, kinoXAt(t) - 44));

// ——— Kino's frames ———
const front = (x, extra = {}) => ({ state: "look", pose: "front", flip: false, x, ...extra });
const SLEEP = E.buildTimeline("sleep", { x: START_X, facing: "right", lying: true }, WORLD_W).loop;
const WAKE = E.buildTimeline("look", { x: START_X, facing: "right", lying: true }, WORLD_W).intro;
const SKID = E.buildTimeline("look", { x: STOP_X, facing: "right", lying: false, running: true }, WORLD_W).intro;
const HOP = [2, 4, 6, 7, 7, 6, 4, 2];
function kinoAt(t) {
  if (t < BAR(3)) {
    const f = SLEEP[Math.floor(t * 6) % SLEEP.length];
    const bumped = HOOK_HITS.some(h => t >= h && t < h + 0.07);
    return bumped ? { ...f, pose: "sleepIn" } : f;
  }
  if (t < BAR(3) + WAKE.length / 10) return WAKE[Math.floor((t - BAR(3)) * 10)];
  if (t < WALK_FROM) {
    if (t >= BOING && t < BOING + HOP.length / 15) return front(START_X, { lift: HOP[Math.floor((t - BOING) * 15)] });
    const turnAt = WALK_FROM - 0.3;
    if (t >= turnAt) {
      const seq = [{ pose: "center", flip: false }, { pose: "front", flip: true }, { pose: "q34", flip: true }];
      return { state: "look", x: START_X, ...seq[Math.min(2, Math.floor((t - turnAt) * 10))] };
    }
    const look = t > 5.6 && t < 6.1 ? -1 : t > 6.4 && t < 6.95 ? 1 : 0;
    return front(START_X, { look, blink: (t > 7.35 && t < 7.43) || (t > 7.52 && t < 7.6) });
  }
  if (t < RUN_FROM) {
    const i = Math.floor((t - WALK_FROM) * 15);
    return { state: "wander", pose: "side", flip: true, x: kinoXAt(t), gait: i % 4, stride: 1, bob: i % 2 ? -1 : 0 };
  }
  if (t < STOP_AT) {
    const i = Math.floor((t - RUN_FROM) * 15);
    return { state: "run", pose: "run", flip: true, x: kinoXAt(t), gait: i % 4, stride: 2, lift: i % 2 };
  }
  // The skid's own dust is drawn in snow white by skidDust, so drop the engine's teal puffs.
  if (t < STOP_AT + SKID.length / 15) return { ...SKID[Math.floor((t - STOP_AT) * 15)], fx: undefined };
  if (t < TADA) {
    const crouch = QUOTE_HITS.some(h => t >= h && t < h + 0.09);
    return front(STOP_X, { bob: crouch ? 1 : 0, look: t > STOP_AT + 0.9 && t < STOP_AT + 1.4 ? 1 : 0 });
  }
  const j = Math.floor((t - TADA) * 15);
  // A short hop through the doorway: kept low so Kino never rises into the THEATER sign.
  return j < 5 ? front(STOP_X, { bob: j < 2 ? 1 : -1, lift: [0, 0, 2, 3, 3][j] }) : null;
}

// ——— Pixel drawing on the logical grid ———
let buf;
const put = (x, y, rgb) => {
  x = Math.round(x); y = Math.round(y);
  if (x >= 0 && x < LW && y >= 0 && y < LH) buf.set(rgb, (y * LW + x) * 3);
};
const rect = (x, y, w, h, rgb) => { for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) put(x + i, y + j, rgb); };
const blend = (x, y, rgb, a) => {
  x = Math.round(x); y = Math.round(y);
  if (x < 0 || x >= LW || y < 0 || y >= LH) return;
  const o = (y * LW + x) * 3;
  buf.set(mixRGB([buf[o], buf[o + 1], buf[o + 2]], rgb, a), o);
};

function farPines(cam) {
  const shift = Math.floor(cam * 0.45);
  for (let k = -1; k < 14; k++) {
    const base = k * 17 - (shift % 17), h = 14 + ((k * 7 + Math.floor(shift / 17) * 3) % 3 + 3) % 3 * 5;
    for (let j = 0; j < h; j++) {
      const half = Math.floor((j * 6) / h) + (j % 4 === 3 ? 0 : 1);
      rect(base + 8 - half, GROUND_Y - h + j, half * 2 + 1, 1, C.far);
      if (j < 2) rect(base + 8 - half, GROUND_Y - h + j, half * 2 + 1, 1, C.farSnow);
    }
  }
}

function pine(cx, h) {
  rect(cx - 1, GROUND_Y - 3, 3, 3, C.trunk);
  const top = GROUND_Y - 3 - h;
  for (let j = 0; j < h; j++) {
    const tier = j % 6, half = 1 + Math.floor(j / 6) * 2 + Math.floor(tier / 2);
    rect(cx - half, top + j, half * 2 + 1, 1, j % 6 === 5 ? C.pineDark : C.pine);
    if (tier === 0) rect(cx - half, top + j, half * 2 + 1, 1, C.snow);
  }
}

function lamp(x, glow) {
  rect(x, GROUND_Y - 24, 2, 24, C.frame);
  rect(x - 2, GROUND_Y - 28, 6, 4, C.frame);
  rect(x - 1, GROUND_Y - 27, 4, 2, C.warm);
  for (let r = 3; r <= 7; r++) for (let a = 0; a < 24; a++) {
    const ang = (a / 24) * Math.PI * 2;
    blend(x + 1 + Math.cos(ang) * r, GROUND_Y - 26 + Math.sin(ang) * r, C.warm, glow * 0.05 * (8 - r));
  }
}

function kinoHome(x) {
  // A small round hut: snow dome roof, wood front, warm round window.
  for (let j = 0; j < 12; j++) {
    const half = Math.round(Math.sqrt(Math.max(0, 144 - (12 - j) ** 2)) * 1.6);
    rect(x + 20 - half, GROUND_Y - 30 + j, half * 2, 1, j < 3 ? C.snow : C.roofs[0]);
  }
  rect(x + 2, GROUND_Y - 18, 36, 18, C.wood);
  for (let j = 0; j < 18; j += 4) rect(x + 2, GROUND_Y - 18 + j, 36, 1, C.woodDark);
  rect(x + 16, GROUND_Y - 13, 8, 13, C.door);
  rect(x + 6, GROUND_Y - 14, 6, 5, C.warm);
  rect(x + 28, GROUND_Y - 14, 6, 5, C.warm);
}

function welcomeSign(x) {
  rect(x - 18, GROUND_Y - 22, 2, 22, C.trunk);
  rect(x + 16, GROUND_Y - 22, 2, 22, C.trunk);
  rect(x - 24, GROUND_Y - 30, 48, 11, C.plank);
  rect(x - 24, GROUND_Y - 20, 48, 1, C.plankDark);
  rect(x - 24, GROUND_Y - 31, 48, 1, C.snow);
}

function shop(s, t) {
  const L = s.x - 29, top = GROUND_Y - 40;
  // Roof with a snow cap and a few drips.
  for (let j = 0; j < 14; j++) {
    const over = 3 + Math.floor(j / 4);
    rect(L - over + 3, top - 14 + j, 58 + 2 * (over - 3), 1, j < 3 ? C.snow : s.roof);
  }
  for (const d of [6, 19, 33, 47]) rect(L + d, top, 1, 2 + (d % 3), C.snow);
  rect(L, top, 58, 40, C.wood);
  for (let j = 3; j < 40; j += 4) rect(L, top + j, 58, 1, C.woodDark);
  // Windows glow; the door opens on the ta-da.
  for (const wx of [L + 5, L + 44]) {
    rect(wx, top + 2, 9, 8, C.warm);
    rect(wx + 4, top + 2, 1, 8, C.frame);
    rect(wx, top + 5, 9, 1, C.frame);
  }
  const open = s.name === "THEATER" && t >= TADA;
  rect(s.x - 5, GROUND_Y - 16, 10, 16, open ? C.warm : C.door);
  if (!open) put(s.x + 3, GROUND_Y - 8, C.warm);
  else for (let j = 0; j < 8; j++) for (let i = -12 - j; i <= 12 + j; i++) blend(s.x + i, GROUND_Y + j, C.warm, 0.4 * (1 - j / 8));
  // Sign board above the door, high enough that Kino's fin clears it even mid-hop.
  // The name pops on the downbeat with a little bounce.
  const since = t - s.popAt, bounce = since < 0 ? 0 : since < 0.05 ? -2 : since < 0.1 ? -1 : 0;
  rect(s.x - 22, top + 12 + bounce, 44, 9, C.plank);
  rect(s.x - 22, top + 20 + bounce, 44, 1, C.plankDark);
  return since >= 0 ? { x: s.x - 22, y: top + 12 + bounce, w: 44, h: 9, alpha: Math.min(1, since / 0.06) } : null;
}

function ground(cam) {
  rect(0, GROUND_Y, LW, LH - GROUND_Y, C.snow);
  rect(0, GROUND_Y, LW, 1, C.white.map((v, i) => Math.round((v + C.snow[i] * 3) / 4)));
  for (let x = 0; x < LW; x++) {
    const wx = x + cam;
    if ((wx * 37) % 23 === 0) put(x, GROUND_Y + 5 + (wx % 7), C.snowShade);
    if ((wx * 53) % 31 === 0) put(x, GROUND_Y + 14 + (wx % 11), C.snowShade);
  }
}

const rng = (() => { let s = 99; return () => ((s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff); })();
const FLAKES = Array.from({ length: 70 }, () => ({
  x: rng() * (LW + 40), y: rng() * LH, v: 7 + rng() * 7, ph: rng() * 6.28, big: rng() < 0.2,
}));
function snowfall(t, cam) {
  for (const f of FLAKES) {
    const x = ((f.x - cam * 0.35 + Math.sin(t * 1.3 + f.ph) * 2) % (LW + 40) + LW + 40) % (LW + 40) - 20;
    const y = (f.y + f.v * t) % LH;
    put(x, y, C.white);
    if (f.big) put(x + 1, y, C.snow);
  }
}

function drawKino(frame, cam) {
  if (!frame) return;
  const r = E.rasterize(frame, WORLD_W);
  if (r.shadow) {
    for (let x = r.shadow.x; x < r.shadow.x + r.shadow.width; x++) put(x - cam, GROUND_Y, C.snowShade);
    for (let x = r.shadow.x + 2; x < r.shadow.x + r.shadow.width - 2; x++) put(x - cam, GROUND_Y + 1, C.snowShade);
  }
  const x0 = Math.max(0, cam - 2), x1 = Math.min(WORLD_W, cam + LW + 2);
  for (let y = 0; y < r.height; y++) for (let x = x0; x < x1; x++) {
    const c = r.cells[y * WORLD_W + x];
    if (c) put(x - cam, GY0 + y, KINO[c]);
  }
}

function runDust(t, cam) {
  if (t < RUN_FROM || t >= STOP_AT) return;
  const i = Math.floor((t - RUN_FROM) * 15);
  for (let age = 0; age < 3; age++) {
    const k = i - age;
    if (k < 0 || k % 2) continue;
    const x = RUN_X + 2 * k - 9 - cam;
    put(x - age, GROUND_Y - 1 - age, C.white);
    put(x - age - 1, GROUND_Y - age, C.snowShade);
  }
}

function skidDust(t, cam) {
  const s = Math.floor((t - STOP_AT) * 15);
  if (s < 0 || s > 6) return;
  for (const [dx, dir] of [[10, 1], [-3, -1]]) for (let age = 0; age <= Math.min(s, 3); age++) {
    const x = STOP_X + dx + dir * (age * 2) - cam;
    put(x, GROUND_Y - 1 - Math.min(age, 2), C.white);
    put(x + dir, GROUND_Y - 1, C.snowShade);
  }
}

function sparkle(t, cam) {
  const s = t - TADA - 5 / 15;
  if (s < 0 || s > 0.5) return;
  const cx = STOP_X - cam, cy = GROUND_Y - 22 - Math.floor(s * 30);
  for (let a = 0; a < 6; a++) {
    const ang = a * Math.PI / 3 + s * 4, r = 4 + s * 26;
    const x = cx + Math.cos(ang) * r, y = cy + Math.sin(ang) * r;
    put(x, y, C.warm); put(x - 1, y, C.white); put(x + 1, y, C.white); put(x, y - 1, C.white); put(x, y + 1, C.white);
  }
}

// ——— Text: typeset once in Chromium, composited at full resolution ———
const BOX = { x: 5, y: 120, w: 125, h: 45 };
const SCRIPT = [
  // The first line is already written on frame 0, which is also the feed preview.
  { from: 0, until: BAR(2), lines: ["* You remember a scene..."], typeAt: [null] },
  { from: BAR(2), until: BAR(3), lines: ["* You remember a scene...", "* ...but not where it was."], typeAt: [null, BAR(2) + 0.05] },
  { from: BAR(3), until: BAR(4), lines: ["* Kino stirs."], typeAt: [BAR(3) + 0.15] },
  { from: BAR(4), until: WALK_FROM, lines: ["* Kino stirs.", "* Kino will find it for you!"], typeAt: [null, BAR(4) + 0.05] },
  ...SHOPS.map((s, k) => ({
    from: s.popAt, until: k === 3 ? TADA : SHOPS[k + 1].popAt,
    lines: [`* ${s.name.replace("&", " & ")}`, ["  Your videos stay on your machine.", "  Every answer shows its source.", "  Describe a scene, get the clip.", "  Ask about what you're watching."][k]],
    // Captions type fast so each stays readable for about 1.5 s before the next shop.
    typeAt: [s.popAt + 0.02, s.popAt + 0.2], cps: 60,
  })),
];
const scriptAt = t => [...SCRIPT].reverse().find(s => t >= s.from && t < s.until) ?? null;
const CPS = 32;

const require = createRequire(join(REPO, "web/package.json"));
const { chromium } = require("playwright");
const browser = await chromium.launch();
const page = await browser.newPage();
async function typeset(text, px, color, weight = "bold") {
  return page.evaluate(({ text, px, color, weight }) => {
    const c = document.createElement("canvas"), g = c.getContext("2d");
    const font = `${weight} ${px}px Menlo, monospace`;
    g.font = font;
    const w = Math.ceil(g.measureText(text).width) + 4, h = Math.ceil(px * 1.35);
    c.width = w; c.height = h;
    g.font = font; g.fillStyle = color; g.textBaseline = "top";
    g.fillText(text, 2, Math.round(px * 0.12));
    const data = g.getImageData(0, 0, w, h).data;
    return { w, h, advance: g.measureText("M").width, rgba: Array.from(data) };
  }, { text, px, color, weight });
}
const texts = new Map();
async function text(key, str, px, color, weight) {
  if (!texts.has(key)) texts.set(key, await typeset(str, px, color, weight));
  return texts.get(key);
}
for (const s of SCRIPT) for (const line of s.lines) await text(`box:${line}`, line, 42, "#ffffff");
for (const s of SHOPS) await text(`sign:${s.name}`, s.name.replace("&", " & "), s.name.length > 8 ? 20 : 26, "#2b1d14");
await text("welcome", "SEEKINO TOWN", 28, "#2b1d14");
await text("card-title", "THEATER", 120, "#f2c46d");
await text("card-sub", "* Ask about what you're watching.", 40, "#ffffff");
await text("card-part", "PART 1", 34, "#5f7f7a");
await browser.close();

function stamp(screen, img, sx, sy, alpha = 1, chars = Infinity) {
  const maxW = Number.isFinite(chars) ? Math.min(img.w, Math.ceil(2 + chars * img.advance)) : img.w;
  for (let y = 0; y < img.h; y++) {
    const py = sy + y;
    if (py < 0 || py >= H) continue;
    for (let x = 0; x < maxW; x++) {
      const px = sx + x;
      if (px < 0 || px >= W) continue;
      const o = (y * img.w + x) * 4, a = img.rgba[o + 3] / 255 * alpha;
      if (!a) continue;
      const d = (py * W + px) * 3;
      for (let k = 0; k < 3; k++) screen[d + k] = Math.round(screen[d + k] * (1 - a) + img.rgba[o + k] * a);
    }
  }
}

// ——— Frame assembly ———
const blips = [];
let lastTyped = 0, lastScript = null;
function renderFrame(n) {
  const t = n / FPS, cam = cameraAt(t);
  buf = Buffer.alloc(LW * LH * 3);
  for (let i = 0; i < LW * LH; i++) buf.set(C.sky, i * 3);
  farPines(cam);
  ground(cam);
  const at0 = x => x - cam;
  kinoHome(at0(18));
  for (const [x, h] of PINES) pine(at0(x), h);
  for (const x of LAMPS) lamp(at0(x), 1);
  welcomeSign(at0(SIGN_X));
  const signs = SHOPS.map(s => {
    const shifted = { ...s, x: s.x - cam };
    return { s, rect: shop(shifted, t) };
  });
  drawKino(kinoAt(t), cam);
  runDust(t, cam);
  skidDust(t, cam);
  sparkle(t, cam);
  snowfall(t, cam);

  const script = t < TADA ? scriptAt(t) : null;
  if (script) {
    rect(BOX.x, BOX.y, BOX.w, BOX.h, C.white);
    rect(BOX.x + 2, BOX.y + 2, BOX.w - 4, BOX.h - 4, C.black);
  }

  // White flash into the title card.
  const flash = t < TADA + 0.55 ? 0 : t < TADA + 0.75 ? (t - TADA - 0.55) / 0.2 : t < TADA + 0.95 ? 1 : 0;
  const card = t >= TADA + 0.95;
  if (card) for (let i = 0; i < LW * LH; i++) buf.set(C.sky, i * 3);
  if (flash) for (let y = 0; y < LH; y++) for (let x = 0; x < LW; x++) blend(x, y, C.white, flash);

  // Upscale 6x: build each screen row once, then repeat it.
  const screen = Buffer.alloc(W * H * 3);
  const row = new Uint8Array(W * 3);
  for (let y = 0; y < LH; y++) {
    let d = 0;
    for (let x = 0; x < LW; x++) {
      const o = (y * LW + x) * 3, r = buf[o], g = buf[o + 1], b = buf[o + 2];
      for (let k = 0; k < PX; k++) { row[d++] = r; row[d++] = g; row[d++] = b; }
    }
    for (let k = 0; k < PX; k++) screen.set(row, (y * PX + k) * W * 3);
  }

  if (!card && flash < 1) {
    const welcome = texts.get("welcome");
    stamp(screen, welcome, (at0(SIGN_X) - 24) * PX + Math.round((48 * PX - welcome.w) / 2), (GROUND_Y - 30) * PX + 6);
    for (const { s, rect: r } of signs) {
      if (!r) continue;
      const img = texts.get(`sign:${s.name}`);
      stamp(screen, img, r.x * PX + Math.round((r.w * PX - img.w) / 2), r.y * PX + Math.round((r.h * PX - img.h) / 2) + 2, r.alpha);
    }
  }
  if (script) {
    if (script !== lastScript) { lastScript = script; lastTyped = 0; }
    let typed = 0;
    script.lines.forEach((line, i) => {
      const img = texts.get(`box:${line}`);
      const start = script.typeAt[i];
      const chars = start == null ? Infinity : Math.max(0, Math.floor((t - start) * (script.cps ?? CPS)));
      if (start != null) typed += Math.min(chars, line.length);
      stamp(screen, img, (BOX.x + 5) * PX, (BOX.y + 5) * PX + i * 62, 1, chars);
    });
    // A soft blip for each newly typed letter.
    if (typed > lastTyped) blips.push(t);
    lastTyped = typed;
  }
  if (card) {
    const k = Math.min(1, (t - TADA - 0.95) / 0.25);
    const title = texts.get("card-title"), sub = texts.get("card-sub"), part = texts.get("card-part");
    stamp(screen, part, Math.round((W - part.w) / 2), 450, k);
    stamp(screen, title, Math.round((W - title.w) / 2), 520 - Math.round((1 - k) * 18), k);
    stamp(screen, sub, Math.round((W - sub.w) / 2), 720, k);
  }
  return screen;
}

// ——— Typewriter blips (quiet), mixed under the theme ———
async function writeBlips(path) {
  const SR = 44100, out = new Float32Array(Math.ceil((DURATION + 0.5) * SR));
  let last = -1;
  for (const t of blips) {
    if (t - last < 0.045) continue;
    last = t;
    const s0 = Math.round(t * SR);
    for (let i = 0; i < 0.03 * SR; i++) {
      const x = i / SR;
      out[s0 + i] += Math.sin(2 * Math.PI * 760 * x) * Math.exp(-x / 0.009) * 0.09;
    }
  }
  const pcm = Buffer.alloc(44 + out.length * 2);
  pcm.write("RIFF", 0); pcm.writeUInt32LE(36 + out.length * 2, 4); pcm.write("WAVEfmt ", 8);
  pcm.writeUInt32LE(16, 16); pcm.writeUInt16LE(1, 20); pcm.writeUInt16LE(1, 22); pcm.writeUInt32LE(SR, 24);
  pcm.writeUInt32LE(SR * 2, 28); pcm.writeUInt16LE(2, 32); pcm.writeUInt16LE(16, 34); pcm.write("data", 36);
  pcm.writeUInt32LE(out.length * 2, 40);
  for (let i = 0; i < out.length; i++) pcm.writeInt16LE(Math.round(Math.max(-1, Math.min(1, out[i])) * 32767), 44 + i * 2);
  writeFileSync(path, pcm);
}

const silent = join(WORK, "video.mp4");
const ff = spawn("ffmpeg", [
  "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", `${W}x${H}`, "-r", String(FPS), "-i", "pipe:0",
  "-vf", `crop=${W}:${OUT_H}:0:0`, "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-tune", "animation", "-pix_fmt", "yuv420p", silent,
], { stdio: ["pipe", "inherit", "inherit"] });
const done = new Promise((resolve, reject) => ff.on("close", code => code ? reject(new Error(`ffmpeg exited ${code}`)) : resolve()));
for (let n = 0; n < FRAMES; n++) {
  if (!ff.stdin.write(renderFrame(n))) await new Promise(r => ff.stdin.once("drain", r));
}
ff.stdin.end();
await done;

const blipWav = join(WORK, "blips.wav");
await writeBlips(blipWav);
const out = join(OUT, "explorations/snowdin-town-intro.mp4");
await new Promise((resolve, reject) => spawn("ffmpeg", [
  "-loglevel", "error", "-y", "-i", silent, "-i", THEME, "-i", blipWav,
  "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]",
  "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", String(DURATION), "-movflags", "+faststart", out,
], { stdio: "inherit" }).on("close", code => code ? reject(new Error(`mux exited ${code}`)) : resolve()));

console.log(`${out}\n${FRAMES} frames · ${DURATION}s · walk ${START_X}->${RUN_X} · run ->${STOP_X} · shops ${SHOPS.map(s => `${s.name}@${s.x}`).join(" ")}`);

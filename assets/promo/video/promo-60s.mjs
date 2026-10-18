// Seekino 60 s promo animatic: Kino walks a dashed ground line, collects "moment" tiles, headbutts a play block,
// and a projector screen drops down to show Seekino. Synced to the 60 s Kino's Theme.
// 1080x1350 (4:5, for the X feed), 30 fps, about 62.2 s. Pixel art on a 135x169 grid scaled 8x; text, footage and
// the wordmark are composited at full resolution.
// Usage (from the repo root): ./assets/promo/render.sh, or after rendering the theme: node assets/promo/video/promo-60s.mjs
//
//   bars 1-4    0.0-9.2   asleep on the line, bumps on the bass hits; wakes; hop on the boing (8.08)
//   bars 5-8    9.2-18.5  walks right, ground and background scroll; "Seekino watches your videos..."
//   bars 9-12   18.5-27.7 hops on each ding to collect a moment tile (one tip each); the play block appears;
//                         stops under it and crouches on the bass break
//   bar 13      27.7      headbutts the block on the play hit; the screen drops with a bounce
//   bars 13-20  27.7-46.2 footage: watch + chat, then search results; hop on the boing (36.35)
//   bars 21-24  46.2-55.4 recap: four screens, one per ding; "Watch. Ask. Find. Check."
//   bars 25-26  55.4-60.0 the screen rolls up; "Kino is getting sleepy..."; crouches on the break
//   bar 27      60.0-62.2 ta-da: wordmark drops in, Kino jumps, then curls up and sleeps
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import * as E from "../../../web/src/shared/kino/engine.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "../../..");
const OUT = join(HERE, "../out");
const WORK = join(OUT, ".work/promo");
mkdirSync(WORK, { recursive: true });
const THEME = join(OUT, "kino-theme-60s.wav");
if (!existsSync(THEME)) throw new Error(`render the theme first: python3 assets/promo/music/kino_theme.py ${THEME}`);

const FPS = 30, PX = 8, LW = 135, LH = 169, W = LW * PX, H = LH * PX, OUT_H = 1350;
const DURATION = 62.2, FRAMES = Math.round(DURATION * FPS);

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
const BOING1 = at(4, 2), BOING2 = at(16, 3), PLAY = BAR(13), TADA = BAR(27), ROLL_UP = BAR(25);
const HOOK_HITS = [[1, [0, 4, 6, 8, 10, 12, 14]], [2, [0, 4, 6, 8, 10, 12, 13, 14]]].flatMap(([b, s]) => s.map(x => at(b, x / 4)));
const QUOTE_HITS = [12, 26].flatMap(b => [12, 14, 15].map(s => at(b, s / 4)));
const DINGS1 = [9, 10, 11, 12].map(BAR), DINGS2 = [21, 22, 23, 24].map(BAR);

// ——— Palette ———
const hex = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
const C = {
  bg: hex("#0c1412"), ground: hex("#2c4a44"), shadow: hex("#060a09"), tile: hex("#16302b"), tileHi: hex("#1f453e"),
  white: [255, 255, 255], black: [0, 0, 0], warm: hex("#f2c46d"), warmDeep: hex("#c98f2e"), teal: hex("#74c5ba"),
  rod: hex("#3b4d49"), rodHi: hex("#5d736e"), surface: hex("#d9e2df"), blockEdge: hex("#7a5a24"),
};
const KINO = Object.fromEntries(Object.entries(E.PALETTE).map(([k, v]) => [k, hex(v)]));
const mixRGB = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));

// ——— Layout (logical px) ———
const GROUND_Y = 104, GY0 = GROUND_Y - E.GROUND;   // dashed line row; Kino's feet stand on row 103
// Footage area, 936 x 528 (16:9). It hangs low, its bottom bar just above the ground line, so Kino stands in front of it.
const SCREEN = { x: 9, y: 34, w: 117, h: 66 };
const TEXT = { x: 5, y: 113, w: 125, h: 44 };
const KINO_SCREEN_X = 58;

// ——— Kino's path ———
const START_X = 40, WALK_FROM = BAR(5), STOP_AT = at(12, 2);
const walkSteps = Math.round((STOP_AT - WALK_FROM) * 15), STOP_X = START_X + walkSteps;
const kinoXAt = t => t < WALK_FROM ? START_X : START_X + Math.min(walkSteps, Math.floor((t - WALK_FROM) * 15));
const cameraAt = t => Math.max(0, kinoXAt(t) - KINO_SCREEN_X);
const BLOCK = { x: STOP_X, y: 77, size: 10 };       // the play block floats over the stopping point
const COINS = DINGS1.map(t => ({ t, x: kinoXAt(t) }));

const front = (x, extra = {}) => ({ state: "look", pose: "front", flip: false, x, ...extra });
const arc = (t, start, lifts) => { const i = Math.floor((t - start) * 15); return i >= 0 && i < lifts.length ? lifts[i] : null; };
const HOP = [2, 4, 6, 7, 7, 6, 4, 2], WALK_HOP = [2, 4, 5, 5, 4, 2], HEADBUTT = [2, 4, 6, 6, 4, 2];
const SLEEP = E.buildTimeline("sleep", { x: START_X, facing: "right", lying: true }, 600).loop;
const WAKE = E.buildTimeline("look", { x: START_X, facing: "right", lying: true }, 600).intro;
const BEDTIME = E.buildTimeline("sleep", { x: STOP_X, facing: "front", lying: false }, 600);
const BED_FROM = TADA + 0.55;

function kinoAt(t) {
  if (t < BAR(3)) {
    const f = SLEEP[Math.floor(t * 6) % SLEEP.length];
    return HOOK_HITS.some(h => t >= h && t < h + 0.07) ? { ...f, pose: "sleepIn" } : f;
  }
  if (t < BAR(3) + WAKE.length / 10) return WAKE[Math.floor((t - BAR(3)) * 10)];
  if (t < WALK_FROM) {
    const lift = arc(t, BOING1, HOP);
    if (lift !== null) return front(START_X, { lift });
    const turnAt = WALK_FROM - 0.3;
    if (t >= turnAt) {
      const seq = [{ pose: "center", flip: false }, { pose: "front", flip: true }, { pose: "q34", flip: true }];
      return { state: "look", x: START_X, ...seq[Math.min(2, Math.floor((t - turnAt) * 10))] };
    }
    return front(START_X, { look: t > 5.6 && t < 6.1 ? -1 : t > 6.4 && t < 6.95 ? 1 : 0, blink: t > 7.35 && t < 7.43 });
  }
  if (t < STOP_AT) {
    const i = Math.floor((t - WALK_FROM) * 15);
    // Hop mid-stride so the top of each hop lands on its ding.
    const lift = COINS.map(c => arc(t, c.t - 2 / 15, WALK_HOP)).find(l => l !== null) ?? 0;
    return { state: "wander", pose: "side", flip: true, x: kinoXAt(t), gait: i % 4, stride: 1, bob: lift ? 0 : i % 2 ? -1 : 0, lift };
  }
  if (t < STOP_AT + 0.2) return { state: "look", x: STOP_X, ...[{ pose: "q34", flip: true }, { pose: "front", flip: true }, { pose: "center", flip: false }][Math.min(2, Math.floor((t - STOP_AT) * 15))] };
  if (t < PLAY - 2 / 15) return front(STOP_X, { bob: QUOTE_HITS.some(h => t >= h && t < h + 0.09) ? 1 : 0, look: -1 });
  const butt = arc(t, PLAY - 2 / 15, HEADBUTT);
  if (butt !== null) return front(STOP_X, { lift: butt, blink: butt >= 6 });
  if (t < ROLL_UP) {
    const hop = arc(t, BOING2 - 2 / 15, HOP);
    if (hop !== null) return front(STOP_X, { lift: hop });
    const ding = DINGS2.some(d => t >= d && t < d + 0.08);
    const k = Math.floor((t - PLAY) / 2.31);
    return front(STOP_X, { bob: ding ? 1 : 0, look: k % 2 ? 1 : -1, blink: (t * 10) % 31 < 0.8 });
  }
  if (t < TADA - 2 / 15) {
    // Sleepy: long slow blinks, then the crouch on the break.
    return front(STOP_X, { blink: (t - ROLL_UP) % 1.6 > 1.0, bob: QUOTE_HITS.some(h => t >= h && t < h + 0.09) ? 1 : 0 });
  }
  const jump = arc(t, TADA - 2 / 15, HEADBUTT);
  if (jump !== null) return front(STOP_X, { lift: jump });
  if (t < BED_FROM) return front(STOP_X);
  const i = Math.floor((t - BED_FROM) * 10), { intro, loop } = BEDTIME;
  return i < intro.length ? intro[i] : loop[Math.floor((t - BED_FROM - intro.length / 10) * 6) % loop.length];
}

// ——— Logical canvas ———
// Two layers: the background (backdrop, ground, screen) sits under the full-resolution footage, and the
// foreground (Kino, tiles, block, text box) goes on top of it. `mask` marks the foreground pixels; 2 = Kino.
let buf, mask = null, tag = 1;
const put = (x, y, rgb) => {
  x = Math.round(x); y = Math.round(y);
  if (x < 0 || x >= LW || y < 0 || y >= LH) return;
  buf.set(rgb, (y * LW + x) * 3);
  if (mask) mask[y * LW + x] = tag;
};
const rect = (x, y, w, h, rgb) => { for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) put(x + i, y + j, rgb); };
const outline = (x, y, w, h, rgb) => { rect(x, y, w, 1, rgb); rect(x, y + h - 1, w, 1, rgb); rect(x, y, 1, h, rgb); rect(x + w - 1, y, 1, h, rgb); };

// Faint drifting "moment tiles": tiny video frames, some with a play mark, the promo's quiet backdrop.
const rand = (() => { let s = 4242; return () => ((s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff); })();
const TILES = Array.from({ length: 18 }, () => ({
  x: rand() * 260, y: 6 + rand() * 88, w: rand() < 0.5 ? 9 : 12, v: 1.5 + rand() * 3, kind: Math.floor(rand() * 3), depth: 0.25 + rand() * 0.3,
}));
function backdrop(t, cam) {
  for (const k of TILES) {
    const x = ((k.x - k.v * t - cam * k.depth) % 260 + 260) % 260 - 40, h = Math.round(k.w * 0.6);
    outline(x, k.y, k.w, h, C.tile);
    if (k.kind === 0) { put(x + Math.floor(k.w / 2) - 1, k.y + 2, C.tileHi); put(x + Math.floor(k.w / 2), k.y + 2, C.tileHi); put(x + Math.floor(k.w / 2) - 1, k.y + 3, C.tileHi); }
    else if (k.kind === 1) { rect(x + 2, k.y + 2, k.w - 5, 1, C.tileHi); rect(x + 2, k.y + 4, k.w - 7, 1, C.tileHi); }
  }
}

function ground(cam) {
  for (let x = 0; x < LW; x++) if ((((x + cam) % 4) + 4) % 4 < 2) put(x, GROUND_Y, C.ground);
}

function drawKino(frame, cam) {
  if (!frame) return;
  const r = E.rasterize(frame, 600);
  if (r.shadow) {
    for (let x = r.shadow.x; x < r.shadow.x + r.shadow.width; x++) put(x - cam, GROUND_Y, C.shadow);
    for (let x = r.shadow.x + 2; x < r.shadow.x + r.shadow.width - 2; x++) put(x - cam, GROUND_Y + 1, C.shadow);
  }
  for (let y = 0; y < r.height; y++) for (let x = Math.max(0, cam - 2); x < Math.min(600, cam + LW + 2); x++) {
    const c = r.cells[y * 600 + x];
    if (c) put(x - cam, GY0 + y, KINO[c]);
  }
}

// Collectible moment tiles, bobbing until Kino hops into them.
function coins(t, cam) {
  for (const c of COINS) {
    const x = c.x - cam - 4, since = t - c.t;
    if (since >= 0.35) continue;
    if (since >= 0) { burst(c.x - cam, 80, since, C.teal); continue; }
    const y = 79 + Math.round(Math.sin(t * 4 + c.x) * 0.8);
    outline(x, y, 9, 6, C.teal);
    put(x + 4, y + 2, C.warm); put(x + 4, y + 3, C.warm); put(x + 5, y + 2, C.warm);
  }
}
function burst(cx, cy, s, rgb) {
  for (let a = 0; a < 6; a++) {
    const ang = a * Math.PI / 3 + s * 3, r = 2 + s * 30;
    const x = cx + Math.cos(ang) * r, y = cy + Math.sin(ang) * r;
    put(x, y, rgb); put(x - 1, y, C.white); put(x + 1, y, C.white);
  }
}

// The play block: floats, takes the headbutt, bumps, then pops.
function block(t, cam) {
  const x = BLOCK.x - cam - 5, since = t - PLAY;
  if (since >= 0.1) { if (since < 0.5) burst(BLOCK.x - cam, BLOCK.y + 4, since - 0.1, C.warm); return; }
  const bump = since >= 0 ? -2 : 0, y = BLOCK.y + bump + (since < 0 ? Math.round(Math.sin(t * 3) * 0.8) : 0);
  rect(x, y, 10, 10, C.warm);
  outline(x, y, 10, 10, C.blockEdge);
  rect(x + 1, y + 1, 8, 1, C.white.map(v => v * 0.95));
  for (let j = 0; j < 5; j++) rect(x + 4, y + 2 + j, j < 3 ? j + 1 : 5 - j, 1, C.blockEdge);  // a play triangle
  // A glint sweeps the block every couple of seconds.
  const g = Math.floor((t * 8) % 18);
  if (g < 10) put(x + g, y + 1, C.white);
}

// ——— Projector screen ———
const DROP_FROM = PLAY + 0.1, DROP_FOR = 0.42, ROLL_FOR = 0.4;
const easeOutBack = p => 1 + 2.4 * (p - 1) ** 3 + 1.4 * (p - 1) ** 2;
function screenRows(t) {
  if (t < DROP_FROM) return 0;
  if (t < DROP_FROM + DROP_FOR) return Math.round(SCREEN.h * Math.max(0, easeOutBack((t - DROP_FROM) / DROP_FOR)));
  if (t < ROLL_UP) return SCREEN.h;
  if (t < ROLL_UP + ROLL_FOR) return Math.round(SCREEN.h * (1 - ((t - ROLL_UP) / ROLL_FOR) ** 2));
  return 0;
}
function screenFrame(rows) {
  if (t0Rows(rows)) return;
  rect(SCREEN.x - 2, SCREEN.y - 3, SCREEN.w + 4, 2, C.rod);
  rect(SCREEN.x - 2, SCREEN.y - 3, SCREEN.w + 4, 1, C.rodHi);
  const shown = Math.min(rows, SCREEN.h + 4);
  rect(SCREEN.x - 1, SCREEN.y - 1, SCREEN.w + 2, shown + 2, C.surface);
  rect(SCREEN.x - 2, SCREEN.y + shown, SCREEN.w + 4, 2, C.rod);   // the weighted bottom bar
}
const t0Rows = rows => rows <= 0;

// Footage: each shot is a 16:9 crop of a Seekino screenshot, panned slowly, resolving out of a mosaic.
const SHOTS = {
  chat: { file: "watch-chat.jpg", crop: "1272:716:0:70", pan: [0, 0, 1.0, 1.1] },
  search: { file: "search-results.jpg", crop: "1000:563:136:150", pan: [0.1, 0.0, 1.0, 1.08] },
  home: { file: "watch-home.jpg", crop: "1272:716:0:80", pan: [0, 0, 1.0, 1.06] },
  series: { file: "series.jpg", crop: "1272:716:0:60", pan: [0, 0, 1.0, 1.06] },
  chatZoom: { file: "watch-chat.jpg", crop: "560:315:712:180", pan: [0, 0, 1.0, 1.05] },
  searchZoom: { file: "search-results.jpg", crop: "620:349:0:200", pan: [0, 0, 1.0, 1.05] },
};
const SW = Math.round(SCREEN.w * PX * 1.12), SH = Math.round(SCREEN.h * PX * 1.12);
for (const shot of Object.values(SHOTS)) {
  const { stdout } = spawnSync("ffmpeg", ["-v", "error", "-i", join(REPO, "assets/screenshots", shot.file),
    "-vf", `crop=${shot.crop},scale=${SW}:${SH}:flags=lanczos`, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], { maxBuffer: 64 << 20 });
  shot.rgb = stdout;
}
const PLAYLIST = [
  { from: DROP_FROM, until: BAR(17), shot: "chat" },
  { from: BAR(17), until: BAR(21), shot: "search" },
  { from: BAR(21), until: BAR(22), shot: "home", quick: true },
  { from: BAR(22), until: BAR(23), shot: "chatZoom", quick: true },
  { from: BAR(23), until: BAR(24), shot: "searchZoom", quick: true },
  { from: BAR(24), until: ROLL_UP + ROLL_FOR, shot: "series", quick: true },
];
function stampFootage(screen, t, rows) {
  const entry = PLAYLIST.find(e => t >= e.from && t < e.until);
  if (!entry || rows <= 0) return;
  const shot = SHOTS[entry.shot], span = entry.until - entry.from, p = Math.min(1, (t - entry.from) / span);
  // Slow push-in: the sampled window shrinks from the whole frame by up to 1/zoom.
  const zoom = shot.pan[2] + (shot.pan[3] - shot.pan[2]) * p, ww = SW / zoom / 1.12 * 1.0, wh = SH / zoom / 1.12;
  const ox = (SW - ww * 1.12) / 2 + shot.pan[0] * p * 40, oy = (SH - wh * 1.12) / 2 + shot.pan[1] * p * 40;
  const resolveFor = entry.quick ? 0.25 : 0.6, resolve = Math.min(1, (t - entry.from) / resolveFor);
  const iw = SCREEN.w * PX, ih = Math.min(rows, SCREEN.h) * PX, x0 = SCREEN.x * PX, y0 = SCREEN.y * PX;
  const edge = Math.round(iw * resolve), block = 24, scale = 1.12 * ww / iw;
  for (let y = 0; y < ih; y++) for (let x = 0; x < iw; x++) {
    let sx = x, sy = y;
    if (x >= edge) { sx = Math.min(iw - 1, Math.floor(x / block) * block + block / 2); sy = Math.min(SCREEN.h * PX - 1, Math.floor(y / block) * block + block / 2); }
    const u = Math.min(SW - 1, Math.round(ox + sx * scale)), v = Math.min(SH - 1, Math.round(oy + sy * scale));
    const s = (v * SW + u) * 3, d = ((y0 + y) * W + x0 + x) * 3;
    screen[d] = shot.rgb[s]; screen[d + 1] = shot.rgb[s + 1]; screen[d + 2] = shot.rgb[s + 2];
  }
  if (resolve < 1) for (let y = 0; y < ih; y++) for (let k = 0; k < 6 && edge + k < iw; k++) screen.set(C.warm, ((y0 + y) * W + x0 + edge + k) * 3);
}

// ——— Text ———
const SCRIPT = [
  { from: 0, until: BAR(2), lines: ["* You remember a scene..."], typeAt: [null] },
  { from: BAR(2), until: BAR(3), lines: ["* You remember a scene...", "* ...but not where it was."], typeAt: [null, BAR(2) + 0.05] },
  { from: BAR(3), until: BAR(4), lines: ["* Kino stirs."], typeAt: [BAR(3) + 0.15] },
  { from: BAR(4), until: BAR(5), lines: ["* Kino stirs.", "* Kino will find it for you!"], typeAt: [null, BAR(4) + 0.05] },
  { from: BAR(7), until: BAR(9), lines: ["* Seekino watches your videos,", "  moment by moment."], typeAt: [BAR(7) + 0.05, BAR(7) + 0.9] },
  ...["* Search every episode at once.", "* Ask about what's on screen.", "* Every answer shows its source.", "* All on your own machine."]
    .map((line, k) => ({ from: DINGS1[k], until: k < 3 ? DINGS1[k + 1] : PLAY, lines: [line], typeAt: [DINGS1[k] + 0.02], cps: 60 })),
  { from: PLAY, until: BAR(15), lines: ["* Pause anytime. Ask anything."], typeAt: [PLAY + 0.5] },
  { from: BAR(15), until: BAR(17), lines: ["* Pause anytime. Ask anything.", "  Answers jump back to the moment."], typeAt: [null, BAR(15) + 0.05] },
  { from: BAR(17), until: BAR(19), lines: ["* Describe a scene you remember."], typeAt: [BAR(17) + 0.05] },
  { from: BAR(19), until: BAR(21), lines: ["* Describe a scene you remember.", "  Get the clip back."], typeAt: [null, BAR(19) + 0.05] },
  ...["* Watch.", "* Watch. Ask.", "* Watch. Ask. Find.", "* Watch. Ask. Find. Check."]
    .map((line, k) => ({ from: DINGS2[k], until: k < 3 ? DINGS2[k + 1] : ROLL_UP, lines: [line], typeAt: [k ? null : DINGS2[k]], cps: 40 })),
  { from: ROLL_UP, until: TADA, lines: ["* Kino is getting sleepy..."], typeAt: [ROLL_UP + 0.3], cps: 18 },
  { from: TADA + 0.6, until: DURATION + 1, lines: ["* (Kino is waiting for", "   your next question.)"], typeAt: [TADA + 0.6, TADA + 1.3] },
];
const scriptAt = t => [...SCRIPT].reverse().find(s => t >= s.from && t < s.until) ?? null;

const require = createRequire(join(REPO, "web/package.json"));
const { chromium } = require("playwright");
const browser = await chromium.launch();
const page = await browser.newPage();
const fontData = readFileSync(join(HERE, "fonts/inter-wordmark-750.woff2")).toString("base64");
await page.evaluate(async data => {
  const face = new FontFace("Seekino Brand", `url(data:font/woff2;base64,${data})`, { weight: "750" });
  document.fonts.add(await face.load());
}, fontData);
const typeset = (text, px, color, family = "Menlo, monospace", weight = "bold") => page.evaluate(({ text, px, color, family, weight }) => {
  const c = document.createElement("canvas"), g = c.getContext("2d"), font = `${weight} ${px}px ${family}`;
  g.font = font;
  const w = Math.ceil(g.measureText(text).width) + 6, h = Math.ceil(px * 1.35);
  c.width = w; c.height = h; g.font = font; g.fillStyle = color; g.textBaseline = "top";
  g.fillText(text, 3, Math.round(px * 0.12));
  return { w, h, advance: g.measureText("M").width, rgba: Array.from(g.getImageData(0, 0, w, h).data) };
}, { text, px, color, family, weight });
// The wordmark matches the web header: Inter 750, teal, with a white dot on the "i".
const wordmark = await page.evaluate(() => {
  const px = 190, c = document.createElement("canvas"), g = c.getContext("2d"), font = `750 ${px}px "Seekino Brand"`;
  g.font = font;
  g.letterSpacing = "-10px";
  const w = Math.ceil(g.measureText("Seekino").width) + 20, h = Math.ceil(px * 1.2);
  c.width = w; c.height = h; g.font = font; g.letterSpacing = "-10px"; g.textBaseline = "top";
  g.fillStyle = "#74c5ba"; g.fillText("Seekino", 10, 10);
  const before = g.measureText("Seek").width, iw = g.measureText("i").width;
  g.save(); g.beginPath(); g.rect(10 + before - 4, 0, iw + 4, 10 + px * 0.42); g.clip();
  g.fillStyle = "#ffffff"; g.fillText("Seekino", 10, 10); g.restore();
  return { w, h, rgba: Array.from(g.getImageData(0, 0, w, h).data) };
});
const texts = new Map();
for (const s of SCRIPT) for (const line of s.lines) if (!texts.has(line)) texts.set(line, await typeset(line, 44, "#ffffff"));
const slogan = await typeset("Ask while you watch. Find any scene.", 40, "#b7c6c1", '"Helvetica Neue", Arial, sans-serif', "500");
const tagline = await typeset("self-hosted · bring your own models", 32, "#5f7f7a", '"Helvetica Neue", Arial, sans-serif', "500");
await browser.close();

function stamp(screen, img, sx, sy, alpha = 1, chars = Infinity) {
  const maxW = Number.isFinite(chars) ? Math.min(img.w, Math.ceil(3 + chars * img.advance)) : img.w;
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

// ——— Frames ———
const blips = [];
let lastTyped = 0, lastScript = null;
function renderFrame(n) {
  const t = n / FPS, cam = cameraAt(t);
  buf = Buffer.alloc(LW * LH * 3);
  for (let i = 0; i < LW * LH; i++) buf.set(C.bg, i * 3);
  backdrop(t, cam);
  ground(cam);
  const rows = screenRows(t);
  screenFrame(rows);
  const back = buf;

  buf = Buffer.alloc(LW * LH * 3);
  mask = new Uint8Array(LW * LH);
  coins(t, cam);
  block(t, cam);
  tag = 2;
  drawKino(kinoAt(t), cam);
  tag = 1;
  // A thin dark outline keeps Kino readable wherever it stands in front of bright footage.
  if (rows > 0) {
    const top = SCREEN.y - 1, bottom = SCREEN.y + Math.min(rows, SCREEN.h);
    for (let y = top; y <= bottom; y++) for (let x = SCREEN.x - 1; x <= SCREEN.x + SCREEN.w; x++) {
      if (mask[y * LW + x]) continue;
      const near = [[1, 0], [-1, 0], [0, 1], [0, -1]].some(([dx, dy]) => mask[(y + dy) * LW + x + dx] === 2);
      if (near) put(x, y, C.bg);
    }
  }
  const script = scriptAt(t);
  if (script) {
    rect(TEXT.x, TEXT.y, TEXT.w, TEXT.h, C.white);
    rect(TEXT.x + 1, TEXT.y + 1, TEXT.w - 2, TEXT.h - 2, C.black);
  }
  const fore = buf, foreMask = mask;
  buf = back;
  mask = null;

  const screen = Buffer.alloc(W * H * 3), row = new Uint8Array(W * 3);
  for (let y = 0; y < LH; y++) {
    let d = 0;
    for (let x = 0; x < LW; x++) {
      const o = (y * LW + x) * 3, r = buf[o], g = buf[o + 1], b = buf[o + 2];
      for (let k = 0; k < PX; k++) { row[d++] = r; row[d++] = g; row[d++] = b; }
    }
    for (let k = 0; k < PX; k++) screen.set(row, (y * PX + k) * W * 3);
  }
  stampFootage(screen, t, rows);
  for (let y = 0; y < LH; y++) for (let x = 0; x < LW; x++) {
    if (!foreMask[y * LW + x]) continue;
    const o = (y * LW + x) * 3;
    for (let j = 0; j < PX; j++) for (let k = 0; k < PX; k++) {
      const d = ((y * PX + j) * W + x * PX + k) * 3;
      screen[d] = fore[o]; screen[d + 1] = fore[o + 1]; screen[d + 2] = fore[o + 2];
    }
  }

  if (t >= TADA) {
    const k = Math.min(1, (t - TADA) / 0.25), drop = Math.round((1 - k) ** 3 * 60);
    stamp(screen, wordmark, Math.round((W - wordmark.w) / 2), 190 - drop, k);
    const k2 = Math.min(1, Math.max(0, (t - TADA - 0.35) / 0.4));
    stamp(screen, slogan, Math.round((W - slogan.w) / 2), 470, k2);
    stamp(screen, tagline, Math.round((W - tagline.w) / 2), 540, k2);
  }
  if (script) {
    if (script !== lastScript) { lastScript = script; lastTyped = 0; }
    let typed = 0;
    script.lines.forEach((line, i) => {
      const img = texts.get(line), start = script.typeAt[i];
      const chars = start == null ? Infinity : Math.max(0, Math.floor((t - start) * (script.cps ?? 32)));
      if (start != null) typed += Math.min(chars, line.length);
      stamp(screen, img, (TEXT.x + 4) * PX, (TEXT.y + 5) * PX + i * 70, 1, chars);
    });
    if (typed > lastTyped) blips.push(t);
    lastTyped = typed;
  }
  return screen;
}

function writeBlips(path) {
  const SR = 44100, out = new Float32Array(Math.ceil((DURATION + 0.5) * SR));
  let last = -1;
  for (const t of blips) {
    if (t - last < 0.045) continue;
    last = t;
    const s0 = Math.round(t * SR);
    for (let i = 0; i < 0.03 * SR; i++) out[s0 + i] += Math.sin(2 * Math.PI * 760 * i / SR) * Math.exp(-i / SR / 0.009) * 0.09;
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
const ff = spawn("ffmpeg", ["-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", `${W}x${H}`, "-r", String(FPS), "-i", "pipe:0",
  "-vf", `crop=${W}:${OUT_H}:0:0`, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-tune", "animation", "-pix_fmt", "yuv420p", silent],
{ stdio: ["pipe", "inherit", "inherit"] });
const done = new Promise((resolve, reject) => ff.on("close", code => code ? reject(new Error(`ffmpeg exited ${code}`)) : resolve()));
for (let n = 0; n < FRAMES; n++) if (!ff.stdin.write(renderFrame(n))) await new Promise(r => ff.stdin.once("drain", r));
ff.stdin.end();
await done;

const blipWav = join(WORK, "blips.wav");
writeBlips(blipWav);
const out = join(OUT, "kino-promo-60s.mp4");
await new Promise((resolve, reject) => spawn("ffmpeg", ["-loglevel", "error", "-y", "-i", silent, "-i", THEME, "-i", blipWav,
  "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]",
  "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", String(DURATION), "-movflags", "+faststart", out],
{ stdio: "inherit" }).on("close", code => code ? reject(new Error(`mux exited ${code}`)) : resolve()));
console.log(`${out}\n${FRAMES} frames · ${DURATION}s · walk ${START_X}->${STOP_X} · coins ${COINS.map(c => c.x).join(",")} · block at ${BLOCK.x}`);

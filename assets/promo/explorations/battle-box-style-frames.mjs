// Style frames for the 60 s Seekino promo: "Undertale's UI is Seekino's UI".
// The white battle box is the screen (abstract data visuals or real Seekino footage), Kino lives on top of it,
// a stats row plays the HP bar, the text box talks to the viewer, and the four buttons are SEARCH / ASK / WATCH / SAVE.
// Earlier direction (kept for reference). 1080x1350 (4:5).
// Usage (from the repo root): node assets/promo/explorations/battle-box-style-frames.mjs  -> assets/promo/out/explorations/style-frames/*.png
import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import * as E from "../../../web/src/shared/kino/engine.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "../../..");
const OUT = join(HERE, "../out/explorations/style-frames");
mkdirSync(OUT, { recursive: true });

const PX = 8, LW = 135, LH = 169, W = LW * PX, H = 1350;
const hex = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
const C = {
  bg: hex("#0c1412"), white: [255, 255, 255], black: [0, 0, 0], dim: hex("#1b2b28"), grid: hex("#14211e"),
  teal: hex("#74c5ba"), tealDeep: hex("#349489"), warm: hex("#f2c46d"), orange: hex("#f08a2c"),
  tiles: [hex("#2f5d57"), hex("#4b3f62"), hex("#6b5a3a"), hex("#3a4f6b"), hex("#5a3a3a"), hex("#2f4a3a")],
};
const KINO = Object.fromEntries(Object.entries(E.PALETTE).map(([k, v]) => [k, hex(v)]));

// Layout on the 135x169 grid.
const BOX = { x: 8, y: 30, w: 119, h: 68 };          // battle box, 1 px white border
const INNER = { x: BOX.x + 1, y: BOX.y + 1, w: BOX.w - 2, h: BOX.h - 2 }; // 117 x 66 -> 936 x 528 (16:9)
const STATS_Y = 101;
const TEXT = { x: 5, y: 112, w: 125, h: 42 };
const BUTTONS_Y = 157;
const LEDGE = BOX.y - 1;                               // Kino's feet stand on the box's top edge
const GY0 = LEDGE + 1 - E.GROUND;

let buf;
const put = (x, y, rgb) => {
  x = Math.round(x); y = Math.round(y);
  if (x >= 0 && x < LW && y >= 0 && y < LH) buf.set(rgb, (y * LW + x) * 3);
};
const rect = (x, y, w, h, rgb) => { for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) put(x + i, y + j, rgb); };
const frameRect = (x, y, w, h, rgb) => { rect(x, y, w, 1, rgb); rect(x, y + h - 1, w, 1, rgb); rect(x, y, 1, h, rgb); rect(x + w - 1, y, 1, h, rgb); };

function drawKino(frame) {
  const r = E.rasterize(frame, LW);
  for (let y = 0; y < r.height; y++) for (let x = 0; x < LW; x++) {
    const c = r.cells[y * LW + x];
    if (c) put(x, GY0 + y, KINO[c]);
  }
}

// ——— Abstract data visuals for the inside of the box ———
const rng = seed => () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
function filmStrip(t, highlight) {
  const r = rng(11);
  const tiles = Array.from({ length: 14 }, () => ({ c: C.tiles[Math.floor(r() * C.tiles.length)], a: r(), b: r(), event: r() < 0.35 }));
  const y = INNER.y + 8, tw = 13, th = 9, gap = 2, shift = Math.floor(t * 9) % (tw + gap);
  rect(INNER.x, y - 3, INNER.w, th + 6, C.dim);
  for (let k = -1; k < tiles.length; k++) {
    const tile = tiles[(k + tiles.length) % tiles.length], x = INNER.x + 2 + k * (tw + gap) - shift;
    for (let j = 0; j < th; j++) for (let i = 0; i < tw; i++) {
      if (x + i < INNER.x || x + i >= INNER.x + INNER.w) continue;
      // Each "frame" is just two soft blocks of colour: footage, not content.
      const inA = i > tile.a * 6 && i < tile.a * 6 + 5 && j > 2, inB = j < 3 + tile.b * 4 && i > 7;
      put(x + i, y + j, inA ? C.teal.map(v => v * 0.55) : inB ? C.warm.map(v => v * 0.45) : tile.c);
    }
    // Sprocket holes above and below the strip.
    for (let i = 1; i < tw; i += 4) { put(x + i, y - 2, C.bg); put(x + i, y + th + 1, C.bg); }
    if (tile.event && highlight && x - 1 >= INNER.x && x + tw + 1 <= INNER.x + INNER.w) frameRect(x - 1, y - 1, tw + 2, th + 2, C.warm);
  }
  return { tiles, y, tw, gap, shift };
}

function timeline(t, scan) {
  const y = INNER.y + 28, x0 = INNER.x + 4, x1 = INNER.x + INNER.w - 5;
  rect(x0, y, x1 - x0, 1, C.tealDeep);
  const r = rng(5);
  const marks = [];
  for (let k = 0; k < 9; k++) {
    const x = Math.round(x0 + 4 + r() * (x1 - x0 - 8));
    const on = x < scan;
    marks.push({ x, on });
    rect(x - 1, y - 1, 3, 3, on ? C.warm : C.dim);
    if (on) rect(x, y - 5, 1, 3, C.warm.map(v => v * 0.6));
  }
  // Audio waveform under the timeline.
  for (let x = x0; x < x1; x += 2) {
    const h = Math.round(2 + 6 * Math.abs(Math.sin(x * 0.37) * Math.sin(x * 0.11 + 1.3)));
    rect(x, y + 14 - h, 1, h * 2, x < scan ? C.teal.map(v => v * 0.8) : C.dim);
  }
  // The scanline that "reads" the video.
  rect(scan, INNER.y + 2, 1, INNER.h - 4, C.warm);
  return marks;
}

function grid() {
  for (let y = INNER.y; y < INNER.y + INNER.h; y += 6) rect(INNER.x, y, INNER.w, 1, C.grid);
  for (let x = INNER.x; x < INNER.x + INNER.w; x += 6) rect(x, INNER.y, 1, INNER.h, C.grid);
}

// ——— Real footage: a 16:9 crop of a Seekino screenshot, resolving from a coarse mosaic ———
function loadShot(name, crop) {
  const src = join(REPO, "assets/screenshots", name);
  const w = INNER.w * PX, h = INNER.h * PX;
  const { stdout } = spawnSync("ffmpeg", ["-v", "error", "-i", src, "-vf", `crop=${crop},scale=${w}:${h}:flags=lanczos`,
    "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], { maxBuffer: 64 * 1024 * 1024 });
  return { w, h, rgb: stdout };
}
function stampFootage(screen, shot, resolve) {
  // resolve: 0 = all mosaic, 1 = all sharp; the sharp part sweeps in from the left like the data scanline.
  const x0 = INNER.x * PX, y0 = INNER.y * PX, edge = Math.round(shot.w * resolve), block = 24;
  for (let y = 0; y < shot.h; y++) for (let x = 0; x < shot.w; x++) {
    let sx = x, sy = y;
    if (x >= edge) { sx = Math.min(shot.w - 1, Math.floor(x / block) * block + block / 2); sy = Math.min(shot.h - 1, Math.floor(y / block) * block + block / 2); }
    const s = (sy * shot.w + sx) * 3, d = ((y0 + y) * W + x0 + x) * 3;
    screen[d] = shot.rgb[s]; screen[d + 1] = shot.rgb[s + 1]; screen[d + 2] = shot.rgb[s + 2];
  }
  // The resolve edge glows warm.
  if (resolve > 0 && resolve < 1) for (let y = 0; y < shot.h; y++) for (let k = 0; k < 6; k++) {
    const d = ((y0 + y) * W + x0 + edge + k) * 3;
    screen.set(C.warm, d);
  }
}

// ——— Text, typeset in Chromium ———
const require = createRequire(join(REPO, "web/package.json"));
const { chromium } = require("playwright");
const browser = await chromium.launch();
const page = await browser.newPage();
const typeset = (text, px, color, weight = "bold") => page.evaluate(({ text, px, color, weight }) => {
  const c = document.createElement("canvas"), g = c.getContext("2d"), font = `${weight} ${px}px Menlo, monospace`;
  g.font = font;
  const w = Math.ceil(g.measureText(text).width) + 4, h = Math.ceil(px * 1.35);
  c.width = w; c.height = h; g.font = font; g.fillStyle = color; g.textBaseline = "top";
  g.fillText(text, 2, Math.round(px * 0.12));
  return { w, h, rgba: Array.from(g.getImageData(0, 0, w, h).data) };
}, { text, px, color, weight });
function stamp(screen, img, sx, sy) {
  for (let y = 0; y < img.h; y++) for (let x = 0; x < img.w; x++) {
    const o = (y * img.w + x) * 4, a = img.rgba[o + 3] / 255, px = sx + x, py = sy + y;
    if (!a || px < 0 || px >= W || py < 0 || py >= H) continue;
    const d = (py * W + px) * 3;
    for (let k = 0; k < 3; k++) screen[d + k] = Math.round(screen[d + k] * (1 - a) + img.rgba[o + k] * a);
  }
}

// ——— Shared chrome: box, stats row, text box, buttons ———
async function chrome(stats, lines, selected) {
  frameRect(BOX.x, BOX.y, BOX.w, BOX.h, C.white);
  rect(TEXT.x, TEXT.y, TEXT.w, TEXT.h, C.white);
  rect(TEXT.x + 1, TEXT.y + 1, TEXT.w - 2, TEXT.h - 2, C.black);
  const labels = ["SEARCH", "ASK", "WATCH", "SAVE"];
  labels.forEach((_, i) => {
    const x = 5 + i * 32;
    frameRect(x, BUTTONS_Y, 29, 10, i === selected ? C.warm : C.orange);
  });
  // Index progress bar (the "HP" bar).
  rect(84, STATS_Y + 3, 28, 4, C.dim);
  rect(84, STATS_Y + 3, Math.round(28 * stats.progress), 4, C.warm);
  const pct = await typeset(`${Math.round(stats.progress * 100)}%`, 28, "#ffffff");
  return [
    { img: await typeset(stats.left, 28, "#ffffff"), x: 5 * PX, y: STATS_Y * PX + 6 },
    { img: await typeset("INDEX", 24, "#f2c46d"), x: 71 * PX, y: STATS_Y * PX + 10 },
    { img: pct, x: 127 * PX - pct.w, y: STATS_Y * PX + 6 },
    ...await Promise.all(lines.map(async (line, i) => ({ img: await typeset(line, 44, "#ffffff"), x: (TEXT.x + 4) * PX, y: (TEXT.y + 5) * PX + i * 70 }))),
    ...await Promise.all(labels.map(async (label, i) => {
      const img = await typeset(label, 28, i === selected ? "#f2c46d" : "#f08a2c");
      return { img, x: (5 + i * 32) * PX + Math.round((29 * PX - img.w) / 2), y: BUTTONS_Y * PX + 14 };
    })),
  ];
}

function encode(screen, name) {
  const path = join(OUT, `${name}.png`);
  spawnSync("ffmpeg", ["-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", `${W}x${LH * PX}`, "-i", "pipe:0",
    "-vf", `crop=${W}:${H}:0:0`, "-frames:v", "1", path], { input: screen });
  return path;
}
function upscale() {
  const screen = Buffer.alloc(W * LH * PX * 3), row = new Uint8Array(W * 3);
  for (let y = 0; y < LH; y++) {
    let d = 0;
    for (let x = 0; x < LW; x++) {
      const o = (y * LW + x) * 3;
      for (let k = 0; k < PX; k++) { row[d++] = buf[o]; row[d++] = buf[o + 1]; row[d++] = buf[o + 2]; }
    }
    for (let k = 0; k < PX; k++) screen.set(row, (y * PX + k) * W * 3);
  }
  return screen;
}
const clear = () => { buf = Buffer.alloc(LW * LH * 3); for (let i = 0; i < LW * LH; i++) buf.set(C.bg, i * 3); };

// ——— Frame A: the hook. Kino asleep on top of an empty box. ———
clear();
grid();
const sleep = E.buildTimeline("sleep", { x: 40, facing: "right", lying: true }, LW).loop;
drawKino(sleep[3]);
let texts = await chrome({ left: "KINO   LV 1   FRAMES 0", progress: 0 }, ["* You remember a scene...", "* ...but not where it was."], -1);
let screen = upscale();
for (const t of texts) stamp(screen, t.img, t.x, t.y);
const a = encode(screen, "A-hook");

// ——— Frame B: data. Kino walks the top edge while the box reads a video into moments. ———
clear();
const scan = INNER.x + 70;
filmStrip(1.3, true);
const marks = timeline(1.3, scan);
drawKino({ state: "wander", pose: "side", flip: true, x: 52, gait: 1, stride: 1, bob: -1 });
texts = await chrome({ left: "KINO   LV 1   FRAMES 34,560", progress: 0.62 }, ["* Seekino watches the whole video,", "  moment by moment."], -1);
screen = upscale();
for (const t of texts) stamp(screen, t.img, t.x, t.y);
const stampsB = [];
const spaced = marks.filter(m => m.on).sort((p, q) => p.x - q.x).filter((m, i, all) => i === 0 || m.x - all[i - 1].x >= 14);
for (const m of spaced.slice(0, 3)) {
  const label = `${String(Math.floor(m.x / 3)).padStart(2, "0")}:${String((m.x * 7) % 60).padStart(2, "0")}`;
  stampsB.push({ img: await typeset(label, 20, "#f2c46d"), x: m.x * PX - 30, y: (INNER.y + 28 - 9) * PX - 18 });
}
for (const t of stampsB) stamp(screen, t.img, t.x, t.y);
const b = encode(screen, "B-data");

// ——— Frame C: footage. A real Seekino screen resolves out of the mosaic; Kino peeks over the box. ———
clear();
drawKino({ state: "look", pose: "front", flip: false, x: 108, look: -1 });
texts = await chrome({ left: "KINO   LV 2   CLIPS 128", progress: 1 }, ["* Describe a scene.", "* Seekino brings back the clip."], 0);
screen = upscale();
stampFootage(screen, loadShot("search-results.jpg", "1000:563:136:150"), 0.72);
for (const t of texts) stamp(screen, t.img, t.x, t.y);
const c = encode(screen, "C-footage");

await browser.close();
spawnSync("ffmpeg", ["-v", "error", "-y", "-i", a, "-i", b, "-i", c, "-filter_complex", "[0][1][2]hstack=3,scale=1620:-1", join(OUT, "triptych.png")]);
console.log(`wrote ${OUT}: A-hook, B-data, C-footage, triptych`);

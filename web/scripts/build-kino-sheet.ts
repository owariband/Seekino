// Renders a contact sheet of every Kino move to assets/mascot/kino/kino-moves.svg.
// Run from web/: node scripts/build-kino-sheet.ts
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  COLOURS, GROUND, PALETTE, SCENE_H, STATES, buildTimeline, rasterize, startCursor, toPaths,
} from "../src/shared/kino/engine.ts";

const WIDTH = 44, PICKS = 6, SCALE = 4, LABEL = 96, GAP = 8;
const out = resolve(dirname(fileURLToPath(import.meta.url)), "../../assets/mascot/kino/kino-moves.svg");
const rowHeight = SCENE_H * SCALE + GAP;
const sheetWidth = LABEL + PICKS * WIDTH * SCALE;
const sheetHeight = STATES.length * rowHeight;

const rows = STATES.map((state, row) => {
  const timeline = buildTimeline(state, startCursor(state, WIDTH), WIDTH);
  const frames = [...timeline.intro, ...timeline.loop];
  const y = row * rowHeight;
  const cells = Array.from({ length: PICKS }, (_, k) => {
    const raster = rasterize(frames[Math.floor(k * frames.length / PICKS)]!, WIDTH);
    const paths = toPaths(raster);
    const x = LABEL + k * WIDTH * SCALE;
    const shadow = raster.shadow
      ? `<path fill="${PALETTE.ink}" opacity=".25" d="M${raster.shadow.x} ${GROUND}h${raster.shadow.width}v1h${-raster.shadow.width}z"/>`
      : "";
    const body = COLOURS.map(c => paths[c] ? `<path fill="${PALETTE[c]}" d="${paths[c]}"/>` : "").join("");
    return `<g transform="translate(${x} ${y}) scale(${SCALE})">${shadow}${body}</g>`;
  }).join("");
  const label = `<text x="12" y="${y + SCENE_H * SCALE / 2 + 6}" font-family="Inter, system-ui, sans-serif" font-size="16" font-weight="650" fill="${PALETTE.ink}">${state}</text>`;
  return label + cells;
}).join("\n");

const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${sheetWidth} ${sheetHeight}" width="${sheetWidth}" height="${sheetHeight}" shape-rendering="crispEdges">
<title>Kino moves</title>
<rect width="100%" height="100%" fill="#f4f7f2"/>
${rows}
</svg>
`;
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, svg);
console.log(`wrote ${out} (${STATES.length} states × ${PICKS} frames)`);

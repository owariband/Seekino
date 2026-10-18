// Screenshot the direction board and export per-direction preview frames.
// Usage: node shoot.mjs [--frames]
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(join(here, "../../web/package.json"));
const { chromium } = require("@playwright/test");

const page_url = pathToFileURL(join(here, "Seekino Pixel Walk · 三方向.html")).href;
const out = join(here, "out");
mkdirSync(out, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1760, height: 1000 }, deviceScaleFactor: 2 });
const errors = [];
page.on("pageerror", e => errors.push(String(e)));
page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
await page.goto(page_url);
await page.waitForFunction(() => window.__ready === true, null, { timeout: 8000 }).catch(e => {
  console.log(errors.join("\n"));
  throw e;
});
await page.waitForTimeout(700);
await page.screenshot({ path: join(out, "board.png"), fullPage: true });
await page.click('button[data-mode="place"]');
await page.waitForTimeout(450);
await page.screenshot({ path: join(out, "board-place.png"), fullPage: true });

if (process.argv.includes("--frames")) {
  for (const [id, SW, SH, scale] of [["a", 48, 22, 10], ["b", 96, 52, 5], ["c", 84, 58, 5]]) {
    const frames = await page.evaluate(([i, w, h, s]) => window.__exportLoop(i, w, h, s), [id, SW, SH, scale]);
    const dir = join(out, `loop-${id}`);
    mkdirSync(dir, { recursive: true });
    frames.forEach((u, n) => writeFileSync(join(dir, `${String(n).padStart(3, "0")}.png`), Buffer.from(u.split(",")[1], "base64")));
    const sheet = await page.evaluate(([i]) => window.__exportSheet(i, 8), [id]);
    writeFileSync(join(out, `sheet-${id}.png`), Buffer.from(sheet.split(",")[1], "base64"));
  }
}
await browser.close();
console.log(errors.length ? `ERRORS:\n${errors.join("\n")}` : "no console errors");

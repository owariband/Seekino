// Screenshot the A wander page and export loop frames + transparent sheet.
// Usage: node shoot-wander.mjs
import { createRequire } from "node:module";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(join(here, "../../web/package.json"));
const { chromium } = require("@playwright/test");

const page_url = pathToFileURL(join(here, "Seekino Pixel A · 来回游荡.html")).href;
const out = join(here, "out");
mkdirSync(out, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 }, deviceScaleFactor: 2 });
const errors = [];
page.on("pageerror", e => errors.push(String(e)));
page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
await page.goto(page_url);
await page.waitForFunction(() => window.__ready === true, null, { timeout: 8000 }).catch(e => {
  console.log(errors.join("\n"));
  throw e;
});
// 2× speed so the shots land in different phases: walking, then turned to face the screen.
await page.click('.speed button[data-speed="2"]');
await page.waitForTimeout(1200);
await page.screenshot({ path: join(out, "wander-board.png"), fullPage: true });
await page.waitForTimeout(1700);
await page.locator(".theater").screenshot({ path: join(out, "wander-front.png") });
await page.evaluate(() => localStorage.removeItem("seekino-wander-speed"));

const frames = await page.evaluate(() => window.__exportLoop(8));
const dir = join(out, "loop-a-wander");
rmSync(dir, { recursive: true, force: true });
mkdirSync(dir, { recursive: true });
frames.forEach((u, n) => writeFileSync(join(dir, `${String(n).padStart(3, "0")}.png`), Buffer.from(u.split(",")[1], "base64")));
const sheet = await page.evaluate(() => window.__exportSheet(8));
writeFileSync(join(out, "sheet-a-wander.png"), Buffer.from(sheet.split(",")[1], "base64"));
await browser.close();
console.log(`frames=${frames.length}`);
console.log(errors.length ? `ERRORS:\n${errors.join("\n")}` : "no console errors");

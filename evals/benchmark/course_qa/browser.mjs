/** Real browser evidence collection; no mocked API, no automatic quality pass. */
import { chromium } from "../../../web/node_modules/playwright/index.mjs";
import { readFileSync, writeFileSync, appendFileSync, mkdirSync } from "node:fs";
import { resolve, dirname } from "node:path";

const [origin, destination, live] = process.argv.slice(2);
if (!origin || !destination || ![undefined, "--live"].includes(live)) {
  throw new Error("Usage: node evals/benchmark/course_qa/browser.mjs http://127.0.0.1:15173 OUTPUT [--live]");
}
if (!["localhost", "127.0.0.1"].includes(new URL(origin).hostname)) throw new Error("Use an isolated loopback QA service");
const cases = JSON.parse(readFileSync(new URL("./cases.json", import.meta.url)));
const out = resolve(destination);
mkdirSync(dirname(out), { recursive: true });
mkdirSync(out);
const save = (name, value) => writeFileSync(`${out}/${name}.json`, JSON.stringify(value, null, 2));
const rows = [];
const errors = [];
const requests = [];
const media = [];
const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const state = async () => ({
  url: page.url(), text: await page.locator("body").innerText(),
  videos: await page.locator("video").evaluateAll(videos => videos.map(v => ({
    currentTime: v.currentTime, duration: v.duration, readyState: v.readyState,
    paused: v.paused, error: v.error?.message ?? null,
  }))),
});
await page.exposeFunction("recordQaEvent", row => {
  rows.push(row);
  appendFileSync(`${out}/events.jsonl`, JSON.stringify(row) + "\n");
  if (row.event !== "answer.delta") console.log(JSON.stringify({ turn: row.turn, event: row.event, seconds: row.seconds }));
});
await page.addInitScript(() => {
  localStorage.setItem("seekino-locale", "zh");
  const original = window.fetch;
  window.fetch = async function(input, options) {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (!url.includes("/api/agent/messages/send")) return original.apply(this, arguments);
    const start = performance.now();
    const turn = window.qaTurn;
    const response = await original.apply(this, arguments);
    if (response.headers.get("content-type")?.includes("text/event-stream")) {
      void (async () => {
        const reader = response.clone().body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { value, done } = await reader.read();
          buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
          buffer = buffer.replace(/\r\n/g, "\n");
          let index;
          while ((index = buffer.indexOf("\n\n")) >= 0) {
            const lines = buffer.slice(0, index).split("\n");
            buffer = buffer.slice(index + 2);
            const event = lines.find(line => line.startsWith("event:"))?.slice(6).trim();
            const data = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
            if (event && data) await window.recordQaEvent({ turn, event, seconds: (performance.now() - start) / 1000, data: JSON.parse(data) });
          }
          if (done) break;
        }
      })().catch(error => window.recordQaEvent({ turn, event: "observer.error", error: String(error) }));
    } else await window.recordQaEvent({ turn, event: "http.error", status: response.status, body: await response.clone().text() });
    return response;
  };
});
page.on("pageerror", error => errors.push(error.message));
page.on("request", request => {
  if (request.url().includes("/api/agent/messages/send")) requests.push({
    url: request.url(), payload: request.postDataJSON(), key: request.headers()["idempotency-key"],
  });
});
page.on("response", response => {
  if (response.url().includes("/api/videos/content")) media.push({ url: response.url(), status: response.status(), range: response.headers()["content-range"] });
});
try {
  await page.goto(`${origin}/watch/${cases.series_id}/${cases.video_id}?tenantId=local-default&atSeconds=${cases.position_ms}`);
  await page.getByRole("button", { name: "打开观看侧栏" }).click();
  await page.waitForFunction(() => document.querySelector("video")?.readyState >= 2);
  save("initial", await state());
  await page.screenshot({ path: `${out}/initial.png`, fullPage: true });
  if (live) {
    await page.getByRole("button", { name: "新建对话", exact: true }).click();
    for (const turn of cases.turns) {
      await page.evaluate(id => { window.qaTurn = id; }, turn.id);
      await page.getByRole("textbox", { name: "向 Seekino 助手提问" }).fill(turn.query);
      await page.getByRole("button", { name: "发送问题", exact: true }).click();
      console.log(`sent ${turn.id}`);
      const deadline = Date.now() + 240_000;
      while (!rows.some(row => row.turn === turn.id && ["answer.completed", "answer.failed", "answer.interrupted", "http.error", "observer.error"].includes(row.event))) {
        if (Date.now() > deadline) {
          const stop = page.getByRole("button", { name: "停止生成", exact: true });
          if (await stop.isEnabled().catch(() => false)) await stop.click();
          throw new Error(`${turn.id}: QA 240-second deadline; cancellation requested, terminal state needs inspection`);
        }
        await page.waitForTimeout(500);
      }
      await page.waitForTimeout(600);
      save(turn.id, await state());
      await page.screenshot({ path: `${out}/${turn.id}.png`, fullPage: true });
      if (!rows.some(row => row.turn === turn.id && row.event === "answer.completed")) throw new Error(`${turn.id}: non-success terminal; stop paid requests`);
    }
    const links = page.locator('button[title^="跳转到"]');
    const count = await links.count();
    save("time-links", { count, labels: await links.allTextContents() });
    if (count) {
      const title = await links.first().getAttribute("title");
      const before = await state();
      await links.first().click();
      await page.waitForTimeout(1000);
      save("seek", { clicked: title, before, ...await state() });
      await page.screenshot({ path: `${out}/seek.png`, fullPage: true });
    }
    const before = await state();
    await page.reload();
    await page.getByRole("button", { name: "打开观看侧栏" }).click();
    await page.getByText(cases.turns.at(-1).query, { exact: true }).waitFor();
    await page.waitForTimeout(600);
    save("refresh", { before, after: await state() });
    await page.screenshot({ path: `${out}/refresh.png`, fullPage: true });
  }
} catch (error) {
  save("runner-error", { message: String(error), state: await state().catch(() => null) });
  process.exitCode = 1;
} finally {
  save("requests", requests);
  save("browser-errors", errors);
  save("media", media);
  save("run", { live: Boolean(live), recordedEvents: rows.length, requests: requests.length, browserVersion: browser.version(), quality: "Requires human evidence review" });
  await browser.close();
}

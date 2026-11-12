// Production-build replay. All API data and model events below are isolated fixtures.
// node web/tests/chat-frontend-performance.mjs /absolute/dist /absolute/result.json [1,20,60] [--expect-fixed]
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, extname, resolve, sep } from "node:path";
import { chromium, expect } from "@playwright/test";
import { configuredModels } from "../src/test/personalModelFixtures.ts";

const [buildPath, outputPath, counts = "1,20,60"] = process.argv.slice(2);
if (!buildPath || !outputPath) throw new Error("Supply a built dist directory and a result.json path");
const dist = resolve(buildPath);
const expectFixed = process.argv.includes("--expect-fixed");
const chunk = "流式回放用于测量页面更新、输入和自动滚动。";
const summarize = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  return { samples: sorted.length, p50Ms: sorted[Math.floor(sorted.length * .5)] ?? null,
    p95Ms: sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * .95))] ?? null,
    maxMs: sorted.at(-1) ?? null };
};

function fixture(historyCount) {
  const conversation = { conversationId: "perf-conv", tenantId: "local-default", title: "固定性能回放",
    modelId: configuredModels.defaultModelId, messageCount: historyCount * 2, createdAt: 0, updatedAt: 0,
    lastMessagePreview: null, lastMessageAt: null };
  const message = (id, sequenceNo, content, overrides = {}) => ({
    messageId: id, conversationId: conversation.conversationId, sequenceNo, role: "ASSISTANT", status: "COMPLETED",
    phase: null, version: 1, seriesIds: [], reference: null, attachments: [], content, modelName: "fixture",
    searchResult: null, renderBlocks: content ? [{ type: "markdown", markdown: content }] : [],
    timing: null, lastError: null, createdAt: 0, updatedAt: 0, ...overrides,
  });
  const paragraph = "向量保存一组数值，矩阵把多个向量按行列组织。这个固定段落只用于性能回放，不包含真实课程或用户数据。";
  const history = Array.from({ length: historyCount }, (_, i) => [
    message(`user-${i}`, i * 2, `历史问题 ${i}`, { role: "USER", renderBlocks: [] }),
    message(`answer-${i}`, i * 2 + 1, `## 固定回答 ${i}\n\n${paragraph.repeat(12)}\n\n- **第一步**：检查形状。\n- **第二步**：核对结果。\n\n\`\`\`python\na = [1, 2, 3]\n\`\`\``),
  ]).flat();
  let stored = history;
  const user = message("live-user", history.length, "继续做一次固定回放", { role: "USER", renderBlocks: [] });
  let assistant = message("live-answer", history.length + 1, "", { status: "PROCESSING", phase: "QUEUED" });
  let response;
  let sequence = 0;
  let sent = 0;
  let interval;
  let snapshotInterval;
  let postCount = 0;
  const subscribers = new Set();
  const unexpected = [];
  let accept;
  const opened = new Promise((resolveOpened) => { accept = resolveOpened; });
  const emit = (type, fields = {}) => response.write(`event: ${type}\ndata: ${JSON.stringify({ type,
    conversationId: conversation.conversationId, messageId: assistant.messageId,
    occurredAt: Date.now(), sequence: ++sequence, ...fields })}\n\n`);
  const snapshot = () => {
    const payload = JSON.stringify({ type: "messages", conversationId: conversation.conversationId,
      occurredAt: Date.now(), messages: stored });
    for (const subscriber of subscribers) subscriber.write(`event: messages\ndata: ${payload}\n\n`);
  };
  const json = (res, value, status = 200) => { res.writeHead(status, { "content-type": "application/json" }); res.end(JSON.stringify(value)); };
  const server = createServer(async (req, res) => {
    const path = new URL(req.url, "http://fixture").pathname;
    if (path === "/healthz") return json(res, { status: "ok" });
    if (path === "/api/auth/me") return json(res, { userId: "local", email: null, isGuest: false,
      tenantId: "local-default", publicTenantId: "public", mode: "local", csrfToken: "", emailLoginEnabled: false,
      permissions: { canUpload: true, canManage: true, canConfigure: true } });
    if (path === "/api/system/runtime-config") return json(res, { displayMode: "release" });
    if (path === "/api/agent/model-connections") return json(res, configuredModels);
    if (path === "/api/agent/messages/options") return json(res, Object.fromEntries(
      ["topK", "maxLoopTurns", "maxModelCallsPerTurn", "maxToolCallsPerTurn"].map((key) => [key, { defaultValue: 3, minimum: 1, maximum: 30 }])));
    if (path === "/api/agent/conversations/get") return json(res, conversation);
    if (path === "/api/agent/conversations/list") return json(res, { items: [conversation], total: 1, hasMore: false, nextCursor: null });
    if (path === "/api/agent/messages/list") return json(res, { items: stored, total: stored.length, hasMore: false, nextCursor: null });
    if (path === "/api/agent/messages/events") {
      res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
      res.write(": ready\n\n"); subscribers.add(res);
      req.on("close", () => subscribers.delete(res));
      return;
    }
    if (path === "/api/agent/messages/send" && req.method === "POST") {
      req.resume(); postCount++; response = res;
      stored = [...history, user, assistant];
      res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
      emit("answer.started", { userMessage: user, assistantMessage: assistant });
      accept();
      return;
    }
    if (path.startsWith("/api/") && path.endsWith("/list")) return json(res, { items: [], total: 0, hasMore: false, nextCursor: null });
    if (path.startsWith("/api/")) { unexpected.push(path); return json(res, { message: "Unexpected fixture route" }, 404); }
    const requested = resolve(dist, `.${decodeURIComponent(path)}`);
    if (requested !== dist && !requested.startsWith(dist + sep)) return json(res, {}, 404);
    const file = extname(requested) ? requested : resolve(dist, "index.html");
    try {
      const data = await readFile(file);
      const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
        ".png": "image/png", ".woff2": "font/woff2", ".json": "application/json" }[extname(file)] ?? "application/octet-stream";
      res.writeHead(200, { "content-type": mime }); res.end(data);
    } catch { res.writeHead(404); res.end(); }
  });
  return { server, opened, unexpected,
    phase(phase) { assistant = { ...assistant, phase }; stored = [...history, user, assistant]; emit("answer.progress", { phase }); },
    stream() {
      snapshotInterval = setInterval(snapshot, 1000);
      return new Promise((done) => {
        interval = setInterval(() => {
          sent++;
          const content = assistant.content + chunk;
          assistant = { ...assistant, content, version: sent + 1, renderBlocks: [{ type: "markdown", markdown: content }] };
          stored = [...history, user, assistant];
          emit("answer.delta", { callId: "fixture-call", newBlock: sent === 1, delta: chunk,
            textStream: { origin: "provider_delta", callId: "fixture-call", deltaSequence: sent, modelFirstTextMs: 1,
              modelElapsedMs: sent * 33, providerReceivedMonotonicMs: sent * 33,
              serverPublishedMonotonicMs: sent * 33, beforeModelCompleted: true } });
          if (sent === 100) { clearInterval(interval); clearInterval(snapshotInterval); done(); }
        }, 33);
      });
    },
    durableComplete() { assistant = { ...assistant, status: "COMPLETED", version: 102 }; stored = [...history, user, assistant]; snapshot(); },
    terminal() { emit("answer.completed", { userMessage: user, assistantMessage: assistant }); },
    end() { response.end(); },
    get postCount() { return postCount; },
    close() { clearInterval(interval); clearInterval(snapshotInterval); for (const res of subscribers) res.end(); server.closeAllConnections(); server.close(); },
  };
}

const browser = await chromium.launch({ channel: process.env.SEEKINO_TEST_BROWSER_CHANNEL || undefined });
const results = [];
try {
  for (const historyCount of counts.split(",").map(Number)) {
    assert(Number.isInteger(historyCount) && historyCount > 0);
    const api = fixture(historyCount);
    await new Promise((ready) => api.server.listen(0, "127.0.0.1", ready));
    const origin = `http://127.0.0.1:${api.server.address().port}`;
    const context = await browser.newContext({ locale: "zh-CN", viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const cdp = await context.newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
    await page.addInitScript((chunkLength) => {
      localStorage.setItem("seekino-agent-conversation:local-default", JSON.stringify({ key: "fixture", id: "perf-conv" }));
      const perf = window.chatPerf = { inputs: [], dom: [], frames: [], longTasks: [], phases: [], kinoStates: [], windows: {},
        pending: [], receivedChars: 0, stage: "load", expected: null };
      new PerformanceObserver((list) => { for (const entry of list.getEntries()) perf.longTasks.push({ start: entry.startTime, duration: entry.duration }); })
        .observe({ type: "longtask", buffered: true });
      let last = performance.now();
      let lastStage = "load";
      const frame = (at) => {
        if (perf.stage !== "load" && perf.stage === lastStage) perf.frames.push({ stage: perf.stage, ms: at - last });
        last = at; lastStage = perf.stage; requestAnimationFrame(frame);
      };
      requestAnimationFrame(frame);
      window.addEventListener("seekino:chat-delivery", ({ detail }) => {
        if (detail.stage === "answer.delta") {
          perf.receivedChars += chunkLength; perf.pending.push({ at: performance.now(), chars: perf.receivedChars });
        }
        if (detail.stage === "text.rendered") {
          while (perf.pending.length && perf.pending[0].chars <= detail.renderedChars) perf.dom.push(performance.now() - perf.pending.shift().at);
        }
        if (detail.stage === "answer.progress" && perf.expected) perf.expected.receivedAt = performance.now();
      });
      new MutationObserver(() => {
        const state = document.querySelector("[data-kino-stage] [data-kino]")?.getAttribute("data-kino-state");
        if (state && perf.kinoStates.at(-1)?.state !== state) perf.kinoStates.push({ state, stage: perf.stage, at: performance.now() });
        const expected = perf.expected;
        if (expected?.receivedAt != null && document.querySelector(`[data-kino-state="${expected.state}"]`)) {
          perf.phases.push({ state: expected.state, ms: performance.now() - expected.receivedAt }); perf.expected = null;
        }
      }).observe(document, { subtree: true, attributes: true, attributeFilter: ["data-kino-state"] });
    }, chunk.length);
    try {
      await page.goto(origin + "/search");
      const input = page.getByRole("textbox", { name: "向 Seekino 助手提问" });
      await expect(input).toBeVisible();
      await expect(page.locator("article")).toHaveCount(historyCount * 2);
      const historyReadyMs = await page.evaluate(() => performance.now());
      await page.evaluate(() => { window.chatPerf.stage = "input"; window.chatPerf.windows.input = { start: performance.now() }; });
      for (let i = 0; i < 35; i++) await page.evaluate((index) => new Promise((done) => {
        const input = document.querySelector('textarea[aria-label="向 Seekino 助手提问"]');
        const at = performance.now();
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(input, "回放输入" + "字".repeat(index + 1));
        input.dispatchEvent(new Event("input", { bubbles: true }));
        requestAnimationFrame(() => { if (index >= 5) window.chatPerf.inputs.push(performance.now() - at); done(); });
      }), i);
      await page.evaluate(() => { window.chatPerf.windows.input.end = performance.now(); window.chatPerf.stage = "phase"; });
      await input.fill("继续做一次固定回放"); await input.press("Enter"); await api.opened;
      await expect(page.locator('[data-kino-state="wander"]')).toBeVisible({ timeout: 15000 });
      for (const [phase, state] of [["RETRIEVING", "run"], ["EVALUATING", "think"], ["RESPONDING", "loading"]]) {
        await page.evaluate((state) => { window.chatPerf.expected = { state, receivedAt: null }; }, state);
        api.phase(phase);
        await expect(page.locator(`[data-kino-state="${state}"]`)).toBeVisible();
      }
      await page.evaluate(() => { window.chatPerf.stage = "stream"; window.chatPerf.windows.stream = { start: performance.now() }; });
      const streaming = api.stream();
      await page.waitForTimeout(500);
      const thread = page.locator("[data-kino-stage]");
      await thread.hover(); await page.mouse.wheel(0, -800);
      await page.waitForTimeout(500);
      const stayedUp = await thread.evaluate((node) => node.scrollHeight - node.scrollTop - node.clientHeight > 80);
      await thread.evaluate((node) => { node.scrollTop = node.scrollHeight; });
      await streaming;
      await expect(page.locator("article").last()).toContainText(chunk.repeat(100));
      const followedBottom = await thread.evaluate((node) => node.scrollHeight - node.scrollTop - node.clientHeight < 80);
      await page.evaluate(() => { window.chatPerf.windows.stream.end = performance.now(); window.chatPerf.stage = "tail"; });
      await input.fill("下一条草稿");
      api.durableComplete();
      await expect(page.locator("article").last().getByLabel("回答状态", { exact: true })).toContainText("已完成");
      const snapshotFirstConnecting = await page.getByText("正在连接助手…", { exact: true }).isVisible();
      api.terminal();
      await page.waitForTimeout(300);
      const terminalConnecting = await page.getByText("正在连接助手…", { exact: true }).isVisible();
      const stopVisible = await page.getByRole("button", { name: "停止生成", exact: true }).isVisible();
      const send = page.getByRole("button", { name: "发送问题", exact: true });
      const sendVisible = await send.isVisible();
      const tailSendDisabled = sendVisible ? await send.isDisabled() : null;
      await page.waitForTimeout(1700);
      api.end();
      await expect(send).toBeEnabled();
      const metrics = await page.evaluate(() => window.chatPerf);
      results.push({ historyCount, historyReadyMs, inputToNextRaf: summarize(metrics.inputs), deltaToObservedDom: summarize(metrics.dom),
        inputFrameGaps: summarize(metrics.frames.filter((x) => x.stage === "input").map((x) => x.ms)),
        streamFrameGaps: summarize(metrics.frames.filter((x) => x.stage === "stream").map((x) => x.ms)),
        longTasks: Object.fromEntries(["input", "stream"].map((name) => [name, metrics.longTasks.filter((x) => x.start >= metrics.windows[name].start && x.start <= metrics.windows[name].end)])),
        kinoPhaseMs: metrics.phases, kinoTailStates: metrics.kinoStates.filter((x) => x.stage === "tail").map((x) => x.state),
        snapshotFirstConnecting, terminalConnecting, stopVisible, tailSendDisabled,
        stayedUp, followedBottom, postCount: api.postCount, errors, unexpectedApiPaths: api.unexpected });
      assert.equal(errors.length, 0); assert.equal(api.unexpected.length, 0); assert.equal(api.postCount, 1);
      assert.equal(metrics.dom.length, 100);
      if (expectFixed) {
        assert.equal(snapshotFirstConnecting, false); assert.equal(terminalConnecting, false);
        assert.equal(stopVisible, false); assert.equal(tailSendDisabled, true);
        assert.equal(stayedUp, true); assert.equal(followedBottom, true);
        assert(!metrics.kinoStates.some((x) => x.stage === "tail" && x.state === "look"));
      }
      console.log(JSON.stringify(results.at(-1)));
    } finally { await context.close(); api.close(); }
  }
  await mkdir(dirname(resolve(outputPath)), { recursive: true });
  await writeFile(outputPath, JSON.stringify({ environment: { browser: browser.version(), cpuRate: 4, viewport: "1440x900",
    fixture: "1/20/60 completed answers, fixed Markdown; independent Node SSE at 33ms, 100 deltas; whole-conversation snapshot every second",
    inputMetric: "Synthetic input dispatch to next rAF observation; not physical paint or field INP",
    domMetric: "Transport receipt to existing committed-DOM rAF observer; simulated source, not certified provider FTT" }, results }, null, 2) + "\n");
} finally { await browser.close(); }

import { expect, test } from "@playwright/test";
import { writeFile } from "node:fs/promises";

declare global {
  interface Window {
    chatEvidence: Record<string, unknown>[];
    chatEmit?: (event: Record<string, unknown> | null) => void;
  }
}

test("renders growing snapshots before model completion and records distinct receipt/render evidence", async ({ page }, testInfo) => {
  await page.addInitScript(() => {
    window.chatEvidence = [];
    window.addEventListener("seekino:chat-delivery", (event) => window.chatEvidence.push((event as CustomEvent).detail));
    const original = window.fetch;
    window.fetch = async (...args) => {
      if (String(args[0]).includes("/api/agent/messages/send?")) {
        return new Response(new ReadableStream({ start(controller) {
          window.chatEmit = (event) => event === null ? controller.close() : controller.enqueue(
            new TextEncoder().encode(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`),
          );
        } }), { headers: { "content-type": "text/event-stream" } });
      }
      return original(...args);
    };
  });
  const conversation = { conversationId: "stream-conv", tenantId: "local-default", title: "流式验收",
    messageCount: 0, lastMessagePreview: null, lastMessageAt: null, createdAt: 0, updatedAt: 0 };
  await page.route("**/healthz", (route) => route.fulfill({ json: { status: "ok" } }));
  let creations = 0;
  await page.route("**/api/**", (route) => {
    if (new URL(route.request().url()).pathname === "/api/agent/conversations/create") creations++;
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) return route.continue();
    if (path === "/api/auth/me") return route.fulfill({ json: { userId: "local", email: null, isGuest: false,
      tenantId: "local-default", publicTenantId: "public", mode: "local", csrfToken: "",
      permissions: { canUpload: true, canManage: true, canConfigure: true } } });
    if (path === "/api/system/runtime-config") return route.fulfill({ json: { displayMode: "release" } });
    if (path === "/api/agent/messages/options") return route.fulfill({ json: Object.fromEntries(
      ["topK", "maxLoopTurns", "maxModelCallsPerTurn", "maxToolCallsPerTurn"].map((name) => [name, { defaultValue: 3, minimum: 1, maximum: 30 }]),
    ) });
    if (path === "/api/agent/conversations/create" || path === "/api/agent/conversations/get") return route.fulfill({ json: conversation });
    if (path === "/api/agent/messages/events") return route.fulfill({ contentType: "text/event-stream", body: ": ready\n\n" });
    return route.fulfill({ json: { items: [], total: 0, hasMore: false, nextCursor: null } });
  });
  await page.goto("/search");
  const composer = page.getByRole("textbox", { name: "向 Seekino 助手提问" });
  await expect(page.getByRole("heading", { name: "今天，想发现什么？" })).toBeVisible();
  expect(creations).toBe(0);
  const originalComposer = await composer.elementHandle();
  await composer.fill("请逐步解释矩阵");
  await composer.press("Enter");
  await expect.poll(() => page.evaluate(() => Boolean(window.chatEmit))).toBe(true);
  const base = { conversationId: "stream-conv", messageId: "assistant", occurredAt: 1 };
  const message = { ...base, sequenceNo: 1, role: "ASSISTANT", status: "PROCESSING", version: 1,
    seriesIds: [], content: "", phase: null, reference: null, attachments: [], modelName: "mock",
    searchResult: null, renderBlocks: [], timing: null, lastError: null, createdAt: 1, updatedAt: 1 };
  const pair = { assistantMessage: message, userMessage: { ...message, messageId: "user", role: "USER", status: "COMPLETED", content: "请逐步解释矩阵" } };
  await page.evaluate((event) => window.chatEmit!(event), { ...base, ...pair, sequence: 1, type: "answer.started" });
  await expect(page.locator("main[data-draft]")).toHaveAttribute("data-draft", "false");
  expect(await originalComposer!.evaluate((node) => node === document.querySelector('textarea[aria-label="向 Seekino 助手提问"]'))).toBe(true);
  await expect(composer).toBeFocused();
  expect(creations).toBe(1);
  const source = { origin: "provider_delta", callId: "mock-call", deltaSequence: 1, modelFirstTextMs: 20,
    modelElapsedMs: 20, providerReceivedMonotonicMs: 100, serverPublishedMonotonicMs: 101, beforeModelCompleted: true };
  for (const [index, content] of ["矩阵把多个数按行列组织。", "矩阵把多个数按行列组织。向量可以看作只有一列的矩阵。"].entries()) {
    await page.evaluate((event) => window.chatEmit!(event), { ...base, ...pair, sequence: index + 2, type: "answer.snapshot",
      assistantMessage: { ...message, content, version: index + 2, renderBlocks: [{ type: "markdown", markdown: content }] },
      textStream: { ...source, deltaSequence: index + 1 } });
    await expect(page.getByText(content, { exact: true })).toBeVisible();
    await expect.poll(() => page.evaluate(() => window.chatEvidence.filter((e) => e.stage === "text.rendered").length)).toBe(index + 1);
  }
  await page.screenshot({ path: testInfo.outputPath("before-model-completion.png") });
  await page.evaluate((event) => window.chatEmit!(event), { ...base, sequence: 4, type: "answer.model.completed", callId: "mock-call" });
  await page.evaluate((event) => { window.chatEmit!(event); window.chatEmit!(null); }, { ...base, ...pair,
    sequence: 5, type: "answer.completed", assistantMessage: { ...message, status: "COMPLETED", version: 4,
      content: "矩阵把多个数按行列组织。向量可以看作只有一列的矩阵。" } });
  const evidence = await page.evaluate(() => window.chatEvidence);
  expect(evidence.at(-1)).toMatchObject({ status: "completed", fttMs: null, fttValidity: "requires_trace" });
  expect(evidence.filter((e) => e.stage === "text.rendered")).toHaveLength(2);
  await writeFile(testInfo.outputPath("browser-simulated.json"), JSON.stringify(evidence, null, 2));
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(page.getByRole("heading", { name: "今天，想发现什么？" })).toBeVisible();
  expect(creations).toBe(1);
  await testInfo.attach("simulated-browser-delivery", { body: JSON.stringify(evidence, null, 2), contentType: "application/json" });
});

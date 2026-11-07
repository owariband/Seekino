import { expect, test } from "@playwright/test";

// Isolated browser fixtures: layout checks must never create real chat records.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("seekino-theme", "dark"));
  await page.route("**/healthz", (route) => route.fulfill({ json: { status: "ok" } }));
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) return route.continue();
    if (path === "/api/auth/me") return route.fulfill({ json: {
      userId: "local", email: null, isGuest: false, tenantId: "local-default", publicTenantId: "public",
      mode: "local", csrfToken: "", emailLoginEnabled: false,
      permissions: { canUpload: true, canManage: true, canConfigure: true },
    } });
    if (path === "/api/system/runtime-config") return route.fulfill({ json: { displayMode: "release" } });
    if (path === "/api/agent/messages/options") return route.fulfill({ json: {
      chatModels: [{ id: "default", label: "Default model" }, { id: "alternate", label: "Second model" }],
      ...Object.fromEntries(["topK", "maxLoopTurns", "maxModelCallsPerTurn", "maxToolCallsPerTurn"]
        .map(name => [name, { defaultValue: 4, minimum: 1, maximum: 10 }])),
    } });
    return route.fulfill({ json: { items: [], total: 0, hasMore: false, nextCursor: null } });
  });
});

test("opens settings above the draft, edits its model and returns to the same textarea", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const writes: string[] = [];
  page.on("request", r => { if (r.method() !== "GET" && r.url().includes("/api/agent/")) writes.push(r.url()); });
  await page.goto("/search");
  const input = page.getByRole("textbox", { name: "向 Seekino 助手提问" });
  await input.fill("这里有一段未发送的草稿");
  const original = await input.elementHandle();
  await input.evaluate((node: HTMLTextAreaElement) => node.setSelectionRange(2, 5));
  await page.getByRole("link", { name: "回答模型设置" }).click();
  const dialog = page.getByRole("dialog", { name: "设置", exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("radio", { name: /Second model/ }).check();
  await dialog.getByRole("button", { name: "观看与 Kino" }).click();
  const delay = dialog.getByRole("spinbutton");
  await delay.fill("1.5");
  await delay.press("Tab");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("seekino-watch-fade-delay"))).toBe("1.5");
  await dialog.getByRole("switch", { name: "发送问题时暂停视频" }).uncheck();
  await page.keyboard.press("Escape");
  await expect(page).toHaveURL(/\/search$/);
  await expect(input).toBeFocused();
  await expect(input).toHaveValue("这里有一段未发送的草稿");
  expect(await input.evaluate((node, previous) => node === previous, original)).toBe(true);
  expect(await input.evaluate((node: HTMLTextAreaElement) => [node.selectionStart, node.selectionEnd])).toEqual([2, 5]);
  await expect(page.getByRole("combobox", { name: "回答模型" })).toHaveValue("alternate");
  expect(writes).toEqual([]);
});

test("opens a standalone mobile settings directory with authentic device preferences", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 700 });
  await page.addInitScript(() => {
    localStorage.setItem("seekino-watch-fade-delay", "2.5");
    localStorage.setItem("seekino-watch-pause-on-send", "false");
  });
  await page.goto("/settings");
  const navigation = page.getByRole("navigation", { name: "设置分类" });
  await expect(navigation).toBeVisible();
  await navigation.getByRole("button", { name: "观看与 Kino" }).click();
  await expect(page.getByRole("switch", { name: "发送问题时暂停视频" })).not.toBeChecked();
  await expect(page.getByRole("spinbutton")).toHaveValue("2.5");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320);
  await page.getByRole("button", { name: "返回设置分类" }).click();
  await navigation.getByRole("button", { name: "回答模型" }).click();
  await expect(page.getByText("Second model", { exact: true })).toBeVisible();
  await expect(page.getByRole("radio", { name: /Second model/ })).toHaveCount(0);
});

test("keeps a bounded centered draft and an independently collapsible history rail", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  let creates = 0;
  page.on("request", (request) => { if (request.url().includes("/conversations/create")) creates++; });
  await page.goto("/search");
  const kino = page.getByRole("button", { name: "戳一戳 Kino" });
  await expect(kino).toBeVisible();
  await expect(page.getByText("描述记得的画面、台词或人物关系，一起把那一刻找回来。", { exact: true })).toBeVisible();
  await kino.click();
  await expect(kino.locator("svg")).toHaveAttribute("data-kino-state", /jump|happy/);
  const sidebar = page.getByRole("complementary", { name: "对话侧栏" });
  const input = page.getByRole("textbox", { name: "向 Seekino 助手提问" });
  const bounds = (await input.boundingBox())!;
  expect((await sidebar.boundingBox())!.width).toBe(248);
  expect(bounds.width).toBe(760);
  expect(bounds.y).toBeGreaterThan(300);
  expect(bounds.y).toBeLessThan(600);
  await input.fill("这段草稿保留在原处");
  await page.getByRole("button", { name: "收起对话记录" }).click();
  await expect(sidebar).toBeHidden();
  await page.getByRole("button", { name: "打开对话记录", exact: true }).click();
  await expect(sidebar).toBeVisible();
  await expect(input).toHaveValue("这段草稿保留在原处");
  await page.screenshot({ path: testInfo.outputPath("draft-desktop-dark.png") });
  await page.setViewportSize({ width: 1920, height: 1080 });
  expect((await input.boundingBox())!.width).toBe(760);
  await page.getByRole("button", { name: "切换至白天模式" }).click();
  await page.screenshot({ path: testInfo.outputPath("draft-wide-light.png") });
  expect(creates).toBe(0);
});

test("opens mobile history as a modal, traps focus and closes by Escape and backdrop", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/search");
  await expect(page.getByRole("button", { name: "戳一戳 Kino" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  const trigger = page.getByRole("button", { name: "打开对话记录", exact: true });
  await trigger.click();
  const drawer = page.getByRole("dialog", { name: "对话侧栏" });
  await expect(drawer).toBeVisible();
  for (let i = 0; i < 4; i++) await page.keyboard.press("Tab");
  expect(await drawer.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("history-mobile-dark.png") });
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(trigger).toBeFocused();
  await trigger.click();
  await page.mouse.click(360, 400);
  await expect(drawer).toBeHidden();
  await expect(trigger).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("draft-mobile-dark.png") });
});

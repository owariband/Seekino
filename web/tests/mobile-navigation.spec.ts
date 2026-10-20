import { expect, test, type Page } from "@playwright/test";

import type { AgentConversationView, AgentMessageView, AuthIdentity } from "../src/api/contracts";

async function mockService(page: Page, guest = true) {
  const identity: AuthIdentity = {
    userId: guest ? "mobile-guest" : "mobile-member", email: guest ? null : "viewer@example.com",
    isGuest: guest, tenantId: guest ? null : "personal", publicTenantId: "public",
    mode: "cloud", csrfToken: "",
    permissions: { canUpload: !guest, canManage: !guest, canConfigure: false },
  };
  const conversations: AgentConversationView[] = ["雨天演出片段", "角色关系梳理"].map((title, index) => ({
    conversationId: `mobile-history-${index + 1}`, tenantId: guest ? "public" : "personal", title,
    messageCount: 2, lastMessagePreview: `历史回答 ${index + 1}`, lastMessageAt: index + 1, createdAt: 0, updatedAt: index + 1,
  }));
  const messages: Record<string, AgentMessageView[]> = Object.fromEntries(conversations.map((conversation, index) => [
    conversation.conversationId, [`历史问题 ${index + 1}`, `历史回答 ${index + 1}`].map((content, sequenceNo) => ({
      messageId: `${conversation.conversationId}-${sequenceNo}`, conversationId: conversation.conversationId,
      seriesIds: [], sequenceNo, role: sequenceNo === 0 ? "USER" : "ASSISTANT", status: "COMPLETED",
      phase: null, version: 1, reference: null, attachments: [], content, modelName: null, searchResult: null,
      renderBlocks: [], timing: null, lastError: null, createdAt: 0, updatedAt: 0,
    })),
  ]));
  const emptyConversation: AgentConversationView = {
    conversationId: "mobile-conversation", tenantId: guest ? "public" : "personal", title: "新对话",
    messageCount: 0, lastMessagePreview: null, lastMessageAt: null, createdAt: 0, updatedAt: 0,
  };
  const requests: string[] = [];
  const unexpected: string[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/healthz", (route) => route.fulfill({ json: { status: "ok" } }));
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    requests.push(path);
    if (path === "/api/auth/me") return route.fulfill({ json: identity });
    if (path === "/api/system/runtime-config") return route.fulfill({ json: { displayMode: "release" } });
    if (["/api/series/list", "/api/series/public"].includes(path)) {
      return route.fulfill({ json: { items: [], total: 0, hasMore: false, nextCursor: null } });
    }
    if (path === "/api/agent/conversations/list") return route.fulfill({ json: {
      items: conversations, total: conversations.length, hasMore: false, nextCursor: null,
    } });
    if (path === "/api/agent/conversations/create") return route.fulfill({ json: emptyConversation });
    if (path === "/api/agent/conversations/get") {
      const conversation = [...conversations, emptyConversation].find((item) => item.conversationId === url.searchParams.get("conversationId"));
      if (conversation) return route.fulfill({ json: conversation });
    }
    if (path === "/api/agent/messages/list") {
      const items = messages[url.searchParams.get("conversationId") ?? ""] ?? [];
      return route.fulfill({ json: { items, total: items.length, hasMore: false, nextCursor: null } });
    }
    if (path === "/api/agent/messages/events") return route.fulfill({
      contentType: "text/event-stream", body: ": ready\n\n",
    });
    unexpected.push(path);
    return route.fulfill({ status: 404, json: { message: "Unmocked mobile navigation test API" } });
  });
  return { conversations, requests, unexpected, errors };
}

test.use({ viewport: { width: 390, height: 844 } });

test("keeps the mobile composer compact, grows for long drafts and shrinks after clearing", async ({ page }, testInfo) => {
  const mock = await mockService(page);
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/search");
    const input = page.getByRole("textbox", { name: "向 Seekino 助手提问" });
    const composer = page.locator("form").filter({ has: input });
    await expect(input).toBeVisible();
    await expect.poll(() => composer.evaluate((element) => element.getBoundingClientRect().height))
      .toBeLessThanOrEqual(width === 320 ? 130 : 105);
    const emptyHeight = await composer.evaluate((element) => element.getBoundingClientRect().height);
    await input.fill(Array.from({ length: 12 }, (_, index) => `第 ${index + 1} 行待发送内容`).join("\n"));
    await expect.poll(() => composer.evaluate((element) => element.getBoundingClientRect().height))
      .toBeGreaterThan(emptyHeight + 24);
    const expandedInputHeight = await input.evaluate((element) => element.getBoundingClientRect().height);
    expect(expandedInputHeight).toBeLessThanOrEqual(140);
    testInfo.annotations.push({ type: "composer dimensions", description: `${width}px viewport: empty composer ${emptyHeight}px, expanded textarea ${expandedInputHeight}px` });
    await testInfo.attach(`composer-${width}`, { body: JSON.stringify({ viewportWidth: width, emptyHeight, expandedInputHeight }), contentType: "application/json" });
    expect(await input.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
    await input.fill("");
    await expect.poll(() => composer.evaluate((element) => element.getBoundingClientRect().height))
      .toBeLessThanOrEqual(emptyHeight + 1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  }
  await page.screenshot({ path: testInfo.outputPath("chat-mobile-390.png") });
  expect(mock.unexpected).toEqual([]);
  expect(mock.errors).toEqual([]);
});

test("keeps focus in the drawer and preserves the draft when dismissed", async ({ page }, testInfo) => {
  const mock = await mockService(page);
  await page.goto("/search");
  const input = page.getByRole("textbox", { name: "向 Seekino 助手提问" });
  await input.fill("保留这段还没发送的草稿");
  const trigger = page.getByRole("button", { name: "打开侧边栏", exact: true });
  const drawer = page.getByRole("dialog", { name: "手机导航", exact: true });
  await trigger.click();
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("navigation", { name: "对话记录", exact: true }).getByRole("link")).toHaveCount(2);
  await page.screenshot({ path: testInfo.outputPath("drawer-mobile-390.png") });
  const controlCount = await drawer.locator("a, button, select").count();
  for (let index = 0; index <= controlCount; index += 1) {
    await page.keyboard.press("Tab");
    expect(await drawer.evaluate((element) =>
      document.activeElement === document.body || element.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(trigger).toBeFocused();
  await expect(input).toHaveValue("保留这段还没发送的草稿");
  await trigger.click();
  await expect(drawer).toBeVisible();
  await page.mouse.click(389, 200);
  await expect(drawer).toBeHidden();
  await expect(input).toHaveValue("保留这段还没发送的草稿");
  await trigger.click();
  await drawer.getByRole("button", { name: "关闭侧边栏", exact: true }).click();
  await expect(drawer).toBeHidden();
  await trigger.click();
  await expect(drawer).toBeVisible();
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(drawer).toBeHidden();
  await expect(trigger).toBeHidden();
  await page.getByRole("button", { name: "打开账号菜单", exact: true }).click();
  await expect(page.getByRole("region", { name: "当前账号", exact: true })).toBeVisible();
  await expect(input).toHaveValue("保留这段还没发送的草稿");
  expect(mock.unexpected).toEqual([]);
  expect(mock.errors).toEqual([]);
});

test("keeps watch as the home page and navigates between watch and the assistant", async ({ page }) => {
  const mock = await mockService(page);
  await page.goto("/");
  await expect(page).toHaveURL(/\/watch$/);
  await expect(page.getByRole("link", { name: "搜索名场面", exact: true })).toHaveAttribute("href", "/search");
  const trigger = page.getByRole("button", { name: "打开侧边栏", exact: true });
  const drawer = page.getByRole("dialog", { name: "手机导航", exact: true });
  await trigger.click();
  const navigation = drawer.getByRole("navigation", { name: "主导航", exact: true });
  await expect(navigation.getByRole("link").first()).toHaveAttribute("href", "/watch");
  await navigation.getByRole("link", { name: "Seekino 助手", exact: true }).click();
  await expect(page).toHaveURL(/\/search$/);
  await expect(drawer).toBeHidden();
  const input = page.getByRole("textbox", { name: "向 Seekino 助手提问" });
  await expect(input).toBeVisible();
  await expect(page.getByRole("heading", { name: "Seekino 助手", exact: true })).toBeHidden();
  await page.getByRole("button", { name: "搜索名场面", exact: true }).click();
  await expect(input).toBeFocused();
  await trigger.click();
  await navigation.getByRole("link", { name: "观看", exact: true }).click();
  await expect(page).toHaveURL(/\/watch$/);
  await expect(drawer).toBeHidden();
  await page.getByRole("link", { name: "搜索名场面", exact: true }).click();
  await expect(page).toHaveURL(/\/search$/);
  await expect(input).toBeFocused();
  expect(mock.unexpected).toEqual([]);
  expect(mock.errors).toEqual([]);
});

test("shows guest sign-in and only exposes management tabs for a private source", async ({ page, context }) => {
  const guest = await mockService(page);
  await page.goto("/watch");
  await page.getByRole("button", { name: "打开侧边栏", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "手机导航", exact: true });
  await expect(drawer.getByRole("link", { name: "设置", exact: true })).toBeVisible();
  for (const name of ["资料库", "解析任务", "模型消耗"]) {
    await expect(drawer.getByRole("link", { name, exact: true })).toHaveCount(0);
  }
  const signIn = drawer.getByRole("button", { name: "登录", exact: true });
  const theme = drawer.getByRole("button", { name: /^切换至(?:黑夜|白天)模式$/ });
  await expect(signIn).toContainText("未登录");
  const signInBounds = await signIn.evaluate((element) => element.getBoundingClientRect().toJSON());
  const themeBounds = await theme.evaluate((element) => element.getBoundingClientRect().toJSON());
  expect(signInBounds.right).toBeLessThanOrEqual(themeBounds.left);
  expect(signInBounds.top).toBeGreaterThan(844 * 0.7);
  expect(Math.abs(signInBounds.bottom - themeBounds.bottom)).toBeLessThanOrEqual(8);
  const initialTheme = await page.locator("html").getAttribute("data-theme");
  await theme.click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", initialTheme === "light" ? "dark" : "light");
  await signIn.click();
  await expect(drawer).toBeHidden();
  const login = page.getByRole("dialog", { name: "邮箱登录", exact: true });
  await expect(login).toBeVisible();
  await expect(login.getByLabel("邮箱", { exact: true })).toBeFocused();
  await login.getByRole("button", { name: "关闭登录", exact: true }).click();

  const managerPage = await context.newPage();
  const member = await mockService(managerPage, false);
  await managerPage.goto("/watch");
  await managerPage.getByRole("button", { name: "打开侧边栏", exact: true }).click();
  const managerDrawer = managerPage.getByRole("dialog", { name: "手机导航", exact: true });
  for (const name of ["资料库", "解析任务", "模型消耗", "设置"]) {
    await expect(managerDrawer.getByRole("link", { name, exact: true })).toBeVisible();
  }
  await expect(managerDrawer.getByRole("button", { name: "退出", exact: true })).toBeVisible();
  await managerPage.setViewportSize({ width: 320, height: 568 });
  const managerHistory = managerDrawer.getByRole("navigation", { name: "对话记录", exact: true });
  await expect.poll(() => managerHistory.evaluate((element) => element.getBoundingClientRect().height))
    .toBeGreaterThanOrEqual(120);
  const firstHistory = managerHistory.getByRole("link").first();
  await firstHistory.scrollIntoViewIfNeeded();
  await expect(firstHistory).toBeInViewport();
  const signOut = managerDrawer.getByRole("button", { name: "退出", exact: true });
  await signOut.scrollIntoViewIfNeeded();
  await expect(signOut).toBeInViewport();
  expect(guest.unexpected).toEqual([]);
  expect(member.unexpected).toEqual([]);
  expect(guest.errors).toEqual([]);
  expect(member.errors).toEqual([]);
});

test("opens and switches drawer history without creating a blank conversation or losing the draft", async ({ page }) => {
  const mock = await mockService(page);
  await page.goto("/watch");
  const trigger = page.getByRole("button", { name: "打开侧边栏", exact: true });
  const drawer = page.getByRole("dialog", { name: "手机导航", exact: true });
  const history = drawer.getByRole("navigation", { name: "对话记录", exact: true });
  await trigger.click();
  for (const conversation of mock.conversations) {
    await expect(history.getByRole("link", { name: `打开对话 ${conversation.title}`, exact: true })).toBeVisible();
  }
  await history.getByRole("link", { name: `打开对话 ${mock.conversations[0].title}`, exact: true }).click();
  await expect(page).toHaveURL(/\/search(?:\?|$)/);
  await expect(drawer).toBeHidden();
  const thread = page.getByRole("region", { name: "与 Seekino 助手对话", exact: true });
  await expect(thread.getByText("历史回答 1", { exact: true })).toBeVisible();
  await expect(thread.getByText("历史回答 2", { exact: true })).toHaveCount(0);
  expect(mock.requests).not.toContain("/api/agent/conversations/create");
  const input = page.getByRole("textbox", { name: "向 Seekino 助手提问" });
  await input.fill("切换历史后继续编辑的草稿");
  await trigger.click();
  await history.getByRole("link", { name: `打开对话 ${mock.conversations[1].title}`, exact: true }).click();
  await expect(drawer).toBeHidden();
  await expect(thread.getByText("历史回答 2", { exact: true })).toBeVisible();
  await expect(thread.getByText("历史回答 1", { exact: true })).toHaveCount(0);
  await expect(input).toHaveValue("切换历史后继续编辑的草稿");

  await trigger.click();
  await expect(history.getByRole("link", { name: `打开对话 ${mock.conversations[1].title}`, exact: true })).toBeVisible();
  const previousLists = mock.requests.filter((path) => path === "/api/agent/conversations/list").length;
  await drawer.getByRole("button", { name: "关闭侧边栏", exact: true }).click();
  mock.conversations[1].title = "更新后的角色关系梳理";
  await trigger.click();
  await expect(history.getByRole("link", { name: "打开对话 更新后的角色关系梳理", exact: true })).toBeVisible();
  expect(mock.requests.filter((path) => path === "/api/agent/conversations/list").length).toBeGreaterThan(previousLists);
  await history.getByRole("link", { name: `打开对话 ${mock.conversations[0].title}`, exact: true }).click();
  await expect(thread.getByText("历史回答 1", { exact: true })).toBeVisible();
  await expect(input).toHaveValue("切换历史后继续编辑的草稿");
  await page.getByLabel("打开对话记录", { exact: true }).click();
  await page.getByRole("region", { name: "对话记录", exact: true })
    .getByRole("button", { name: /^打开对话 更新后的角色关系梳理/ }).click();
  await expect(thread.getByText("历史回答 2", { exact: true })).toBeVisible();
  await expect(input).toHaveValue("切换历史后继续编辑的草稿");
  await page.reload();
  await expect(thread.getByText("历史回答 2", { exact: true })).toBeVisible();
  await expect(thread.getByText("历史回答 1", { exact: true })).toHaveCount(0);
  expect(mock.requests).not.toContain("/api/agent/conversations/create");
  expect(mock.unexpected).toEqual([]);
  expect(mock.errors).toEqual([]);
});

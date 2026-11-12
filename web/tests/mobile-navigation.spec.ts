import { configuredModels } from "../src/test/personalModelFixtures";
import { expect, test, type Locator, type Page } from "@playwright/test";

import type { AgentConversationView, AgentMessageView, AuthIdentity, DisplayMode, SeriesView, VideoSummary } from "../src/api/contracts";

async function mockService(page: Page, guest = true, { displayMode = "release", series = [], emailLoginEnabled = true }: { displayMode?: DisplayMode; series?: SeriesView[]; emailLoginEnabled?: boolean } = {}) {
  const identity: AuthIdentity = {
    userId: guest ? "mobile-guest" : "mobile-member", email: guest ? null : "viewer@example.com",
    isGuest: guest, tenantId: guest ? null : "personal", publicTenantId: "public",
    mode: "cloud", csrfToken: "", emailLoginEnabled,
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
  const emptyConversation: AgentConversationView = { modelId: configuredModels.defaultModelId,
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
    if (path === "/api/system/runtime-config") return route.fulfill({ json: { displayMode } });
    if (["/api/series/list", "/api/series/public"].includes(path)) {
      return route.fulfill({ json: { items: series, total: series.length, hasMore: false, nextCursor: null } });
    }
    if (path === "/api/series/get") {
      const selected = series.find((item) => item.seriesId === url.searchParams.get("seriesId"));
      if (selected) return route.fulfill({ json: selected });
    }
    if (path === "/api/agent/model-connections") return route.fulfill({ json: configuredModels });
    if (path === "/api/agent/messages/options") return route.fulfill({ json: {
      topK: { defaultValue: 10, minimum: 1, maximum: 20 },
      maxLoopTurns: { defaultValue: 10, minimum: 1, maximum: 100 },
      maxModelCallsPerTurn: { defaultValue: 6, minimum: 1, maximum: 12 },
      maxToolCallsPerTurn: { defaultValue: 24, minimum: 1, maximum: 48 },
    } });
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

const compactSeries: SeriesView = {
  seriesId: "compact-series", tenantId: "local-default", name: "紧凑选集测试系列",
  description: "用于验证手机选集行高与整行链接。",
  visibility: "private", access: "member", publisher: null, status: "READY",
  videoCount: 13, readyVideoCount: 11, failedVideoCount: 0, pendingUploadCount: 0,
  cover: { imageId: "compact-cover", url: "/media-placeholder.svg", expiresAt: null },
  lastSearch: null, createdAt: 0, updatedAt: 0, transcriptLanguageHint: null,
  importantExtraData: null, characterBible: [], pendingUploads: [],
  videos: Array.from({ length: 13 }, (_, index): VideoSummary => {
    const order = index + 1;
    const unavailable = order === 5 || order === 11;
    return {
      videoId: `compact-ep-${order}`, tenantId: "local-default", seriesId: "compact-series",
      episodeOrder: order, uploadSequence: order,
      displayName: order === 2 ? "雨夜里那场没有说完的演出与沿着河岸寻找走失猫咪的漫长夜晚"
        : order === 3 ? "A deliberately long English episode title that should wrap inside the compact row without overflowing the viewport"
        : order === 8 ? null : `第 ${order} 集 · 测试标题`,
      originalFilename: `compact-episode-${order}.mp4`, totalBytes: 1_000, durationSeconds: 60_000 + order * 1_000,
      uploadStatus: "COMPLETED", parseStatus: "SUCCEEDED", uploadProgress: 100, parseProgress: 100,
      indexStatus: "READY", playbackStatus: unavailable ? "PREPARING" : "READY",
      playbackUrl: unavailable ? null : `/api/videos/content?videoId=compact-ep-${order}`,
      playbackUrlExpiresAt: null,
      thumbnail: { imageId: `compact-thumb-${order}`, url: "/media-placeholder.svg", expiresAt: null },
      lastError: null,
    };
  }),
};

test.use({ viewport: { width: 390, height: 844 } });

for (const width of [320, 390]) {
  test(`keeps the docs entry visible, labelled and tappable at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    const mock = await mockService(page);
    await page.goto("/watch");
    const header = page.getByRole("banner", { name: "全站导航", exact: true });
    const docsLink = header.getByRole("link", { name: "使用文档", exact: true });
    await expect(docsLink).toBeVisible();
    const linkBounds = (await docsLink.boundingBox())!;
    const labelBounds = (await docsLink.getByText("文档", { exact: true }).boundingBox())!;
    expect(linkBounds.width).toBeGreaterThanOrEqual(44);
    expect(linkBounds.height).toBeGreaterThanOrEqual(44);
    expect(labelBounds.width).toBeGreaterThan(1);
    expect(labelBounds.height).toBeGreaterThan(1);
    expect(linkBounds.x + linkBounds.width).toBeLessThanOrEqual(width + 1);
    for (const control of [header.getByRole("button", { name: "打开侧边栏", exact: true }), header.getByRole("link", { name: "搜索名场面", exact: true })]) {
      const bounds = (await control.boundingBox())!;
      expect(bounds.width).toBeGreaterThanOrEqual(44);
      expect(bounds.height).toBeGreaterThanOrEqual(44);
    }
    expect(await header.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    testInfo.annotations.push({ type: "docs entry dimensions", description: `${width}px viewport: link ${linkBounds.width}×${linkBounds.height}, label ${labelBounds.width}×${labelBounds.height}` });
    await testInfo.attach(`docs-entry-${width}`, { body: JSON.stringify({ width, link: linkBounds, label: labelBounds }), contentType: "application/json" });
    await docsLink.click();
    await expect(page).toHaveURL(/\/docs$/);
    await expect(page.getByRole("heading", { level: 1, name: "41 小时的课，你要的只是其中一段", exact: true })).toBeVisible();
    expect(mock.unexpected).toEqual([]);
    expect(mock.errors).toEqual([]);
  });
}

for (const width of [320, 390, 760]) {
  test(`lays out 13 episodes as compact full-row links at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const mock = await mockService(page, true, { series: [compactSeries] });
    await page.goto("/series/compact-series?tenantId=local-default");
    const region = page.getByRole("region", { name: "系列选集", exact: true });
    await expect(region.getByRole("heading", { level: 2, name: /^剧集/ })).toBeVisible();
    const rows = region.getByRole("listitem");
    const links = region.getByRole("link");
    await expect(rows).toHaveCount(13);
    await expect(links).toHaveCount(11);
    await expect(region.getByText("暂不可播放", { exact: true })).toHaveCount(2);
    const rowBoxes = await rows.evaluateAll((nodes) => nodes.map((node) => {
      const box = node.getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top, width: box.width, height: box.height };
    }));
    expect(Math.max(...rowBoxes.map((box) => box.height))).toBeLessThanOrEqual(100);
    expect(Math.max(...rowBoxes.map((box) => box.right))).toBeLessThanOrEqual(width + 1);
    const firstLink = links.first();
    const linkBounds = (await firstLink.boundingBox())!;
    const rowBounds = await firstLink.evaluate((link) => {
      const row = link.closest("li")!.getBoundingClientRect();
      return { width: row.width, height: row.height };
    });
    expect(Math.abs(linkBounds.width - rowBounds.width)).toBeLessThanOrEqual(1);
    expect(Math.abs(linkBounds.height - rowBounds.height)).toBeLessThanOrEqual(1);
    const firstRow = (await rows.first().boundingBox())!;
    const fifthRow = (await rows.nth(4).boundingBox())!;
    expect(fifthRow.y + fifthRow.height - firstRow.y).toBeLessThanOrEqual(600);
    const preview = (await firstLink.locator("img").first().boundingBox())!;
    expect(preview.width).toBeGreaterThanOrEqual(112);
    expect(preview.width).toBeLessThanOrEqual(128.5);
    expect(Math.abs(preview.width / preview.height - 16 / 9)).toBeLessThan(0.05);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    testInfo.annotations.push({ type: "episode row dimensions", description: `${width}px viewport: max row height ${Math.max(...rowBoxes.map((box) => box.height))}px, five rows ${fifthRow.y + fifthRow.height - firstRow.y}px` });
    await testInfo.attach(`episode-rows-${width}`, { body: JSON.stringify({ width, rowBoxes }), contentType: "application/json" });
    expect(mock.unexpected).toEqual([]);
    expect(mock.errors).toEqual([]);
  });
}

test("keeps the original four-column episode grid at 1280px", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const mock = await mockService(page, true, { series: [compactSeries] });
  await page.goto("/series/compact-series?tenantId=local-default");
  const region = page.getByRole("region", { name: "系列选集", exact: true });
  const rows = region.getByRole("listitem");
  await expect(rows).toHaveCount(13);
  await expect(region.getByText("暂不可播放", { exact: true })).toHaveCount(2);
  const rowBoxes = await rows.evaluateAll((nodes) => nodes.map((node) => {
    const box = node.getBoundingClientRect();
    return { left: box.left, top: box.top, width: box.width };
  }));
  const firstRow = rowBoxes.slice(0, 4);
  expect(new Set(firstRow.map((box) => Math.round(box.top))).size).toBe(1);
  expect(new Set(firstRow.map((box) => Math.round(box.left))).size).toBe(4);
  const firstDesktopRow = rowBoxes[0]!;
  const fifthDesktopRow = rowBoxes[4]!;
  expect(fifthDesktopRow.top).toBeGreaterThan(firstDesktopRow.top + 40);
  expect(firstDesktopRow.width).toBeGreaterThan(180);
  expect(mock.unexpected).toEqual([]);
  expect(mock.errors).toEqual([]);
});

for (const width of [390, 1280]) {
  test(`explains unavailable email login without requests at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    const mock = await mockService(page, true, { emailLoginEnabled: false });
    await page.goto("/settings?section=connection");
    await expect(page.getByText("游客模式", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: width < 600 ? "打开侧边栏" : "打开账号菜单", exact: true }).click();
    const account = page.getByRole("region", { name: "当前账号", exact: true });
    await expect(account.getByText("游客模式", { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`account-${width}.png`) });
    await account.getByRole("button", { name: "登录 / 注册（暂未开放）", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "登录与注册暂未开放", exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("textbox")).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "继续体验", exact: true })).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath(`login-unavailable-${width}.png`) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await dialog.getByRole("button", { name: "继续体验", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    if (width < 600) await page.getByRole("button", { name: "打开侧边栏", exact: true }).click();
    await page.getByRole("button", { name: "个人上传暂未开放", exact: true }).click();
    await expect(dialog).toBeVisible();
    expect(mock.requests.filter((path) => path.startsWith("/api/auth/email/"))).toEqual([]);
    expect(mock.unexpected).toEqual([]);
    expect(mock.errors).toEqual([]);
  });
}

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
  const scripts: string[] = [];
  page.on("request", (request) => {
    if (request.resourceType() === "script") scripts.push(new URL(request.url()).pathname);
  });
  let releaseSearch!: () => void;
  const searchDownload = new Promise<void>((resolve) => { releaseSearch = resolve; });
  await page.route(/\/assets\/SearchPage-[^/]+\.js$/, async (route) => {
    await searchDownload;
    await route.continue();
  });
  await page.goto("/");
  await expect(page).toHaveURL(/\/watch$/);
  await expect(page.getByRole("link", { name: "搜索名场面", exact: true })).toHaveAttribute("href", "/search");
  expect(scripts.some((path) => /\/(?:SearchPage|WatchPage|SeriesPage|LibraryPage|SettingsPage|TasksPage|UsagePage)-/.test(path))).toBe(false);
  const trigger = page.getByRole("button", { name: "打开侧边栏", exact: true });
  const drawer = page.getByRole("dialog", { name: "手机导航", exact: true });
  await trigger.click();
  const navigation = drawer.getByRole("navigation", { name: "主导航", exact: true });
  await expect(navigation.getByRole("link").first()).toHaveAttribute("href", "/watch");
  await expect(drawer.getByRole("link", { name: `打开对话 ${mock.conversations[0].title}`, exact: true })).toBeVisible();
  expect(scripts.some((path) => path.includes("/SearchPage-"))).toBe(false);
  await navigation.getByRole("link", { name: "Seekino 助手", exact: true }).click();
  await expect(page).toHaveURL(/\/search$/);
  await expect(drawer).toBeHidden();
  const input = page.getByRole("textbox", { name: "向 Seekino 助手提问" });
  try {
    await expect.poll(() => scripts.some((path) => path.includes("/SearchPage-"))).toBe(true);
    await expect(input).toHaveCount(0);
    await trigger.click();
    await expect(drawer).toBeVisible();
    await drawer.getByRole("button", { name: "关闭侧边栏", exact: true }).click();
  } finally {
    releaseSearch();
  }
  await expect(input).toBeVisible();
  const assistantHeading = page.getByRole("heading", { name: "Seekino 助手", exact: true });
  await expect(assistantHeading).toHaveClass(/sr-only/);
  expect((await assistantHeading.boundingBox())!.height).toBeLessThanOrEqual(1);
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

test("fades the home artwork with the page and respects reduced motion", async ({ page }) => {
  const series: SeriesView = {
    seriesId: "theme-series", tenantId: "personal", name: "系列甲", description: null,
    visibility: "private", access: "member", publisher: null, status: "READY",
    videoCount: 1, readyVideoCount: 1, failedVideoCount: 0, pendingUploadCount: 0,
    cover: { imageId: "cover", url: "/media-placeholder.svg", expiresAt: null },
    lastSearch: null, createdAt: 0, updatedAt: 0, transcriptLanguageHint: null,
    importantExtraData: null, characterBible: [], videos: [], pendingUploads: [],
  };
  await mockService(page, false, { series: [series] });
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "no-preference" });
  await page.goto("/watch");
  const covers = page.locator('main img[src="/media-placeholder.svg"]');
  await expect(covers).toHaveCount(2);
  const appearance = () => covers.evaluateAll(images => ({
    canvas: getComputedStyle(document.body).backgroundColor,
    filters: images.map(image => getComputedStyle(image.parentElement!).filter),
  }));
  const day = await appearance();
  await page.getByRole("button", { name: "打开侧边栏" }).click();
  const toggle = page.getByRole("dialog", { name: "手机导航" }).getByRole("button", { name: /^切换至(?:黑夜|白天)模式$/ });

  // Sample the same point on slow and fast machines.
  await toggle.evaluate(async button => {
    (button as HTMLButtonElement).click();
    await new Promise(requestAnimationFrame);
    const fade = document.documentElement.getAnimations().find(animation => animation instanceof CSSTransition);
    if (!fade?.effect) throw new Error("Expected a theme transition");
    fade.pause();
    fade.currentTime = Number(fade.effect.getTiming().duration) / 2;
  });
  const midway = await appearance();
  await page.evaluate(() => document.documentElement.getAnimations().forEach(animation => animation.finish()));
  const night = await appearance();
  expect(night.filters).toEqual(["brightness(0.84)", "brightness(0.84)"]);
  expect(midway.canvas).not.toBe(day.canvas);
  expect(midway.canvas).not.toBe(night.canvas);
  expect(midway.filters[0]).toBe(midway.filters[1]);
  expect(midway.filters[0]).not.toBe(day.filters[0]);
  expect(midway.filters[0]).not.toBe(night.filters[0]);

  await page.emulateMedia({ reducedMotion: "reduce" });
  await toggle.click();
  expect(await appearance()).toEqual(day);
  expect(await page.locator("html").evaluate(root => root.getAnimations().length)).toBe(0);
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

for (const touch of [false, true]) {
  test.describe(touch ? "mobile touch" : "desktop mouse", () => {
    test.use({ viewport: { width: touch ? 390 : 1280, height: touch ? 844 : 900 }, hasTouch: touch });

    test("dismisses chat popovers outside while preserving internal choices", async ({ page }) => {
      const series = ["系列甲", "系列乙"].map((name, index): SeriesView => ({
        seriesId: `popover-series-${index}`, tenantId: "personal", name, description: null,
        visibility: "private", access: "member", publisher: null, status: "READY",
        videoCount: 1, readyVideoCount: 1, failedVideoCount: 0, pendingUploadCount: 0,
        cover: null, lastSearch: null, createdAt: 0, updatedAt: 0, transcriptLanguageHint: null,
        importantExtraData: null, characterBible: [], videos: [], pendingUploads: [],
      }));
      const mock = await mockService(page, false, { displayMode: "debug", series });
      const activate = (target: Locator) => touch ? target.tap() : target.click();
      const outside = page.getByRole("banner", { name: "全站导航", exact: true });
      const dismissOutside = () => touch ? outside.tap({ position: { x: 1, y: 1 } }) : outside.click({ position: { x: 1, y: 1 } });
      await page.goto("/search");
      const scopeTrigger = page.getByLabel("选择检索视频系列", { exact: true });
      const scope = page.getByRole("group", { name: "检索视频系列多选", exact: true });
      const settingsTrigger = page.getByLabel("配置本次回答", { exact: true });
      const settings = page.getByRole("group", { name: "本次回答设置", exact: true });
      const historyTrigger = page.getByLabel("打开对话记录", { exact: true });
      const history = page.getByRole("region", { name: "对话记录", exact: true });
      const collapseHistory = page.getByLabel("收起对话记录", { exact: true });
      if (!touch) await activate(collapseHistory);

      await activate(scopeTrigger);
      for (const name of ["系列甲", "系列乙"]) {
        const choice = scope.getByRole("checkbox", { name: new RegExp(`^${name}`) });
        await activate(choice);
        await expect(choice).toBeChecked();
        await expect(scope).toBeVisible();
      }
      await activate(scopeTrigger);
      await expect(scope).toBeHidden();
      await activate(scopeTrigger);
      await activate(settingsTrigger);
      await expect(scope).toBeHidden();
      await expect(settings).toBeVisible();
      await activate(settings.getByRole("button", { name: "自定义", exact: true }));
      await expect(settings.getByRole("slider", { name: "最多运行轮次", exact: true })).toBeVisible();
      await expect(settings).toBeVisible();
      await dismissOutside();
      await expect(settings).toBeHidden();

      await activate(settingsTrigger);
      await activate(historyTrigger);
      await expect(settings).toBeHidden();
      await expect(history).toBeVisible();
      await activate(history.getByText("对话记录", { exact: true }));
      await expect(history).toBeVisible();
      if (touch) {
        const drawer = page.getByRole("dialog", { name: "对话侧栏", exact: true });
        const bounds = (await drawer.boundingBox())!;
        await page.touchscreen.tap(bounds.x + bounds.width + 8, bounds.y + 50);
      } else {
        await dismissOutside();
        await expect(history).toBeVisible();
        await activate(collapseHistory);
      }
      await expect(history).toBeHidden();
      await activate(scopeTrigger);
      for (const name of ["系列甲", "系列乙"]) {
        await expect(scope.getByRole("checkbox", { name: new RegExp(`^${name}`) })).toBeChecked();
      }
      await dismissOutside();
      await expect(scope).toBeHidden();
      expect(mock.unexpected).toEqual([]);
      expect(mock.errors).toEqual([]);
    });
  });
}

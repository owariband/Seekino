import { expect, test, type Page } from "@playwright/test";

import type { AuthIdentity } from "../src/api/contracts";

async function mockService(page: Page, guest = false) {
  const identity: AuthIdentity = {
    userId: guest ? "locale-guest" : "local", email: null, isGuest: guest,
    tenantId: guest ? null : "local-default", publicTenantId: "public", mode: guest ? "cloud" : "local", csrfToken: "",
    permissions: { canUpload: !guest, canManage: !guest, canConfigure: !guest },
  };
  const requests: string[] = [];
  const unexpected: string[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/healthz", (route) => route.fulfill({ json: { status: "ok" } }));
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    requests.push(path);
    const emptyPage = { items: [], total: 0, hasMore: false, nextCursor: null };
    if (path === "/api/auth/me") return route.fulfill({ json: identity });
    if (path === "/api/system/runtime-config") return route.fulfill({ json: { displayMode: guest ? "debug" : "release" } });
    if (["/api/series/list", "/api/jobs/list", "/api/jobs/video-parse/list", "/api/agent/conversations/list", "/api/agent/messages/list"].includes(path)) {
      return route.fulfill({ json: emptyPage });
    }
    if (path === "/api/agent/conversations/create") return route.fulfill({ json: {
      conversationId: "locale-conversation", tenantId: "local-default", title: "原始对话名称",
      messageCount: 0, lastMessagePreview: null, lastMessageAt: null, createdAt: 0, updatedAt: 0,
    } });
    if (path === "/api/agent/messages/events") return route.fulfill({
      contentType: "text/event-stream", body: ": ready\n\n",
    });
    if (path === "/api/usage/overview") return route.fulfill({ json: {
      source: "local", status: "available", message: null,
      fromStartTime: 1791244800000, toStartTime: 1792368000000, fetchedAt: 1792368000000,
      truncated: false, totals: {
        callCount: 0, inputTokens: 0, outputTokens: 0, cachedInputTokens: 0,
        cacheWriteInputTokens: 0, reasoningTokens: 0, totalTokens: 0, knownTokens: 0, missingUsageCount: 0,
      }, models: [], runs: [],
    } });
    unexpected.push(path);
    return route.fulfill({ status: 404, json: { message: "Unmocked locale test API" } });
  });
  return { requests, unexpected, errors };
}

test("switches EN/ZH immediately and retains the choice after a reload", async ({ page }) => {
  const mock = await mockService(page);
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "设置", exact: true })).toBeVisible();
  await page.getByRole("radio", { name: "EN English" }).click();
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page).toHaveTitle("Seekino · Watch and Discover");
  expect(await page.evaluate(() => localStorage.getItem("anivag-locale"))).toBe("en");
  const requestsBeforeSwitch = mock.requests.length;
  await page.getByRole("radio", { name: "ZH 中文" }).click();
  await expect(page.getByRole("heading", { name: "设置", exact: true })).toBeVisible();
  await page.getByRole("radio", { name: "EN English" }).click();
  expect(mock.requests.length).toBe(requestsBeforeSwitch);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
  await expect(page.getByRole("radio", { name: "EN English" })).toHaveAttribute("aria-checked", "true");
  await page.screenshot({ path: "/private/tmp/anivag-locale-y_vrdu2o/settings-en-desktop.png", fullPage: true });
  expect(mock.unexpected).toEqual([]);
  expect(mock.errors).toEqual([]);
});

test("lets a guest choose English without exposing global configuration", async ({ page }) => {
  const mock = await mockService(page, true);
  await page.goto("/settings");
  await page.getByRole("radio", { name: "EN English" }).click();
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
  expect(mock.requests).not.toContain("/api/agent/global-settings");
  await expect(page.locator("form")).toHaveCount(0);
  await page.getByRole("button", { name: "Open account menu", exact: true }).click();
  await expect(page.getByRole("link", { name: "Settings", exact: true })).toBeVisible();
  expect(mock.unexpected).toEqual([]);
  expect(mock.errors).toEqual([]);
});

test("localizes a client-generated request failure in the page error state", async ({ page }) => {
  const mock = await mockService(page);
  await page.addInitScript(() => localStorage.setItem("anivag-locale", "en"));
  await page.route("**/api/usage/overview?**", (route) => route.fulfill({ status: 503, body: "Unavailable" }));
  await page.goto("/usage");
  await expect(page.getByRole("alert")).toContainText("Request failed (HTTP 503)");
  expect(await page.getByRole("alert").innerText()).not.toMatch(/[\p{Script=Han}]/u);
  expect(mock.unexpected).toEqual([]);
  expect(mock.errors).toEqual([]);
});

test("shows English empty states on narrow screens without horizontal page overflow", async ({ page }) => {
  const mock = await mockService(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => localStorage.setItem("anivag-locale", "en"));
  for (const path of ["/settings", "/watch", "/library", "/tasks", "/usage", "/search"]) {
    await page.goto(path);
    await expect(page.locator("main")).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
    // No business records are present, so all visible Chinese would be untranslated interface copy.
    expect((await page.locator("main").innerText()).replace(/ZH\s*中文/g, ""), path).not.toMatch(/[\p{Script=Han}]/u);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), path).toBe(true);
    if (path === "/settings") await page.screenshot({ path: "/private/tmp/anivag-locale-y_vrdu2o/settings-en-mobile.png", fullPage: true });
  }
  expect(mock.unexpected).toEqual([]);
  expect(mock.errors).toEqual([]);
});

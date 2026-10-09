import { expect, test, type Page, type Route } from "@playwright/test";

import type { AuthIdentity, SeriesView } from "../src/api/contracts";

const guest: AuthIdentity = {
  userId: "guest-a", email: null, isGuest: true, tenantId: null,
  publicTenantId: "platform-public", mode: "cloud", csrfToken: "csrf-guest-a",
  permissions: { canUpload: false, canManage: false, canConfigure: false },
};
const member: AuthIdentity = {
  ...guest, userId: "member-a", email: "learner@example.com", isGuest: false,
  tenantId: "personal-a", csrfToken: "csrf-member-a",
  permissions: { canUpload: true, canManage: true, canConfigure: false },
};
const loggedOutGuest: AuthIdentity = { ...guest, userId: "guest-b", csrfToken: "csrf-guest-b" };

function course(tenantId: string, name: string): SeriesView {
  return {
    seriesId: `course-${tenantId}`, tenantId, name, isPublished: tenantId === guest.publicTenantId,
    description: `${name}的课程介绍`, status: "READY", videoCount: 1, readyVideoCount: 1,
    failedVideoCount: 0, pendingUploadCount: 0, cover: null, lastSearch: null,
    createdAt: 0, updatedAt: 0, transcriptLanguageHint: null,
    importantExtraData: null, characterBible: [], pendingUploads: [],
    videos: [{
      videoId: `video-${tenantId}`, tenantId, seriesId: `course-${tenantId}`,
      episodeOrder: 1, uploadSequence: 1, originalFilename: `${name}.mp4`,
      totalBytes: 100, durationSeconds: 120_000, uploadStatus: "COMPLETED", uploadProgress: 100,
      parseStatus: "SUCCEEDED", parseProgress: 100, indexStatus: "READY", playbackStatus: "READY",
      playbackUrl: `/api/videos/content?tenantId=${tenantId}&videoId=video-${tenantId}`,
      playbackUrlExpiresAt: null, thumbnail: null, latestJobId: null, lastError: null,
    }],
  };
}

async function mockCloud(page: Page, { empty = false } = {}) {
  let identity: AuthIdentity | null = null;
  const requests: { path: string; method: string; cookie?: string; csrf?: string; body: unknown }[] = [];
  const unexpected: string[] = [];
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.addInitScript(() => {
    const originalFetch = window.fetch;
    const calls: { path: string; credentials?: RequestCredentials }[] = [];
    Object.assign(window, { anivagFetchCalls: calls });
    window.fetch = (input, init) => {
      calls.push({ path: String(input), credentials: init?.credentials });
      return originalFetch(input, init);
    };
  });
  await page.route("**/healthz", (route) => route.fulfill({ json: { status: "ok" } }));
  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const headers = await request.allHeaders();
    requests.push({ path: url.pathname, method: request.method(), cookie: headers.cookie,
      csrf: headers["x-csrf-token"], body: request.postData() ? request.postDataJSON() : null });
    const replyIdentity = async (next: AuthIdentity) => {
      identity = next;
      await route.fulfill({ json: next, headers: {
        "set-cookie": `anivag_auth=${next.userId}; HttpOnly; SameSite=Lax; Path=/`,
      } });
    };
    if (url.pathname === "/api/auth/me") {
      await route.fulfill(identity ? { json: identity } : {
        status: 401, json: { code: "AUTH_REQUIRED", message: "请建立游客身份", requestId: "test-me" },
      });
      return;
    }
    if (url.pathname === "/api/auth/guest") return replyIdentity(guest);
    if (url.pathname === "/api/auth/email/send") {
      await route.fulfill({ json: { retryAfterSeconds: 60 } });
      return;
    }
    if (url.pathname === "/api/auth/email/verify") return replyIdentity(member);
    if (url.pathname === "/api/auth/logout") return replyIdentity(loggedOutGuest);
    const tenantId = url.searchParams.get("tenantId");
    const publicCourse = course(guest.publicTenantId, "公开线性代数");
    const ownCourse = course(member.tenantId!, "我的私有课程");
    const selected = tenantId === guest.publicTenantId ? publicCourse : ownCourse;
    if (url.pathname.startsWith("/api/series/") && (!identity || (
      tenantId !== identity.publicTenantId && (identity.isGuest || tenantId !== identity.tenantId)
    ))) {
      unexpected.push(`Unauthorized tenant request: ${tenantId}`);
      await route.fulfill({ status: 404, json: { code: "NOT_FOUND", message: "Tenant denied", requestId: "test" } });
      return;
    }
    if (url.pathname === "/api/series/list") {
      await route.fulfill({ json: { items: empty ? [] : [selected], total: empty ? 0 : 1,
        hasMore: false, nextCursor: null } });
      return;
    }
    if (url.pathname === "/api/series/get") {
      await route.fulfill({ json: selected });
      return;
    }
    unexpected.push(request.method() + " " + url.pathname);
    await route.fulfill({ status: 404, json: { code: "NOT_FOUND", message: "Unmocked API", requestId: "test" } });
  });
  return { requests, unexpected, pageErrors };
}

test("bootstraps an HttpOnly guest session and shows an empty public catalog", async ({ page, context }) => {
  const mock = await mockCloud(page, { empty: true });
  await page.goto("/");
  await expect(page.getByText("公开课程暂未上线。")).toBeVisible();
  await expect(page.getByLabel("选择资料来源", { exact: true })).toHaveValue(guest.publicTenantId);
  await expect(page.getByRole("link", { name: "资料库", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "设置", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "前往资料库上传视频" })).toHaveCount(0);
  const authCookie = (await context.cookies()).find((cookie) => cookie.name === "anivag_auth");
  expect(authCookie).toMatchObject({ value: guest.userId, httpOnly: true, sameSite: "Lax" });
  expect(await page.evaluate(() => document.cookie)).not.toContain("anivag_auth");
  expect(mock.requests.filter(({ path }) => path.startsWith("/api/auth/")).map(({ path }) => path))
    .toEqual(["/api/auth/me", "/api/auth/guest"]);
  expect(mock.requests.find(({ path }) => path === "/api/series/list")?.cookie).toContain("anivag_auth=guest-a");
  expect(await page.locator("script[src]").first().getAttribute("src")).toMatch(/^\/assets\//);
  expect(mock.unexpected).toEqual([]);
  expect(mock.pageErrors).toEqual([]);
});

test("logs in by email, isolates public and personal data, then resets to a new guest", async ({ page, context }) => {
  const mock = await mockCloud(page);
  const source = page.getByLabel("选择资料来源", { exact: true });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "公开线性代数", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "邮箱登录", exact: true }).first().click();
  const dialog = page.getByRole("dialog", { name: "邮箱登录" });
  await expect(dialog.getByLabel("邮箱", { exact: true })).toBeFocused();
  await dialog.getByLabel("邮箱", { exact: true }).fill("Learner@Example.COM");
  await dialog.getByRole("button", { name: "发送验证码", exact: true }).click();
  await expect(dialog.getByRole("status")).toHaveText("验证码已发送至 learner@example.com");
  await expect(dialog.getByRole("button", { name: /秒后重新发送/ })).toBeDisabled();
  await dialog.getByLabel("验证码", { exact: true }).fill("123456");
  await dialog.getByRole("button", { name: "登录", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(source).toHaveValue(member.tenantId!);
  await expect(page.getByRole("heading", { name: "我的私有课程", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "公开线性代数", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "资料库", exact: true })).toBeVisible();
  for (const path of ["/api/auth/email/send", "/api/auth/email/verify"]) {
    expect(mock.requests.find((request) => request.path === path)).toMatchObject({
      method: "POST", cookie: "anivag_auth=guest-a", csrf: guest.csrfToken,
      body: path.endsWith("verify") ? { email: member.email, code: "123456" } : { email: member.email },
    });
  }
  await source.selectOption(guest.publicTenantId);
  await expect(page.getByRole("heading", { name: "公开线性代数", exact: true })).toBeVisible();
  await expect(page.locator("main")).not.toContainText("我的私有课程");
  await expect(page.getByRole("link", { name: "资料库", exact: true })).toHaveCount(0);
  await source.selectOption(member.tenantId!);
  await expect(page.getByRole("heading", { name: "我的私有课程", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "退出", exact: true }).first().click();
  await expect(source).toHaveValue(guest.publicTenantId);
  await expect(page.getByRole("heading", { name: "公开线性代数", exact: true })).toBeVisible();
  await expect(page.locator("main")).not.toContainText("我的私有课程");
  await expect(page.getByText(member.email!, { exact: true })).toHaveCount(0);
  await expect(source.locator("option")).toHaveCount(1);
  await expect(page.getByRole("link", { name: "资料库", exact: true })).toHaveCount(0);
  expect(mock.requests.find(({ path }) => path === "/api/auth/logout")).toMatchObject({
    method: "POST", cookie: "anivag_auth=member-a", csrf: member.csrfToken,
  });
  expect((await context.cookies()).find((cookie) => cookie.name === "anivag_auth"))
    .toMatchObject({ value: loggedOutGuest.userId, httpOnly: true });
  const fetches = await page.evaluate(() => (window as Window & {
    anivagFetchCalls: { path: string; credentials?: RequestCredentials }[];
  }).anivagFetchCalls);
  expect(fetches.filter(({ path }) => path.startsWith("/api/")).every(({ credentials }) => credentials === "include")).toBe(true);
  expect(mock.unexpected).toEqual([]);
  expect(mock.pageErrors).toEqual([]);
});

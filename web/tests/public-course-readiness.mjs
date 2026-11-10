// Driven by the isolated Python fixture with a real API and a built release frontend.
import assert from "node:assert/strict";
import { chromium, expect } from "@playwright/test";

const [origin, seriesId, videoId] = process.argv.slice(2);
const browser = await chromium.launch({ channel: process.env.SEEKINO_TEST_BROWSER_CHANNEL || undefined });
try {
  const page = await browser.newPage({ locale: "zh-CN", viewport: { width: 1440, height: 900 } });
  const ranges = [];
  page.on("response", (response) => {
    if (response.url().includes("/api/videos/content") && response.url().includes(videoId)) ranges.push(response.status());
  });
  await page.goto(origin + "/watch");
  const course = page.locator("#watch-series-shelf").getByRole("link", { name: /隔离公开课程/ });
  await expect(course).toBeVisible();
  await course.click();
  await page.locator(`a[href*="${videoId}"]`).first().click();
  await page.waitForURL((url) => url.pathname.includes(seriesId) && url.pathname.includes(videoId));
  const url = new URL(page.url());
  url.searchParams.set("atSeconds", "6000");
  await page.goto(url.href);
  const video = page.locator("video").first();
  await expect.poll(() => video.evaluate((element) => element.currentTime), { timeout: 15000 }).toBeCloseTo(6, 1);
  await video.evaluate((element) => { element.muted = true; return element.play(); });
  await expect.poll(() => video.evaluate((element) => element.currentTime)).toBeGreaterThan(6.1);
  await video.evaluate((element) => element.pause());
  assert(ranges.includes(206));
  assert((await (await page.request.get(origin + "/api/auth/me")).json()).isGuest);
  const viewer = page.getByRole("region", { name: "视频播放器" });
  const viewerWidth = () => viewer.evaluate((element) => element.getBoundingClientRect().width);
  const closedWidth = await viewerWidth();
  const kino = page.getByRole("button", { name: "与 Kino 对话" });
  const companion = page.getByRole("complementary", { name: "观看侧栏" });
  await kino.click();
  await expect(companion).toHaveAttribute("data-mode", "floating");
  await expect(companion.getByRole("textbox")).toBeVisible();
  assert.equal(await viewerWidth(), closedWidth);
  await companion.getByRole("button", { name: "停靠到侧栏" }).click();
  await expect.poll(viewerWidth).toBeLessThan(closedWidth - 250);
  await page.reload();
  await kino.click();
  await expect(companion).toHaveAttribute("data-mode", "docked");
  await companion.getByRole("button", { name: "改为悬浮窗" }).click();
  await expect.poll(viewerWidth).toBe(closedWidth);
  await companion.getByRole("button", { name: "关闭对话面板" }).click();
  await viewer.getByRole("button", { name: "网页全屏", exact: true }).click();
  await expect.poll(() => page.evaluate(() => Boolean(document.fullscreenElement))).toBe(true);
  await kino.click();
  await expect(companion.getByRole("textbox")).toBeVisible();
  assert(await companion.evaluate((element) => document.fullscreenElement.contains(element)));
  await page.evaluate(() => document.exitFullscreen());
  for (const [question, answer] of [
    ["你好", "你好，我们可以聊这门课。"], ["继续解释", "可以继续讨论。"],
    ["搜整门课", "本次检索覆盖 E01–E07，E08 正在处理"], ["只查 E08", "该集正在解析，暂时无法检索"],
    ["搜整门课的事件", "本次检索覆盖 E01–E06，E07 暂无可检索的事件索引，E08 正在处理"],
    ["只查第一个片段的事件", "本次检索覆盖 E01 的指定片段"],
    ["只查 E09", "所选课程中没有这一集，请核对集数"],
    ["检索后只发文字", "检索到了候选片段，但本次回答未附带可播放视频，请重试。"],
  ]) {
    await page.getByRole("textbox", { name: "向 Seekino 助手提问" }).fill(question);
    await page.getByRole("button", { name: "发送问题" }).click();
    await expect(page.locator("#watch-chat-panel article").getByText(answer, { exact: true }).filter({ visible: true }).first()).toBeVisible({ timeout: 15000 });
  }
  await page.reload();
  const panel = page.locator("#watch-chat-panel");
  if (!await panel.isVisible()) await kino.click();
  await expect(panel.getByText("检索到了候选片段，但本次回答未附带可播放视频，请重试。", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(companion).toHaveAttribute("data-mode", "docked");
  await expect(companion.getByRole("button", { name: "改为悬浮窗" })).toHaveCount(0);
  console.log(`Guest E08 verified: floating player width ${closedWidth}px, docking and persistence, fullscreen, mobile drawer, playback at 6000 ms (206), eight chat turns and scoped retrieval.`);
} finally {
  await browser.close();
}

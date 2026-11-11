// Run against a real watch page. The expected hits come from an independent fixture or source snapshot.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { chromium, expect } from "@playwright/test";

const [origin, seriesId, tenantId, expectedFile] = process.argv.slice(2);
const episodes = JSON.parse(await readFile(expectedFile, "utf8"));
const captureDir = process.env.SEEKINO_TEST_FIND_SCREENSHOTS;
const browser = await chromium.launch({ channel: process.env.SEEKINO_TEST_BROWSER_CHANNEL || undefined });
try {
  const page = await browser.newPage({ locale: "zh-CN", viewport: { width: 1280, height: 800 } });
  const viewer = page.getByRole("region", { name: "视频播放器" });
  const player = viewer.locator("video").first();
  const find = page.getByRole("search", { name: "在本视频中查找" });
  const input = find.getByRole("textbox");
  const rows = find.getByRole("listitem");
  const requests = [];
  page.on("request", req => {
    const url = new URL(req.url());
    if (url.pathname === "/api/videos/text-timeline") requests.push(url.searchParams.get("videoId"));
  });
  const observed = [];
  for (const [index, { videoId, hits }] of episodes.entries()) {
    const path = `/watch/${seriesId}/${videoId}`;
    if (index === 0) await page.goto(origin + path + "?" + new URLSearchParams({ tenantId }));
    else {
      await page.locator(`a[href*="${path}"]:visible`).first().click();
      await page.waitForURL(url => url.pathname === path);
    }
    await expect(find).toHaveCount(0);
    await expect.poll(() => player.evaluate(v => v.readyState), { timeout: 15000 }).toBeGreaterThanOrEqual(1);
    assert.equal(requests.filter(id => id === videoId).length, 0);
    await page.keyboard.press(index === 0 ? "Meta+f" : "Control+f");
    await expect(input).toBeFocused();
    await expect(input).toHaveValue("");
    for (const query of ["高松燈", "高　松 灯"]) {
      await input.fill(query);
      if (!hits.length) await expect(find.getByText("没有匹配的文字")).toBeVisible();
      await expect(rows).toHaveCount(hits.length);
    }
    const positionsMs = [];
    const at = async (ms) => {
      await expect.poll(() => player.evaluate(v => v.seeking ? -1 : Math.round(v.currentTime * 1000)),
        { timeout: 15000 }).toBe(ms);
    };
    for (const [i, hit] of hits.entries()) {
      const row = rows.nth(i);
      await expect(row.getByText(hit.source === "dialogue" ? "台词" : "画面文字", { exact: true })).toBeVisible();
      await expect(row.getByText(hit.text, { exact: true })).toBeVisible();
      await input.press("Enter");
      await at(hit.startMs);
      positionsMs.push(await player.evaluate(v => Math.round(v.currentTime * 1000)));
      if (captureDir) await viewer.screenshot({
        path: join(captureDir, `${videoId}-${hit.startMs}.png`),
      });
    }
    if (hits.length > 1) {
      await input.press("Enter");
      await at(hits[0].startMs);
      await input.press("Shift+Enter");
      await at(hits.at(-1).startMs);
      await input.press("Shift+Enter");
      await at(hits[hits.length - 2].startMs);
      await rows.first().getByRole("button").click();
      await at(hits[0].startMs);
      await page.keyboard.press("ArrowRight");
      await expect(rows.nth(1).getByRole("button")).toBeFocused();
      await at(hits[1].startMs);
      await page.keyboard.press("ArrowLeft");
      await expect(rows.first().getByRole("button")).toBeFocused();
      await at(hits[0].startMs);
    }
    await input.fill("测试空结果QZX");
    await expect(find.getByText("没有匹配的文字")).toBeVisible();
    assert.equal(requests.filter(id => id === videoId).length, 1);
    observed.push({ videoId, positionsMs, timelineRequests: 1 });
    if (index === episodes.length - 1) {
      await input.press("Escape");
      await expect(find).toHaveCount(0);
      await page.setViewportSize({ width: 390, height: 844 });
      await page.getByRole("button", { name: "在本视频中查找" }).click();
      await expect(input).toBeFocused();
      await input.fill("高 松 燈");
      if (!hits.length) await expect(find.getByText("没有匹配的文字")).toBeVisible();
      await expect(rows).toHaveCount(hits.length);
    }
  }
  console.log(JSON.stringify({ episodes: observed }));
} finally {
  await browser.close();
}

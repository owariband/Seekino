import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClientError } from "../../api/client";
import type { ImageAsset, RuntimeMode, SeriesView } from "../../api/contracts";
import { SeriesBackgroundEditor } from "./SeriesBackgroundEditor";

const mocks = vi.hoisted(() => ({ mode: "live" as RuntimeMode, updateSeries: vi.fn(), uploadSeriesImage: vi.fn() }));
vi.mock("../../app/workspace", () => ({ useWorkspace: () => mocks }));
vi.mock("../../api/client", async (importOriginal) => ({ ...await importOriginal<typeof import("../../api/client")>(), uploadSeriesImage: mocks.uploadSeriesImage }));

const series: SeriesView = {
  seriesId: "series-a", workspaceId: "local-default", name: "MyGO", description: "现有简介", status: "EMPTY",
  transcriptLanguageHint: null,
  importantExtraData: null, characterBible: [], videos: [], pendingUploads: [], videoCount: 0, readyVideoCount: 0,
  failedVideoCount: 0, pendingUploadCount: 0, cover: null, lastSearch: null, createdAt: 1, updatedAt: 1,
};
const asset: ImageAsset = { imageId: "simg_uploaded", url: "/api/media/images/content?workspaceId=local-default&imageId=simg_uploaded", expiresAt: null };

async function openEditor(data: SeriesView["importantExtraData"] = null) {
  const view = render(<SeriesBackgroundEditor series={{ ...series, importantExtraData: data }} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "编辑背景" }));
  return { ...view, user, body: screen.getByRole("textbox", { name: "背景正文" }) as HTMLTextAreaElement };
}

beforeEach(() => {
  mocks.mode = "live";
  mocks.updateSeries.mockReset().mockResolvedValue(series);
  mocks.uploadSeriesImage.mockReset().mockResolvedValue(asset);
});

describe("Series background editing", () => {
  it("renders Markdown headings, images and descriptions in their original positions without a duplicate gallery", () => {
    const content = "## `mygo.tomori` — 高松灯\n\n![高松灯官方立绘](asset://simg_tomori)\n\n- 声优：**羊宫妃那**\n- 乐队职责：主唱 `Vo.`\n\n## 千早爱音\n\n![爱音官方立绘](asset://simg_anon)\n\n- 乐队职责：吉他";
    const view = render(<SeriesBackgroundEditor series={{ ...series, importantExtraData: { format: "markdown", content } }} />);
    const heading = screen.getByRole("heading", { name: "mygo.tomori — 高松灯", level: 2 });
    const tomori = screen.getByRole("img", { name: "高松灯官方立绘" });
    const description = screen.getByText("羊宫妃那");
    const anonHeading = screen.getByRole("heading", { name: "千早爱音", level: 2 });
    const anon = screen.getByRole("img", { name: "爱音官方立绘" });
    const ordered = [heading, tomori, description, anonHeading, anon];
    ordered.slice(1).forEach((element, index) => expect(ordered[index]!.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy());
    expect(tomori).toHaveAttribute("src", "/api/media/images/content?workspaceId=local-default&imageId=simg_tomori");
    expect(anon).toHaveAttribute("src", "/api/media/images/content?workspaceId=local-default&imageId=simg_anon");
    expect(tomori.closest("p")).not.toBeNull();
    expect(description.tagName).toBe("STRONG");
    expect(screen.getAllByRole("list")).toHaveLength(2);
    expect(view.container.querySelectorAll("figure")).toHaveLength(0);
    expect(view.container.textContent).not.toContain("asset://");
    expect(mocks.updateSeries).not.toHaveBeenCalled();
  });

  it("preserves repeated and reference-style images while leaving code references as text", () => {
    const content = "![第一次][灯]\n\n![第二次](asset://simg_tomori)\n\n[灯]: asset://simg_tomori\n\n~~~markdown\n![示例](asset://simg_code_only)\n~~~";
    const view = render(<SeriesBackgroundEditor series={{ ...series, importantExtraData: { seriesBible: content } }} />);
    expect(screen.getAllByRole("img")).toHaveLength(2);
    expect(screen.getByRole("img", { name: "第一次" })).toHaveAttribute("src", "/api/media/images/content?workspaceId=local-default&imageId=simg_tomori");
    expect(screen.getByRole("img", { name: "第二次" })).toHaveAttribute("src", "/api/media/images/content?workspaceId=local-default&imageId=simg_tomori");
    expect(view.container.querySelector("pre code")).toHaveTextContent("![示例](asset://simg_code_only)");
    expect(view.container.querySelectorAll("figure")).toHaveLength(0);
  });

  it("renders Markdown tables and keeps the original source unchanged when editing", async () => {
    const content = "## 人物\n\n| 人物 | 职责 |\n| --- | --- |\n| 高松灯 | 主唱 |\n\n![立绘](asset://simg_tomori)\n";
    render(<SeriesBackgroundEditor series={{ ...series, importantExtraData: { format: "markdown", content } }} />);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "主唱" })).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "编辑背景" }));
    expect(screen.getByRole("textbox", { name: "背景正文" })).toHaveValue(content);
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByRole("img", { name: "立绘" })).toBeInTheDocument();
    expect(mocks.updateSeries).not.toHaveBeenCalled();
  });

  it("does not interpret JSON text as Markdown and preserves its managed image previews", () => {
    const content = '{"人物":"## 高松灯","参考图":"asset://simg_tomori"}';
    const view = render(<SeriesBackgroundEditor series={{ ...series, importantExtraData: { format: "json", content } }} />);
    expect(view.container.querySelector("pre")).toHaveTextContent(content);
    expect(screen.queryByRole("heading", { name: "高松灯" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(1);
    expect(view.container.querySelectorAll("figure")).toHaveLength(1);
  });

  it("rejects unsafe URLs and embedded HTML instead of injecting executable markup", () => {
    const content = '<script>window.bad = true</script>\n\n![非法](javascript:evil)\n\n[危险链接](javascript:evil)\n\n![非法引用](asset://../bad)';
    const view = render(<SeriesBackgroundEditor series={{ ...series, importantExtraData: { format: "markdown", content } }} />);
    expect(view.container.querySelector("script")).toBeNull();
    expect(view.container.querySelector("img")).toBeNull();
    expect(screen.getByText("危险链接")).not.toHaveAttribute("href", "javascript:evil");
    expect(screen.getAllByText("图片暂时无法预览")).toHaveLength(2);
  });

  it("keeps an unavailable image and its explanation at the same position in the document", () => {
    const content = "## 高松灯\n\n![灯的立绘](asset://simg_missing)\n\n她是乐队主唱。";
    render(<SeriesBackgroundEditor series={{ ...series, importantExtraData: { format: "markdown", content } }} />);
    fireEvent.error(screen.getByRole("img", { name: "灯的立绘" }));
    const fallback = screen.getByRole("img", { name: "灯的立绘" });
    expect(fallback).toHaveTextContent("灯的立绘：图片暂时无法预览");
    expect(fallback.closest("p")).not.toBeNull();
    expect(fallback.compareDocumentPosition(screen.getByText("她是乐队主唱。")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("preserves legacy custom fields and omits unchanged background when only description changes", async () => {
    const old = { seriesBible: "背景", 自定义字段: { 术语: ["乐队"] } };
    const { user, body } = await openEditor(old);
    expect(screen.getByRole("combobox", { name: "正文格式" })).toHaveValue("json");
    expect(JSON.parse(body.value)).toEqual(old);
    fireEvent.change(screen.getByRole("textbox", { name: "简介" }), { target: { value: "新简介" } });
    await user.click(screen.getByRole("button", { name: "保存背景" }));
    await waitFor(() => expect(mocks.updateSeries).toHaveBeenCalledWith({ seriesId: "series-a", description: "新简介" }));
  });

  it("updates and clears the ASR transcript language independently", async () => {
    const { user, rerender } = await openEditor();
    const language = screen.getByRole("combobox", { name: "ASR 原文语言" });
    await user.type(language, "ja");
    await user.click(screen.getByRole("button", { name: "保存背景" }));
    await waitFor(() => expect(mocks.updateSeries).toHaveBeenCalledWith({
      seriesId: "series-a",
      transcriptLanguageHint: "ja",
    }));

    mocks.updateSeries.mockClear();
    rerender(<SeriesBackgroundEditor series={{ ...series, transcriptLanguageHint: "ja" }} />);
    expect(screen.getByText("ASR 原文语言：ja")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "编辑背景" }));
    await user.clear(screen.getByRole("combobox", { name: "ASR 原文语言" }));
    await user.click(screen.getByRole("button", { name: "保存背景" }));
    await waitFor(() => expect(mocks.updateSeries).toHaveBeenCalledWith({
      seriesId: "series-a",
      transcriptLanguageHint: null,
    }));
  });

  it.each([' \n[ { "任意字段" : true } ]\n ', '"普通字符串"', "null", "42", "true"])("saves any JSON value without rewriting the original text: %s", async (content) => {
    const { user, body } = await openEditor();
    await user.selectOptions(screen.getByRole("combobox", { name: "正文格式" }), "json");
    fireEvent.change(body, { target: { value: content } });
    await user.click(screen.getByRole("button", { name: "保存背景" }));
    await waitFor(() => expect(mocks.updateSeries).toHaveBeenCalledWith({ seriesId: "series-a", importantExtraData: { format: "json", content } }));
  });

  it("rejects invalid JSON without losing the draft or sending a save", async () => {
    const { user, body } = await openEditor();
    await user.selectOptions(screen.getByRole("combobox", { name: "正文格式" }), "json");
    fireEvent.change(body, { target: { value: '{"unfinished":' } });
    await user.click(screen.getByRole("button", { name: "保存背景" }));
    expect(screen.getByRole("alert")).toHaveTextContent("JSON 格式有误");
    expect(body).toHaveValue('{"unfinished":');
    expect(mocks.updateSeries).not.toHaveBeenCalled();
  });

  it("clears description and background with explicit null and preserves Markdown whitespace", async () => {
    const { user, body } = await openEditor({ seriesBible: "原始背景" });
    fireEvent.change(body, { target: { value: " \n# 背景\n " } });
    await user.click(screen.getByRole("button", { name: "保存背景" }));
    await waitFor(() => expect(mocks.updateSeries).toHaveBeenCalledWith({ seriesId: "series-a", importantExtraData: { format: "markdown", content: " \n# 背景\n " } }));
    await user.click(screen.getByRole("button", { name: "编辑背景" }));
    fireEvent.change(screen.getByRole("textbox", { name: "简介" }), { target: { value: "" } });
    await user.click(screen.getByRole("button", { name: "清空背景" }));
    await user.click(screen.getByRole("button", { name: "保存背景" }));
    await waitFor(() => expect(mocks.updateSeries).toHaveBeenLastCalledWith({ seriesId: "series-a", description: null, importantExtraData: null }));
  });

  it("keeps the editor open and draft intact when the backend rejects a reference", async () => {
    mocks.updateSeries.mockRejectedValue(new ApiClientError(400, { message: "图片不属于当前视频系列", code: "INVALID_REQUEST" }));
    const { user, body } = await openEditor();
    fireEvent.change(body, { target: { value: "![参考](asset://other_series_image)" } });
    await user.click(screen.getByRole("button", { name: "保存背景" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("图片不属于当前视频系列");
    expect(body).toHaveValue("![参考](asset://other_series_image)");
    expect(screen.queryByText("已保存")).not.toBeInTheDocument();
  });

  it("uploads a chosen image and inserts its managed reference at the captured cursor", async () => {
    const { user, body } = await openEditor({ seriesBible: "前后" });
    body.setSelectionRange(1, 1);
    await user.type(screen.getByRole("textbox", { name: "图片说明（选填）" }), "灯");
    const file = new File(["image-bytes"], "reference.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("选择背景参考图"), file);
    await waitFor(() => expect(body).toHaveValue("前![灯](asset://simg_uploaded)后"));
    expect(mocks.uploadSeriesImage).toHaveBeenCalledWith("series-a", file, expect.any(AbortSignal));
    expect(screen.getByRole("img", { name: "背景参考图" })).toHaveAttribute("src", asset.url);
    expect(body).toHaveFocus();
    expect(body.selectionStart).toBe("前![灯](asset://simg_uploaded)".length);
  });

  it("accepts pasted images without intercepting normal text paste", async () => {
    const { user, body } = await openEditor();
    await user.click(body);
    await user.paste("正文");
    expect(body).toHaveValue("正文");
    const file = new File(["image"], "pasted.png", { type: "image/png" });
    fireEvent.paste(body, { clipboardData: { files: [file] } });
    await waitFor(() => expect(body.value).toContain("asset://simg_uploaded"));
    expect(mocks.uploadSeriesImage).toHaveBeenCalledOnce();
  });

  it("does not invent an image reference after a failed upload and allows retry", async () => {
    mocks.uploadSeriesImage.mockRejectedValueOnce(new ApiClientError(400, { message: "无效图片" }));
    const { user, body } = await openEditor({ seriesBible: "原文" });
    const file = new File(["image"], "reference.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("选择背景参考图"), file);
    expect(await screen.findByRole("alert")).toHaveTextContent("图片上传失败：无效图片");
    expect(body).toHaveValue("原文");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    await user.upload(screen.getByLabelText("选择背景参考图"), file);
    await waitFor(() => expect(body.value).toContain("asset://simg_uploaded"));
  });

  it("inserts into a JSON string but never guesses a custom JSON structure", async () => {
    const content = '{"人物":{"图片":""}}';
    const { user, body } = await openEditor({ format: "json", content });
    body.setSelectionRange(content.indexOf('""') + 1, content.indexOf('""') + 1);
    const file = new File(["image"], "reference.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("选择背景参考图"), file);
    await waitFor(() => expect(JSON.parse(body.value)).toEqual({ 人物: { 图片: "asset://simg_uploaded" } }));
    fireEvent.change(body, { target: { value: "{}" } });
    body.setSelectionRange(1, 1);
    await user.upload(screen.getByLabelText("选择背景参考图"), file);
    expect(await screen.findByRole("status")).toHaveTextContent("请将图片引用放入 JSON 的字符串字段");
    expect(body).toHaveValue("{}");
    expect(screen.getByRole("textbox", { name: "图片引用" })).toHaveValue("asset://simg_uploaded");
  });

  it("blocks saving while upload is pending and cancels it when the editor unmounts", async () => {
    let finish!: (value: ImageAsset) => void;
    mocks.uploadSeriesImage.mockImplementation(() => new Promise<ImageAsset>((resolve) => { finish = resolve; }));
    const { user, body, unmount } = await openEditor();
    fireEvent.change(body, { target: { value: "背景" } });
    await user.upload(screen.getByLabelText("选择背景参考图"), new File(["image"], "reference.png", { type: "image/png" }));
    expect(screen.getByRole("progressbar", { name: "参考图上传进度" })).not.toHaveAttribute("value");
    expect(screen.getByRole("button", { name: "保存背景" })).toBeDisabled();
    const signal = mocks.uploadSeriesImage.mock.calls[0]![2] as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => finish(asset));
    expect(mocks.updateSeries).not.toHaveBeenCalled();
  });

  it("displays a missing image explicitly and disables editing when offline", async () => {
    const view = render(<SeriesBackgroundEditor series={{ ...series, importantExtraData: { format: "markdown", content: "![参考](asset://simg_missing)" } }} />);
    fireEvent.error(screen.getByRole("img"));
    expect(screen.getByText("图片暂时无法预览")).toBeInTheDocument();
    mocks.mode = "offline";
    view.rerender(<SeriesBackgroundEditor series={series} />);
    expect(screen.getByRole("button", { name: "编辑背景" })).toBeDisabled();
  });
});

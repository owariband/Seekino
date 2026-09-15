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
  it("preserves legacy custom fields and omits unchanged background when only description changes", async () => {
    const old = { seriesBible: "背景", 自定义字段: { 术语: ["乐队"] } };
    const { user, body } = await openEditor(old);
    expect(screen.getByRole("combobox", { name: "正文格式" })).toHaveValue("json");
    expect(JSON.parse(body.value)).toEqual(old);
    fireEvent.change(screen.getByRole("textbox", { name: "简介" }), { target: { value: "新简介" } });
    await user.click(screen.getByRole("button", { name: "保存背景" }));
    await waitFor(() => expect(mocks.updateSeries).toHaveBeenCalledWith({ seriesId: "series-a", description: "新简介" }));
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

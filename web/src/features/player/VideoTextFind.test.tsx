import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import * as client from "../../api/client";
import type { VideoVisualCue, VideoVisualTimeline } from "../../api/contracts";
import { VideoTextFind } from "./VideoTextFind";

const phone: VideoVisualCue = { startMs: 774833, endMs: 778500, kind: "SCENE",
  visiblePersons: ["千早愛音"], description: "粉发少女坐在沙发上看手机。" };
const second = { ...phone, startMs: 845166, endMs: 850833, description: "她背着吉他包。" };
const mount = (onSeek = vi.fn()) => render(<VideoTextFind videoId="episode" tenantId="tenant"
  inputRef={createRef()} canSeek onSeek={onSeek} onClose={vi.fn()} />);
const type = (value: string) => fireEvent.change(screen.getByRole("textbox"), { target: { value } });

beforeEach(() => {
  localStorage.clear();
  vi.spyOn(client, "getVideoTextTimeline").mockResolvedValue({ ready: true, cues: [
    { startMs: 3000, endMs: 4000, source: "dialogue", texts: ["爱音说。"] },
  ] });
  vi.spyOn(client, "getVideoVisualTimeline").mockResolvedValue({ ready: true, cues: [phone, second] });
  vi.spyOn(client, "searchVideoVisuals").mockResolvedValue({ ready: true, cues: [] });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("finds a silently visible character, jumps to the source, and remembers text-only on reopen", async () => {
  const seek = vi.fn();
  const panel = mount(seek);
  const toggle = screen.getByRole("switch", { name: "画面搜索" });
  expect(toggle).toBeChecked();
  type("愛 音");
  fireEvent.click(await screen.findByText(phone.description));
  expect(seek).toHaveBeenLastCalledWith(776666);
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  expect(seek).toHaveBeenLastCalledWith(847999);
  expect(screen.getByText("爱音说。")).toBeInTheDocument();
  expect(client.searchVideoVisuals).not.toHaveBeenCalled();
  fireEvent.click(toggle);
  expect(screen.queryByText(phone.description)).not.toBeInTheDocument();
  expect(screen.getByText("爱音说。")).toBeInTheDocument();
  panel.unmount();
  mount();
  expect(screen.getByRole("switch", { name: "画面搜索" })).not.toBeChecked();
  type("爱音");
  await screen.findByText("爱音说。");
  expect(client.getVideoVisualTimeline).toHaveBeenCalledTimes(1);
  expect(client.searchVideoVisuals).not.toHaveBeenCalled();
});

it("discards a late semantic response after switching off and confines suggestions to onscreen people", async () => {
  let settle!: (value: VideoVisualTimeline) => void;
  const remote = vi.mocked(client.searchVideoVisuals);
  remote.mockImplementationOnce(() => new Promise(resolve => { settle = resolve; }));
  const stranger = { ...second, visiblePersons: ["高松燈"], description: "旁人弹琴" };
  remote.mockResolvedValue({ ready: true, cues: [stranger, second] });
  mount();
  type("爱音演奏乐器");
  await waitFor(() => expect(remote).toHaveBeenCalledTimes(1));
  const signal = remote.mock.calls[0]![3];
  fireEvent.click(screen.getByRole("switch", { name: "画面搜索" }));
  expect(signal.aborted).toBe(true);
  await act(async () => settle({ ready: true, cues: [second] }));
  expect(screen.queryByText(second.description)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("switch", { name: "画面搜索" }));
  await screen.findByText(second.description);
  expect(screen.getByText("相关画面")).toBeInTheDocument();
  expect(screen.queryByText(stranger.description)).not.toBeInTheDocument();
  expect(client.getVideoVisualTimeline).toHaveBeenCalledTimes(1);
});

it("keeps local results navigable if semantic retrieval is unavailable", async () => {
  vi.mocked(client.searchVideoVisuals).mockResolvedValue({ ready: false, cues: [] });
  const seek = vi.fn();
  mount(seek);
  type("爱音手机");
  await screen.findByText("语义检索暂不可用");
  fireEvent.click(screen.getByText(phone.description));
  expect(seek).toHaveBeenCalledWith(776666);
});

it("keeps ordinary semantics free of chat calls and reruns only the corrected semantic query in smart mode", async () => {
  const interpret = vi.spyOn(client, "interpretVideoFindQuery").mockResolvedValue({
    query: "生气", synonyms: ["恼火", "不满"],
  });
  vi.mocked(client.getVideoTextTimeline).mockResolvedValue({ ready: true, cues: [
    { startMs: 1200, endMs: 2400, source: "dialogue", texts: ["我红温了。"] },
    { startMs: 4000, endMs: 5100, source: "screen_text", texts: ["他心中不满，也很恼火。"] },
  ] });
  const emotion = { ...phone, description: "少女生气地盯着同伴，一脸不满。" };
  vi.mocked(client.getVideoVisualTimeline).mockResolvedValue({ ready: true, cues: [emotion] });
  const seek = vi.fn();
  mount(seek);
  const toggle = screen.getByRole("switch", { name: "智能搜索" });
  expect(toggle).not.toBeChecked();
  type("红温");
  await screen.findByText("我红温了。");
  await waitFor(() => expect(client.searchVideoVisuals).toHaveBeenCalledTimes(1));
  expect(interpret).not.toHaveBeenCalled();
  fireEvent.click(toggle);
  expect(await screen.findByText("纠正")).toHaveTextContent("纠正 生气、恼火、不满");
  expect(screen.getByRole("textbox")).toHaveValue("红温");
  expect(screen.getAllByText("他心中不满，也很恼火。")).toHaveLength(1);
  fireEvent.click(screen.getByText(emotion.description));
  expect(seek).toHaveBeenCalledWith(776666);
  await waitFor(() => expect(vi.mocked(client.searchVideoVisuals).mock.calls.map(call => call[0])).toEqual(["红温", "生气"]));
  expect(interpret).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("switch", { name: "画面搜索" }));
  expect(screen.queryByText(emotion.description)).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("他心中不满，也很恼火。"));
  expect(seek).toHaveBeenCalledWith(4000);
  fireEvent.click(toggle);
  expect(screen.queryByText("他心中不满，也很恼火。")).not.toBeInTheDocument();
  expect(screen.getByText("我红温了。")).toBeInTheDocument();
});

it("keeps smart lookup independent of visuals and ignores a late response for an earlier search", async () => {
  let finish!: (terms: { query: string; synonyms: string[] }) => void;
  const interpret = vi.spyOn(client, "interpretVideoFindQuery")
    .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockResolvedValue({ query: "爱音", synonyms: [] });
  localStorage.setItem("seekino-watch-visual-find", "false");
  mount();
  fireEvent.click(screen.getByRole("switch", { name: "智能搜索" }));
  type("红温");
  await waitFor(() => expect(interpret).toHaveBeenCalledTimes(1));
  type("爱音");
  expect(interpret.mock.calls[0]![3].aborted).toBe(true);
  await act(async () => finish({ query: "生气", synonyms: ["恼火"] }));
  await screen.findByText("爱音说。");
  await waitFor(() => expect(interpret).toHaveBeenCalledTimes(2));
  expect(screen.queryByText(/恼火/)).not.toBeInTheDocument();
  expect(client.searchVideoVisuals).not.toHaveBeenCalled();
  expect(client.getVideoVisualTimeline).not.toHaveBeenCalled();
});

it("falls back to ordinary semantics on model failure and retries the current query", async () => {
  const interpret = vi.spyOn(client, "interpretVideoFindQuery").mockRejectedValueOnce(new Error("unavailable"))
    .mockResolvedValueOnce({ query: "恼火", synonyms: [] });
  mount();
  fireEvent.click(screen.getByRole("switch", { name: "智能搜索" }));
  type("红温");
  await screen.findByText("智能搜索暂不可用，已按原词查找");
  await waitFor(() => expect(vi.mocked(client.searchVideoVisuals).mock.calls.map(call => call[0])).toEqual(["红温"]));
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByText("恼火");
  expect(interpret).toHaveBeenCalledTimes(2);
});

it("keeps the original character as a visual constraint when an interpretation drops the name", async () => {
  vi.spyOn(client, "interpretVideoFindQuery").mockResolvedValue({ query: "生气", synonyms: ["恼火"] });
  const intended = { ...phone, description: "她生气地站起来。" };
  const stranger = { ...second, description: "旁人很恼火。", visiblePersons: ["高松燈"] };
  vi.mocked(client.getVideoVisualTimeline).mockResolvedValue({ ready: true, cues: [intended, stranger] });
  vi.mocked(client.searchVideoVisuals).mockResolvedValue({ ready: true, cues: [stranger] });
  mount();
  fireEvent.click(screen.getByRole("switch", { name: "智能搜索" }));
  type("爱音红温");
  await screen.findByText(intended.description);
  await waitFor(() => expect(client.searchVideoVisuals).toHaveBeenCalledTimes(1));
  expect(screen.queryByText(stranger.description)).not.toBeInTheDocument();
});

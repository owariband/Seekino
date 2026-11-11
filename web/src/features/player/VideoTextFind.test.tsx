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
  expect(seek).toHaveBeenLastCalledWith(774833);
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  expect(seek).toHaveBeenLastCalledWith(845166);
  expect(screen.getByText("爱音说。")).toBeInTheDocument();
  expect(client.searchVideoVisuals).not.toHaveBeenCalled();
  fireEvent.click(toggle);
  expect(screen.queryByText(phone.description)).not.toBeInTheDocument();
  expect(screen.getByText("爱音说。")).toBeInTheDocument();
  panel.unmount();
  mount();
  expect(screen.getByRole("switch")).not.toBeChecked();
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
  fireEvent.click(screen.getByRole("switch"));
  expect(signal.aborted).toBe(true);
  await act(async () => settle({ ready: true, cues: [second] }));
  expect(screen.queryByText(second.description)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("switch"));
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
  expect(seek).toHaveBeenCalledWith(774833);
});

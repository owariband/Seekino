import { act, cleanup, renderHook } from "@testing-library/react";
import { createElement, StrictMode, type PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { identityStorageKey, uploadAgentAttachment } from "../../api/client";
import type { AgentAttachmentUploadView } from "../../api/contracts";
import { useAgentAttachments } from "./useAgentAttachments";

vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  identityStorageKey: vi.fn(), uploadAgentAttachment: vi.fn(),
}));

const createObjectURL = vi.fn();
const revokeObjectURL = vi.fn();
const pending: { resolve: (value: AgentAttachmentUploadView) => void; reject: (error: Error) => void; signal: AbortSignal }[] = [];
const image = (name = "image.png", type = "image/png") => new File(["image bytes"], name, { type });
const attachment = (id = "image"): AgentAttachmentUploadView => ({
  mediaUri: `media://${id}`, previewUrl: `/api/agent/attachments/content?mediaUri=${id}`, mediaType: "image/png", sizeBytes: 11,
});

beforeEach(() => {
  vi.clearAllMocks();
  pending.length = 0;
  vi.mocked(identityStorageKey).mockReturnValue("identity-A");
  createObjectURL.mockImplementation((file: File) => `blob:${file.name}`);
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = createObjectURL;
    static revokeObjectURL = revokeObjectURL;
  });
  vi.mocked(uploadAgentAttachment).mockImplementation((_conversationId, _file, signal) => new Promise((resolve, reject) => {
    pending.push({ resolve, reject, signal: signal! });
  }));
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("Attachment drafts", () => {
  it("preserves selection order when uploads finish out of order and leaves validation to the server", async () => {
    const { result } = renderHook(() => useAgentAttachments("conv-A"));
    const files = [image("one.png"), image("two.png", "")];
    act(() => result.current.addFiles(files));
    expect(result.current.items.map((item) => item.file)).toEqual(files);
    expect(result.current.items.map((item) => item.status)).toEqual(["uploading", "uploading"]);
    expect(uploadAgentAttachment).toHaveBeenNthCalledWith(2, "conv-A", files[1], expect.any(AbortSignal));
    await act(async () => pending[1]!.resolve(attachment("two")));
    await act(async () => pending[0]!.resolve(attachment("one")));
    expect(result.current.items.map((item) => item.attachment?.mediaUri)).toEqual(["media://one", "media://two"]);
    expect(result.current.items.every((item) => item.status === "ready")).toBe(true);
  });

  it("retains failed files for explicit retry with the same draft and preview", async () => {
    const { result } = renderHook(() => useAgentAttachments("conv-A"));
    act(() => result.current.addFiles([image()]));
    const first = result.current.items[0]!;
    act(() => result.current.retry(first.id));
    expect(uploadAgentAttachment).toHaveBeenCalledOnce();
    await act(async () => pending[0]!.reject(new Error("图片像素超过限制")));
    expect(result.current.items[0]).toMatchObject({ id: first.id, status: "failed", error: "图片像素超过限制" });
    act(() => result.current.retry(first.id));
    expect(result.current.items[0]).toMatchObject({ id: first.id, previewUrl: first.previewUrl, status: "uploading", error: null });
    await act(async () => pending[1]!.resolve(attachment()));
    expect(result.current.items[0]?.status).toBe("ready");
    expect(createObjectURL).toHaveBeenCalledOnce();
  });

  it("aborts a removed upload and ignores a success delivered afterward", async () => {
    const { result, unmount } = renderHook(() => useAgentAttachments("conv-A"));
    act(() => result.current.addFiles([image()]));
    const item = result.current.items[0]!;
    act(() => result.current.remove(item.id));
    expect(pending[0]!.signal.aborted).toBe(true);
    expect(revokeObjectURL).toHaveBeenCalledWith(item.previewUrl);
    await act(async () => pending[0]!.resolve(attachment()));
    expect(result.current.items).toEqual([]);
    unmount();
    expect(revokeObjectURL).toHaveBeenCalledOnce();
  });

  it("clears only the submitted snapshot while preserving newer files", async () => {
    const { result } = renderHook(() => useAgentAttachments("conv-A"));
    act(() => result.current.addFiles([image("submitted.png")]));
    await act(async () => pending[0]!.resolve(attachment("submitted")));
    const submitted = result.current.items.map((item) => item.id);
    act(() => result.current.addFiles([image("new.png")]));
    act(() => result.current.clear(submitted));
    expect(result.current.items.map((item) => item.file.name)).toEqual(["new.png"]);
    expect(pending[1]!.signal.aborted).toBe(false);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:submitted.png");
  });

  it("discards old conversation uploads and does not let an old clear remove new drafts", async () => {
    const { result, rerender } = renderHook(({ id }) => useAgentAttachments(id), { initialProps: { id: "conv-A" } });
    act(() => result.current.addFiles([image("old.png")]));
    const oldClear = result.current.clear;
    rerender({ id: "conv-B" });
    expect(result.current.items).toEqual([]);
    expect(pending[0]!.signal.aborted).toBe(true);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:old.png");
    act(() => result.current.addFiles([image("new.png")]));
    act(() => oldClear());
    await act(async () => pending[0]!.resolve(attachment("old")));
    expect(result.current.items.map((item) => item.file.name)).toEqual(["new.png"]);
    expect(uploadAgentAttachment).toHaveBeenLastCalledWith("conv-B", expect.any(File), expect.any(AbortSignal));
  });

  it("ignores an old identity result even before rerender, then releases its resources", async () => {
    const { result, rerender } = renderHook(() => useAgentAttachments("conv-A"));
    act(() => result.current.addFiles([image("old.png")]));
    vi.mocked(identityStorageKey).mockReturnValue("identity-B");
    await act(async () => pending[0]!.resolve(attachment("old")));
    expect(result.current.items[0]?.status).toBe("uploading");
    act(() => result.current.addFiles([image("blocked.png")]));
    expect(uploadAgentAttachment).toHaveBeenCalledOnce();
    rerender();
    expect(result.current.items).toEqual([]);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:old.png");
    act(() => result.current.addFiles([image("new.png")]));
    expect(uploadAgentAttachment).toHaveBeenCalledTimes(2);
  });

  it("aborts identity-scoped pending uploads on rerender", () => {
    const { result, rerender } = renderHook(() => useAgentAttachments("conv-A"));
    act(() => result.current.addFiles([image()]));
    vi.mocked(identityStorageKey).mockReturnValue("identity-B");
    rerender();
    expect(pending[0]!.signal.aborted).toBe(true);
    expect(result.current.items).toEqual([]);
  });

  it("waits for a conversation and cleans up URLs and uploads under StrictMode", async () => {
    const wrapper = ({ children }: PropsWithChildren) => createElement(StrictMode, null, children);
    const { result, rerender, unmount } = renderHook(({ id }) => useAgentAttachments(id), { initialProps: { id: null as string | null }, wrapper });
    act(() => result.current.addFiles([image("early.png")]));
    expect(uploadAgentAttachment).not.toHaveBeenCalled();
    expect(createObjectURL).not.toHaveBeenCalled();
    rerender({ id: "conv-A" });
    act(() => result.current.addFiles([image("ready.png"), image("pending.png")]));
    await act(async () => pending[0]!.resolve(attachment()));
    unmount();
    expect(pending[1]!.signal.aborted).toBe(true);
    expect(revokeObjectURL.mock.calls).toEqual([["blob:ready.png"], ["blob:pending.png"]]);
    await act(async () => pending[1]!.reject(new Error("aborted")));
    expect(revokeObjectURL).toHaveBeenCalledTimes(2);
  });
});

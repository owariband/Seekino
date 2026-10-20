import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClientError } from "../../api/client";
import type { SeriesSummary, SeriesView } from "../../api/contracts";
import { SeriesActionsMenu } from "./SeriesActionsMenu";

const mocks = vi.hoisted(() => ({ updateSeries: vi.fn() }));
vi.mock("../../api/client", async (original) => ({ ...await original<typeof import("../../api/client")>(), updateSeries: mocks.updateSeries }));
vi.mock("./ParseExportDialog", () => ({ ParseExportDialog: ({ series: value, onClose }: { series: SeriesSummary; onClose: () => void }) =>
  <div role="dialog" aria-label="导出解析数据"><span>{value.seriesId}:{value.tenantId}</span><button onClick={onClose}>关闭导出</button></div> }));

const series: SeriesSummary = {
  seriesId: "series-a", tenantId: "owner-tenant", name: "我的系列", description: null,
  visibility: "private", access: "member", publisher: null,
  status: "READY", videoCount: 1, readyVideoCount: 1, failedVideoCount: 0,
  pendingUploadCount: 0, cover: null, lastSearch: null, createdAt: 0, updatedAt: 0,
};
const detail: SeriesView = {
  ...series, visibility: "public", transcriptLanguageHint: null, importantExtraData: null,
  characterBible: [], videos: [], pendingUploads: [],
};

function renderMenu(value = series) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const view = render(<QueryClientProvider client={client}><SeriesActionsMenu series={value} /></QueryClientProvider>);
  return { ...view, client, invalidate, user: userEvent.setup() };
}

beforeEach(() => { mocks.updateSeries.mockReset().mockResolvedValue(detail); });

describe("SeriesActionsMenu", () => {
  it("only exposes series controls for a member", () => {
    renderMenu({ ...series, access: "public" });
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("opens export from the shared menu and restores focus on close", async () => {
    const { user } = renderMenu();
    const trigger = screen.getByRole("button");
    await user.click(trigger);
    expect(screen.getByRole("menu", { name: "系列操作" })).toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "导出解析数据" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveTextContent("series-a:owner-tenant");
    await user.click(screen.getByRole("button", { name: "关闭导出" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(mocks.updateSeries).not.toHaveBeenCalled();
  });

  it("updates the resource tenant and refreshes lists/details after the server accepts", async () => {
    let finish!: (value: SeriesView) => void;
    mocks.updateSeries.mockImplementation(() => new Promise<SeriesView>((resolve) => { finish = resolve; }));
    const { client, invalidate, user } = renderMenu();
    const trigger = screen.getByRole("button", { name: "我的系列 的系列选项" });
    await user.click(trigger);
    expect(screen.getByRole("menuitemradio", { name: /私有/ })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("menuitemradio", { name: /公开/ }));
    expect(mocks.updateSeries).toHaveBeenCalledWith({ tenantId: "owner-tenant", seriesId: "series-a", visibility: "public" });
    expect(screen.getByRole("status")).toHaveTextContent("正在保存…");
    screen.getAllByRole("menuitemradio").forEach((item) => expect(item).toBeDisabled());
    expect(invalidate).not.toHaveBeenCalled();
    await act(async () => { finish(detail); });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["series"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["series-detail"] });
    expect(client.getQueryData(["series-detail", "series-a", "owner-tenant"])).toEqual(detail);
    expect(trigger).toHaveFocus();
  });

  it("allows making a public Series private and keeps server errors available for retry", async () => {
    mocks.updateSeries.mockRejectedValueOnce(new ApiClientError(400, { message: "系列尚不可公开" }));
    const { user, invalidate } = renderMenu({ ...series, visibility: "public" });
    await user.click(screen.getByRole("button"));
    await user.click(screen.getByRole("menuitemradio", { name: /私有/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("系列尚不可公开");
    expect(invalidate).not.toHaveBeenCalled();
    expect(screen.getByRole("menuitemradio", { name: /公开/ })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("menuitemradio", { name: /私有/ }));
    expect(mocks.updateSeries).toHaveBeenLastCalledWith({ tenantId: "owner-tenant", seriesId: "series-a", visibility: "private" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  });

  it("supports keyboard navigation, Escape and outside dismissal without changing visibility", async () => {
    const { user } = renderMenu();
    const trigger = screen.getByRole("button");
    await user.click(trigger);
    expect(screen.getByRole("menuitemradio", { name: /私有/ })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitemradio", { name: /公开/ })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    await user.click(trigger);
    await user.click(document.body);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(mocks.updateSeries).not.toHaveBeenCalled();
  });
});

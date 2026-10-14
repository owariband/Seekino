import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AgentMessageView } from "../../api/contracts";
import { AgentProgress } from "./AgentProgress";
import type { AgentProgressEvent } from "./useAgentConversation";

const message: AgentMessageView = {
  messageId: "assistant", conversationId: "conversation", seriesIds: ["series"], sequenceNo: 1,
  role: "ASSISTANT", status: "PROCESSING", phase: "EVALUATING", version: 2, reference: null,
  content: null, modelName: null, searchResult: null,
  renderBlocks: [], timing: null, lastError: null, createdAt: 1, updatedAt: 2,
};
const steps: AgentProgressEvent[] = ["RETRIEVING", "EVALUATING", "RETRIEVING", "EVALUATING"].map((phase, index) => ({
  type: "answer.progress", conversationId: "conversation", messageId: "assistant", sequence: index + 1,
  occurredAt: index + 1, phase: phase as AgentProgressEvent["phase"],
}));

describe("Agent execution timeline", () => {
  it("pairs a live answer with the mascot mood for its phase and drops it once finished", () => {
    const mood = () => document.querySelector("svg[data-mood]")?.getAttribute("data-mood") ?? null;
    const view = render(<AgentProgress message={{ ...message, phase: "RETRIEVING" }} steps={[]} />);
    expect(mood()).toBe("seek");
    view.rerender(<AgentProgress message={message} steps={[]} />);
    expect(mood()).toBe("puzzled");
    view.rerender(<AgentProgress message={{ ...message, phase: "RESPONDING" }} steps={[]} />);
    expect(mood()).toBe("swim");
    view.rerender(<AgentProgress message={{ ...message, status: "FAILED", phase: null }} steps={[]} />);
    expect(mood()).toBe("oops");
    view.rerender(<AgentProgress message={{ ...message, status: "COMPLETED", phase: null }} steps={[]} />);
    expect(mood()).toBeNull();
  });

  it("shows only generic status and persisted total duration in release", () => {
    const view = render(<AgentProgress message={{ ...message, timing: {
      startedAt: Date.now(), totalDurationMs: 42_298,
      stages: [{ phase: "QUEUED", durationMs: 13_308 }, { phase: "RETRIEVING", durationMs: 28_985 }],
    } }} steps={steps} />);
    expect(screen.getByRole("status", { name: "回答状态" })).toHaveTextContent("处理中 · 已用 00:42");
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(screen.queryByText("查找资料")).not.toBeInTheDocument();
    view.rerender(<AgentProgress message={{ ...message, status: "FAILED", phase: null }} steps={steps} />);
    expect(screen.getByRole("status", { name: "回答状态" })).toHaveTextContent("回答未完成");
    expect(screen.queryByText("已完成")).not.toBeInTheDocument();
  });

  it("shows repeated real steps, lets the user collapse them, and collapses on durable completion", async () => {
    const view = render(<AgentProgress displayMode="debug" message={message} steps={steps} />);
    const card = screen.getByLabelText("回答过程").closest("details")!;
    expect(card).toHaveAttribute("open");
    const list = screen.getByRole("list", { name: "已同步的执行步骤" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
    expect(within(list).getAllByText("查找资料")).toHaveLength(2);
    expect(within(list).getByText("进行中")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("回答过程"));
    expect(card).not.toHaveAttribute("open");
    view.rerender(<AgentProgress displayMode="debug" message={{ ...message, status: "COMPLETED", phase: null }} steps={steps} />);
    expect(card).not.toHaveAttribute("open");
    expect(screen.getByText("已完成")).toBeInTheDocument();
  });

  it.each(["release", "debug"] as const)("formats subsecond, live and completed durations as mm:ss in %s", (displayMode) => {
    vi.useFakeTimers();
    const startedAt = Date.parse("2026-09-15T00:00:00Z");
    vi.setSystemTime(startedAt + 900);
    const view = render(<AgentProgress displayMode={displayMode} message={{
      ...message, timing: { startedAt, totalDurationMs: null, stages: [] },
    }} steps={[]} />);
    try {
      const status = displayMode === "debug" ? "核验资料" : "处理中";
      expect(screen.getByText(`${status} · 已用 00:00`)).toBeInTheDocument();
      act(() => { vi.advanceTimersByTime(62_000); });
      expect(screen.getByText(`${status} · 已用 01:02`)).toBeInTheDocument();
      view.rerender(<AgentProgress displayMode={displayMode} message={{
        ...message, status: "COMPLETED", phase: null,
        timing: { startedAt, totalDurationMs: 62_900, stages: [{ phase: "EVALUATING", durationMs: 62_900 }] },
      }} steps={[]} />);
      expect(screen.getByText("已完成 · 总耗时 01:02")).toBeInTheDocument();
      expect(view.container).not.toHaveTextContent(/毫秒|\d+\.\d+\s*秒/);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("restores only the known snapshot phase instead of inventing previous steps", () => {
    render(<AgentProgress displayMode="debug" message={message} steps={[]} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("listitem")).toHaveTextContent("核验资料");
    expect(screen.queryByText("查找资料")).not.toBeInTheDocument();
  });

  it("retains observed steps when a newer recovery snapshot supplies the current phase", () => {
    render(<AgentProgress displayMode="debug" message={{ ...message, phase: "RESPONDING" }} steps={steps} />);
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(5);
    expect(rows[4]).toHaveTextContent("生成回答");
    expect(rows[4]).toHaveAttribute("data-state", "active");
  });

  it("does not show a failure or historical missing steps as successful work", async () => {
    const view = render(<AgentProgress displayMode="debug" message={{ ...message, status: "FAILED", phase: null }} steps={steps} />);
    expect(screen.getByText("回答未完成")).toBeInTheDocument();
    expect(screen.queryByText("已完成")).not.toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("回答过程"));
    expect(screen.getByText("未确认完成")).toBeInTheDocument();
    view.rerender(<AgentProgress displayMode="debug" message={{ ...message, status: "COMPLETED", phase: null }} steps={[]} />);
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    expect(screen.getByText(/完整执行步骤暂不可回放/)).toBeInTheDocument();
  });

  it("shows persisted total timing and every repeated phase in order", () => {
    render(<AgentProgress displayMode="debug" message={{
      ...message,
      status: "COMPLETED",
      phase: null,
      timing: {
        startedAt: Date.parse("2026-09-15T00:00:00Z"),
        totalDurationMs: 4_700,
        stages: [
          { phase: "QUEUED", durationMs: 100 },
          { phase: "RETRIEVING", durationMs: 1_000 },
          { phase: "EVALUATING", durationMs: 200 },
          { phase: "RETRIEVING", durationMs: 1_200 },
          { phase: "EVALUATING", durationMs: 300 },
          { phase: "RESPONDING", durationMs: 1_900 },
        ],
      },
    }} steps={[]} />);

    expect(screen.getByText("已完成 · 总耗时 00:04")).toBeInTheDocument();
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(6);
    expect(rows[0]).toHaveTextContent("等待处理");
    expect(rows[0]).toHaveTextContent("00:00");
    expect(screen.getAllByText("查找资料")).toHaveLength(2);
    expect(rows[5]).toHaveTextContent("生成回答");
    expect(rows[5]).toHaveTextContent("00:01");
  });

  it("shows live total and current phase duration from a reconnect snapshot", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-15T00:00:03.500Z"));
    const view = render(<AgentProgress displayMode="debug" message={{
      ...message,
      phase: "EVALUATING",
      timing: {
        startedAt: Date.parse("2026-09-15T00:00:00Z"),
        totalDurationMs: null,
        stages: [
          { phase: "QUEUED", durationMs: 100 },
          { phase: "RETRIEVING", durationMs: 1_000 },
        ],
      },
    }} steps={[]} />);

    expect(screen.getByText("核验资料 · 已用 00:03")).toBeInTheDocument();
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[2]).toHaveAttribute("data-state", "active");
    expect(rows[2]).toHaveTextContent("进行中 · 00:02");
    view.unmount();
    vi.useRealTimers();
  });
});

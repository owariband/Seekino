import { act, render, screen, within } from "@testing-library/react";
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AgentMessageView } from "../../api/contracts";
import { AgentProgress } from "./AgentProgress";
import type { AgentProgressEvent } from "./useAgentConversation";

const message: AgentMessageView = {
  messageId: "assistant", conversationId: "conversation", seriesIds: ["series"], sequenceNo: 1,
  role: "ASSISTANT", status: "PROCESSING", phase: "EVALUATING", version: 2, reference: null,
  content: null, modelName: null, searchResult: null,
  renderBlocks: [], attachments: [], timing: null, lastError: null, createdAt: 1, updatedAt: 2,
};
const steps: AgentProgressEvent[] = ["RETRIEVING", "EVALUATING", "RETRIEVING", "EVALUATING"].map((phase, index) => ({
  type: "answer.progress", conversationId: "conversation", messageId: "assistant", sequence: index + 1,
  occurredAt: index + 1, phase: phase as AgentProgressEvent["phase"],
}));

describe("Agent execution timeline", () => {
  it.each(["release", "debug"] as const)("keeps Kino above the card, reacts to the outcome, then sleeps there in %s", (displayMode) => {
    vi.useFakeTimers();
    try {
      const view = render(<AgentProgress displayMode={displayMode} message={message} steps={[]} />);
      const walker = view.container.querySelector("[data-pixel-mascot]")!;
      const card = walker.nextElementSibling!;
      const kinoState = () => view.container.querySelector("[data-kino]")?.getAttribute("data-kino-state");
      expect(walker).toHaveAttribute("aria-hidden", "true");
      expect(card.querySelector("svg[data-mood]")).toBeNull();
      expect(kinoState()).toBe("think");
      view.rerender(<AgentProgress displayMode={displayMode} message={{ ...message, phase: "RESPONDING" }} steps={[]} />);
      expect(view.container.querySelector("[data-pixel-mascot]")).toBe(walker);
      expect(kinoState()).toBe("loading");
      view.rerender(<AgentProgress displayMode={displayMode} message={{ ...message, status: "FAILED", phase: null }} steps={[]} />);
      expect(view.container.querySelector("[data-pixel-mascot]")).toBe(walker);
      expect(kinoState()).toBe("oops");
      act(() => { vi.advanceTimersByTime(6_000); });
      expect(view.container.querySelector("[data-pixel-mascot]")).toBe(walker);
      expect(kinoState()).toBe("sleep");
      // Another bubble took Kino over: this one stays empty.
      view.rerender(<AgentProgress displayMode={displayMode} message={{ ...message, status: "FAILED", phase: null }} steps={[]} kinoHost={false} />);
      expect(view.container.querySelector("[data-pixel-mascot]")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("cheers once when a live answer completes, then falls asleep on the bubble", () => {
    vi.useFakeTimers();
    try {
      const view = render(<AgentProgress message={message} steps={[]} />);
      view.rerender(<AgentProgress message={{ ...message, status: "COMPLETED", phase: null }} steps={[]} />);
      expect(view.container.querySelector("[data-kino]")).toHaveAttribute("data-kino-state", "happy");
      act(() => { vi.advanceTimersByTime(4_000); });
      expect(view.container.querySelector("[data-kino]")).toHaveAttribute("data-kino-state", "sleep");
      view.rerender(<AgentProgress message={{ ...message, status: "COMPLETED", phase: null }} steps={[]} kinoAwake />);
      expect(view.container.querySelector("[data-kino]")).toHaveAttribute("data-kino-state", "look");
    } finally {
      vi.useRealTimers();
    }
  });

  it("sleeps without a reaction on an answer that was already settled", () => {
    const view = render(<AgentProgress message={{ ...message, status: "COMPLETED", phase: null }} steps={[]} />);
    expect(view.container.querySelector("[data-kino]")).toHaveAttribute("data-kino-state", "sleep");
  });

  // The app renders inside StrictMode, which runs mount effects twice in development.
  it.each([false, true])("leaps from the previous bubble onto a new turn's bubble, then gets to work (strict mode: %s)", (strict) => {
    vi.useFakeTimers();
    try {
      const settled = { ...message, messageId: "previous", status: "COMPLETED" as const, phase: null };
      const live = { ...message, messageId: "next" };
      const Thread = ({ next }: { next: boolean }) => <div data-kino-stage="">
        <article data-id="previous"><AgentProgress message={settled} steps={[]} kinoHost={!next} /></article>
        {next ? <article data-id="next"><AgentProgress message={live} steps={[]} kinoHost /></article> : null}
      </div>;
      const view = render(<Thread next={false} />, { wrapper: strict ? StrictMode : undefined });
      const kinoIn = (id: string) => view.container.querySelector(`[data-id="${id}"] [data-kino]`);
      expect(kinoIn("previous")).toHaveAttribute("data-kino-state", "sleep");

      view.rerender(<Thread next />);
      expect(kinoIn("previous")).toBeNull();
      const arriving = kinoIn("next")!;
      expect(arriving).toHaveAttribute("data-kino-state", "leap");
      // It is still drawn on the old perch, getting up from its nap before the jump.
      expect(arriving).toHaveAttribute("data-leaping");
      expect(arriving).toHaveAttribute("data-pose", "side");
      let leapt = false;
      for (let i = 0; i < 40 && kinoIn("next")?.getAttribute("data-kino-state") === "leap"; i++) {
        act(() => { vi.advanceTimersByTime(70); });
        leapt ||= kinoIn("next")?.hasAttribute("data-leaping") === true && (kinoIn("next") as SVGElement).style.transform !== "";
      }
      expect(leapt).toBe(true);
      expect(kinoIn("next")).toHaveAttribute("data-kino-state", "think");
      expect(kinoIn("next")).not.toHaveAttribute("data-leaping");
    } finally {
      vi.useRealTimers();
    }
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

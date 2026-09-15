import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { AgentMessageView } from "../../api/contracts";
import { AgentProgress } from "./AgentProgress";
import type { AgentProgressEvent } from "./useAgentConversation";

const message: AgentMessageView = {
  messageId: "assistant", conversationId: "conversation", seriesIds: ["series"], sequenceNo: 1,
  role: "ASSISTANT", status: "PROCESSING", phase: "EVALUATING", version: 2, reference: null,
  content: null, modelName: null, replyToMessageId: "user", retrievalQuery: null, searchResult: null,
  lastError: null, createdAt: 1, updatedAt: 2,
};
const steps: AgentProgressEvent[] = ["RETRIEVING", "EVALUATING", "RETRIEVING", "EVALUATING"].map((phase, index) => ({
  type: "answer.progress", conversationId: "conversation", messageId: "assistant", sequence: index + 1,
  occurredAt: index + 1, phase: phase as AgentProgressEvent["phase"],
}));

describe("Agent execution timeline", () => {
  it("shows repeated real steps, lets the user collapse them, and collapses on durable completion", async () => {
    const view = render(<AgentProgress message={message} steps={steps} />);
    const card = screen.getByLabelText("检索过程").closest("details")!;
    expect(card).toHaveAttribute("open");
    const list = screen.getByRole("list", { name: "已同步的执行步骤" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
    expect(within(list).getAllByText("检索片段")).toHaveLength(2);
    expect(within(list).getByText("进行中")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("检索过程"));
    expect(card).not.toHaveAttribute("open");
    view.rerender(<AgentProgress message={{ ...message, status: "COMPLETED", phase: null }} steps={steps} />);
    expect(card).not.toHaveAttribute("open");
    expect(screen.getByText("已完成")).toBeInTheDocument();
  });

  it("restores only the known snapshot phase instead of inventing previous steps", () => {
    render(<AgentProgress message={message} steps={[]} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("listitem")).toHaveTextContent("核验候选");
    expect(screen.queryByText("检索片段")).not.toBeInTheDocument();
  });

  it("retains observed steps when a newer recovery snapshot supplies the current phase", () => {
    render(<AgentProgress message={{ ...message, phase: "RESPONDING" }} steps={steps} />);
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(5);
    expect(rows[4]).toHaveTextContent("生成回答");
    expect(rows[4]).toHaveAttribute("data-state", "active");
  });

  it("does not show a failure or historical missing steps as successful work", async () => {
    const view = render(<AgentProgress message={{ ...message, status: "FAILED", phase: null }} steps={steps} />);
    expect(screen.getByText("回答未完成")).toBeInTheDocument();
    expect(screen.queryByText("已完成")).not.toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("检索过程"));
    expect(screen.getByText("未确认完成")).toBeInTheDocument();
    view.rerender(<AgentProgress message={{ ...message, status: "COMPLETED", phase: null }} steps={[]} />);
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
    expect(screen.getByText(/完整执行步骤暂不可回放/)).toBeInTheDocument();
  });
});

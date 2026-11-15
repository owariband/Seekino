import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentMessageFeedback, AgentMessageView } from "../../api/contracts";
import * as api from "../../api/client";
import { I18nProvider } from "../../app/i18n";
import { MessageFeedback } from "./MessageFeedback";

const now = Date.parse("2026-11-15T00:00:00Z");

function feedback(overrides: Partial<AgentMessageFeedback> = {}): AgentMessageFeedback {
  return {
    rating: "UP",
    reasons: [],
    comment: null,
    createdAt: now,
    updatedAt: now,
    ...overrides,
  };
}

function message(overrides: Partial<AgentMessageView> = {}): AgentMessageView {
  return {
    messageId: "assistant-1",
    conversationId: "conversation-1",
    seriesIds: [],
    sequenceNo: 1,
    role: "ASSISTANT",
    status: "COMPLETED",
    phase: null,
    version: 1,
    reference: null,
    content: "回答",
    attachments: [],
    modelName: "deepseek-flash",
    searchResult: null,
    renderBlocks: [],
    timing: null,
    lastError: null,
    feedback: null,
    createdAt: now,
    updatedAt: now,
    ...overrides,
  };
}

function renderFeedback(item: AgentMessageView) {
  return render(
    <I18nProvider>
      <MessageFeedback message={item} />
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("MessageFeedback", () => {
  it("submits a positive rating and shows the acknowledgement", async () => {
    const user = userEvent.setup();
    const save = vi.spyOn(api, "setAgentMessageFeedback").mockResolvedValue(feedback());
    renderFeedback(message());

    expect(screen.queryByText("有用")).not.toBeInTheDocument();
    expect(screen.queryByText("没用")).not.toBeInTheDocument();

    const button = screen.getByRole("button", { name: "这个回答有帮助" });
    await user.click(button);

    expect(save).toHaveBeenCalledWith("conversation-1", "assistant-1", {
      rating: "UP",
      reasons: [],
      comment: null,
    });
    expect(await screen.findByRole("status")).toHaveTextContent("谢谢反馈喵，我们已收到后续会继续改进服务");
    expect(button).toHaveAttribute("aria-pressed", "true");
  });

  it("submits a negative rating with reasons and a comment", async () => {
    const user = userEvent.setup();
    const save = vi.spyOn(api, "setAgentMessageFeedback").mockResolvedValue(
      feedback({ rating: "DOWN", reasons: ["WRONG_EVIDENCE"], comment: "片段错了" }),
    );
    renderFeedback(message());

    await user.click(screen.getByRole("button", { name: "这个回答没帮助" }));
    await user.click(screen.getByRole("button", { name: "片段或出处不对" }));
    await user.type(screen.getByPlaceholderText("补充说明（可选）"), "片段错了");
    await user.click(screen.getByRole("button", { name: "提交反馈" }));

    expect(save).toHaveBeenCalledWith("conversation-1", "assistant-1", {
      rating: "DOWN",
      reasons: ["WRONG_EVIDENCE"],
      comment: "片段错了",
    });
    expect(await screen.findByRole("status")).toHaveTextContent("谢谢反馈喵，我们已收到后续会继续改进服务");
  });

  it("reports a failed answer with the failure reason", async () => {
    const user = userEvent.setup();
    const save = vi.spyOn(api, "setAgentMessageFeedback").mockResolvedValue(
      feedback({ rating: "DOWN", reasons: ["FAILURE"], comment: null }),
    );
    renderFeedback(message({
      status: "FAILED",
      lastError: { code: "AGENT_MODEL_ERROR", message: "回答失败" },
    }));

    await user.click(screen.getByRole("button", { name: "报告这个问题" }));
    await user.click(screen.getByRole("button", { name: "提交报告" }));

    expect(save).toHaveBeenCalledWith("conversation-1", "assistant-1", {
      rating: "DOWN",
      reasons: ["FAILURE"],
      comment: null,
    });
    expect(await screen.findByRole("status")).toHaveTextContent("谢谢反馈喵，我们已收到后续会继续改进服务");
  });

  it("loads an existing negative rating and clears it", async () => {
    const user = userEvent.setup();
    const clear = vi.spyOn(api, "clearAgentMessageFeedback").mockResolvedValue();
    renderFeedback(message({
      feedback: feedback({
        rating: "DOWN",
        reasons: ["TOO_SLOW"],
        comment: "太慢了",
      }),
    }));

    await user.click(screen.getByRole("button", { name: "这个回答没帮助" }));
    expect(screen.getByRole("button", { name: "太慢" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByPlaceholderText("补充说明（可选）")).toHaveValue("太慢了");

    await user.click(screen.getByRole("button", { name: "撤销评价" }));
    expect(clear).toHaveBeenCalledWith("conversation-1", "assistant-1");
    expect(screen.queryByText("谢谢反馈喵，我们已收到后续会继续改进服务")).not.toBeInTheDocument();
  });

  it("does not replay the acknowledgement for persisted feedback", () => {
    renderFeedback(message({
      feedback: feedback({
        rating: "DOWN",
        reasons: ["WRONG_EVIDENCE"],
        comment: "片段错了",
      }),
    }));

    expect(screen.queryByText("谢谢反馈喵，我们已收到后续会继续改进服务")).not.toBeInTheDocument();
  });

  it("hides the acknowledgement after four seconds", async () => {
    vi.useFakeTimers();
    vi.spyOn(api, "setAgentMessageFeedback").mockResolvedValue(feedback());
    renderFeedback(message());

    fireEvent.click(screen.getByRole("button", { name: "这个回答有帮助" }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole("status")).toHaveTextContent("谢谢反馈喵，我们已收到后续会继续改进服务");

    act(() => vi.advanceTimersByTime(4000));

    expect(screen.queryByText("谢谢反馈喵，我们已收到后续会继续改进服务")).not.toBeInTheDocument();
  });

  it("rolls back the optimistic state when the request fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setAgentMessageFeedback").mockRejectedValue(new Error("network down"));
    renderFeedback(message());

    const button = screen.getByRole("button", { name: "这个回答有帮助" });
    await user.click(button);

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(button).toHaveAttribute("aria-pressed", "false");
  });
});

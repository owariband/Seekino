import { randomUUID } from "node:crypto";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import type { TSchema, TextContent, ImageContent } from "@earendil-works/pi-ai";
import { abortError, HostError, type Command, type Emit, type PromptCommand, type ToolSpec } from "./protocol.js";
import { runBudget, type RunBudget } from "./models.js";

type ToolContent = TextContent | ImageContent;
export interface ToolResult { content: ToolContent[]; details: unknown; }
type PendingTool = { sessionId: string; runId: string; resolve: (result: ToolResult) => void; reject: (error: Error) => void; cleanup: () => void; budget?: RunBudget };

/** The broker owns callbacks only. Python keeps tool authorization and execution. */
export class ToolBroker {
  private pending = new Map<string, PendingTool>();
  constructor(private readonly emit: Emit, private readonly maxBytes: number) {}
  definitions(specs: ToolSpec[], command: PromptCommand, signal: AbortSignal): ToolDefinition[] {
    return specs.map((spec) => ({
      name: spec.name, label: spec.name, description: spec.description,
      parameters: spec.inputSchema as TSchema,
      execute: async (_nativeToolCallId, arguments_, toolSignal) => {
        if (signal.aborted || toolSignal?.aborted) throw abortError();
        const toolCallId = randomUUID();
        return await new Promise<ToolResult>((resolve, reject) => {
          const cancel = () => { this.pending.delete(toolCallId); cleanup(); reject(abortError()); };
          const cleanup = () => { signal.removeEventListener("abort", cancel); toolSignal?.removeEventListener("abort", cancel); };
          this.pending.set(toolCallId, { sessionId: command.sessionId, runId: command.runId, resolve, reject, cleanup, budget: runBudget.getStore() });
          signal.addEventListener("abort", cancel, { once: true });
          toolSignal?.addEventListener("abort", cancel, { once: true });
          void this.emit({ type: "tool_request", id: command.id, sessionId: command.sessionId,
            runId: command.runId, toolCallId, name: spec.name, arguments: arguments_ }).catch((error: Error) => {
              this.pending.delete(toolCallId); cleanup(); reject(error);
            });
        });
      },
    }));
  }
  resolve(command: Command): void {
    if (typeof command.toolCallId !== "string") throw new HostError("invalid_tool_result", "toolCallId is required.");
    const pending = this.pending.get(command.toolCallId);
    if (!pending) return; // A cancelled invocation may finish after its run has stopped.
    if (pending.sessionId !== command.sessionId || pending.runId !== command.runId)
      throw new HostError("invalid_tool_result", "The tool result belongs to a different invocation.");
    this.pending.delete(command.toolCallId); pending.cleanup();
    if (!command.result || typeof command.result !== "object" || !Array.isArray((command.result as ToolResult).content)) {
      pending.reject(new HostError("invalid_tool_result", "The tool result must contain a content array.")); return;
    }
    const resultBytes = Buffer.byteLength(JSON.stringify(command.result));
    if (resultBytes > this.maxBytes) {
      pending.reject(new HostError("tool_output_limit", "The tool result exceeds the configured limit.")); return;
    }
    if (pending.budget?.maxRetainedBytes) {
      pending.budget.retainedBytes = (pending.budget.retainedBytes ?? 0) + resultBytes;
      if (pending.budget.retainedBytes > pending.budget.maxRetainedBytes) {
        pending.budget.failureCode = "session_limit";
        pending.reject(new HostError("session_limit", "The retained session history exceeds the configured byte limit.")); return;
      }
    }
    const result = command.result as ToolResult;
    if (result.content.some((part) => (part.type !== "text" && part.type !== "image") ||
      (part.type === "text" ? typeof part.text !== "string" : typeof part.data !== "string" || typeof part.mimeType !== "string"))) {
      pending.reject(new HostError("invalid_tool_result", "Invalid tool result content.")); return;
    }
    if (command.isError) {
      pending.reject(new HostError("tool_error", result.content.filter((part): part is TextContent => part.type === "text").map((part) => part.text).join("\n") || "The tool failed."));
    } else pending.resolve({ ...result, details: result.details ?? {} });
  }
  close(): void {
    for (const pending of this.pending.values()) { pending.cleanup(); pending.reject(abortError()); }
    this.pending.clear();
  }
}

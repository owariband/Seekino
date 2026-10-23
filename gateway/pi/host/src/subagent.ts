import { randomUUID } from "node:crypto";
import { createAgentSession, DefaultResourceLoader, SessionManager, SettingsManager, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Type, type AssistantMessage } from "@earendil-works/pi-ai";
import { HostError, Semaphore, type ModelProfile, type HostConfig } from "./protocol.js";
import { thinkingLevel, type ModelPool } from "./models.js";

/** One-shot tasks use Pi's SDK in memory and never enter the persistent conversation registry. */
export function subagentTool(options: {
  profile: ModelProfile; models: ModelPool; config: HostConfig; capacity: Semaphore;
  signal: AbortSignal; tools: ToolDefinition[];
}): ToolDefinition {
  const taskSchema = Type.Object({ task: Type.String(), systemPrompt: Type.Optional(Type.String()) });
  return {
    name: "subagent", label: "Subagent", description: "Execute a one-off task, parallel tasks, or a sequential chain. Tasks have isolated temporary context and return their results.",
    parameters: Type.Object({ task: Type.Optional(Type.String()), tasks: Type.Optional(Type.Array(taskSchema)), chain: Type.Optional(Type.Array(taskSchema)) }),
    execute: async (_id, rawParams) => {
      const params = rawParams as { task?: string; tasks?: { task: string; systemPrompt?: string }[]; chain?: { task: string; systemPrompt?: string }[] };
      const modes = [params.task !== undefined, params.tasks !== undefined, params.chain !== undefined].filter(Boolean).length;
      if (modes !== 1) throw new HostError("invalid_task", "Provide exactly one of task, tasks, or chain.");
      const list = params.task !== undefined ? [{ task: params.task }] : (params.tasks ?? params.chain ?? []);
      if (!list.length || list.length > options.config.maxTransientTasks) throw new HostError("task_limit", "Invalid number of temporary tasks.");
      const reservations: (() => void)[] = [];
      for (let index = 0; index < (params.chain ? 1 : list.length); index++) {
        const release = options.capacity.tryAcquire();
        if (!release) { for (const reserved of reservations) reserved(); throw new HostError("capacity", "The temporary task capacity is full."); }
        reservations.push(release);
      }
      const run = async (task: { task: string; systemPrompt?: string }, previous?: string): Promise<string> => {
        options.signal.throwIfAborted();
        let lease: Awaited<ReturnType<ModelPool["acquire"]>> | undefined;
        let session: Awaited<ReturnType<typeof createAgentSession>>["session"] | undefined;
        try {
          lease = await options.models.acquire(options.profile);
          const settings = SettingsManager.inMemory({ compaction: { enabled: true }, retry: { enabled: false }, enableSkillCommands: false, cacheWarming: "off" });
          const loader = new DefaultResourceLoader({ cwd: options.config.sessionDir, agentDir: options.config.sessionDir,
            settingsManager: settings, noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true,
            noContextFiles: true, systemPrompt: task.systemPrompt ?? "Complete the assigned task using the available tools." });
          await loader.reload();
          ({ session } = await createAgentSession({ cwd: options.config.sessionDir, agentDir: options.config.sessionDir,
            modelRuntime: lease.runtime, model: lease.model, sessionManager: SessionManager.inMemory(options.config.sessionDir, { id: randomUUID() }),
            settingsManager: settings, resourceLoader: loader, tools: options.tools.map((tool) => tool.name), customTools: options.tools,
            thinkingLevel: thinkingLevel(options.profile.reasoningEffort) }));
          const active = session;
          let lastAssistant: AssistantMessage | undefined;
          const unsubscribe = session.agent.subscribe((event) => {
            if (event.type === "message_end" && event.message.role === "assistant") lastAssistant = event.message;
          });
          const cancel = () => { void active.abort(); };
          options.signal.addEventListener("abort", cancel, { once: true });
          try {
            options.signal.throwIfAborted();
            await session.prompt(previous ? `${task.task}\n\nPrevious task result:\n${previous}` : task.task, { expandPromptTemplates: false });
            const last = lastAssistant ?? [...session.messages].reverse().find((message) => message.role === "assistant");
            if (last?.stopReason === "length") throw new HostError("output_token_limit", "The temporary task reached its output token limit.");
            if (!last || last.stopReason === "error" || last.stopReason === "aborted") throw new HostError(last?.stopReason ?? "task_failed", last?.errorMessage ?? "The temporary task did not complete.");
            return session.getLastAssistantText() ?? "";
          } finally {
            options.signal.removeEventListener("abort", cancel);
            unsubscribe();
          }
        } finally { session?.dispose(); lease?.release(); }
      };
      try {
        let results: string[];
        if (params.chain) {
          results = [];
          for (const task of list) results.push(await run(task, results.at(-1)));
        } else {
          const settled = await Promise.allSettled(list.map((task) => run(task)));
          const failed = settled.find((entry) => entry.status === "rejected");
          if (failed?.status === "rejected") throw failed.reason;
          results = settled.map((entry) => (entry as PromiseFulfilledResult<string>).value);
        }
        return { content: [{ type: "text", text: results.join("\n\n") }], details: { tasks: results.length } };
      } finally { for (const release of reservations) release(); }
    },
  };
}

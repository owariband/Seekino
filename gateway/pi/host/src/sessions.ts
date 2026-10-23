import { createHash, randomUUID } from "node:crypto";
import { existsSync, mkdirSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";
import { createAgentSession, DefaultResourceLoader, SessionManager, SettingsManager, type AgentSession } from "@earendil-works/pi-coding-agent";
import type { AssistantMessage, ImageContent, TextContent } from "@earendil-works/pi-ai";
import { ModelPool, runBudget, thinkingLevel, type RunBudget, type RunUsage } from "./models.js";
import { abortError, HostError, Semaphore, type Emit, type HistoryMessage, type HostConfig, type PromptCommand, type SessionState } from "./protocol.js";
import { subagentTool } from "./subagent.js";
import { ToolBroker } from "./tools.js";

type Slot = { manager: SessionManager; session?: AgentSession; state: SessionState; lastUsed: number; running: boolean; bytes: number; priorHistory: HistoryMessage[]; currentMessage?: HistoryMessage };
type ActiveRun = { sessionId: string; runId: string; controller: AbortController; promise: Promise<unknown> };
const emptyUsage = (): RunUsage => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });

/** Hash only canonical message content; transport metadata and credential references are excluded. */
export function historyDigest(history: HistoryMessage[]): string {
  const normalized = history.map(({ role, content }) => ({ role, content }));
  return createHash("sha256").update(JSON.stringify(normalized)).digest("hex");
}

export class SessionRegistry {
  private slots = new Map<string, Slot>();
  private locks = new Map<string, Semaphore>();
  private active = new Map<string, ActiveRun>();
  private runs: Semaphore;
  private temporary: Semaphore;
  private closed = false;
  private queuedBytes = 0;
  private timer: NodeJS.Timeout;
  readonly models: ModelPool;
  readonly tools: ToolBroker;
  constructor(readonly config: HostConfig, private readonly emit: Emit, models?: ModelPool) {
    mkdirSync(config.sessionDir, { recursive: true, mode: 0o700 });
    this.models = models ?? new ModelPool(config.maxModelRequests, config.maxLoadedSessions, config.sessionTtlSeconds * 1000);
    this.tools = new ToolBroker(emit, config.maxToolOutputBytes);
    this.runs = new Semaphore(config.maxActiveRuns); this.temporary = new Semaphore(config.maxTransientTasks);
    this.timer = setInterval(() => this.sweep(), Math.min(config.sessionTtlSeconds * 1000, 30000)); this.timer.unref();
  }
  prompt(command: PromptCommand): Promise<unknown> {
    if (this.closed) return Promise.reject(new HostError("closed", "The Host is shutting down."));
    if (!command.sessionId || !command.runId || typeof command.message !== "string" || !Array.isArray(command.history ?? []))
      return Promise.reject(new HostError("invalid_request", "sessionId, runId, message and history are required."));
    const key = `${command.sessionId}\0${command.runId}`;
    if (this.active.has(key)) return Promise.reject(new HostError("busy", "This invocation is already running."));
    if (this.active.size >= this.config.maxLoadedSessions * 2) return Promise.reject(new HostError("capacity", "The Host admission queue is full."));
    const commandBytes = Buffer.byteLength(JSON.stringify(command));
    if (this.queuedBytes + commandBytes > this.config.maxQueueBytes)
      return Promise.reject(new HostError("capacity", "The Host input queue byte limit was exceeded."));
    this.queuedBytes += commandBytes;
    const controller = new AbortController();
    const promise = this.execute(command, controller).finally(() => { this.queuedBytes -= commandBytes; this.active.delete(key); this.sweep(); });
    this.active.set(key, { sessionId: command.sessionId, runId: command.runId, controller, promise });
    return promise;
  }
  private async execute(command: PromptCommand, controller: AbortController): Promise<unknown> {
    let lock = this.locks.get(command.sessionId);
    if (!lock) { lock = new Semaphore(1); this.locks.set(command.sessionId, lock); }
    const releaseLock = await lock.acquire(controller.signal);
    let releaseRun: (() => void) | undefined;
    let releaseModel: (() => void) | undefined;
    let slot: Slot | undefined;
    let timer: NodeJS.Timeout | undefined;
    let cancel: (() => void) | undefined;
    let unsubscribe: (() => void) | undefined;
    let lastAssistant: AssistantMessage | undefined;
    const usage = emptyUsage();
    const budget: RunBudget = { signal: controller.signal, maxModelCalls: command.limits?.maxModelCalls ?? 100, calls: 0, maxInputChars: command.limits?.maxInputChars, usage };
    let timedOut = false;
    try {
      releaseRun = await this.runs.acquire(controller.signal);
      if (command.limits?.maxInputChars && command.message.length > command.limits.maxInputChars)
        throw new HostError("input_too_large", "The input exceeds the configured character limit.");
      const lease = await this.models.acquire(command.profile); releaseModel = lease.release;
      slot = this.load(command);
      slot.running = true;
      budget.retainedBytes = slot.bytes + Buffer.byteLength(command.message) + Buffer.byteLength(command.systemPrompt ?? "");
      budget.maxRetainedBytes = this.config.maxSessionBytes;
      if (budget.retainedBytes > budget.maxRetainedBytes) throw new HostError("session_limit", "The retained session history exceeds the configured byte limit.");
      this.reserveCache(slot);
      const images = await this.images(command.images ?? [], controller.signal);
      budget.retainedBytes += images.reduce((total, image) => total + Buffer.byteLength(image.data), 0);
      if (budget.retainedBytes > budget.maxRetainedBytes) throw new HostError("session_limit", "Image input exceeds the retained history byte limit.");
      slot.currentMessage = { role: "user", content: images.length
        ? [{ type: "text", text: command.message }, ...images.map((image) => ({ type: "image_url", image_url: { url: `data:${image.mimeType};base64,${image.data}` } }))]
        : command.message };
      const settings = SettingsManager.inMemory({ compaction: { enabled: true }, retry: { enabled: false }, enableSkillCommands: false, cacheWarming: "off",
        httpIdleTimeoutMs: Math.max(1000, (command.limits?.timeoutSeconds ?? 300) * 1000) });
      const loader = new DefaultResourceLoader({ cwd: this.config.sessionDir, agentDir: this.config.sessionDir,
        settingsManager: settings, noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true,
        noContextFiles: true, systemPrompt: command.systemPrompt ?? "Complete the user's task using the available tools." });
      await loader.reload();
      const customTools = this.tools.definitions(command.tools ?? [], command, controller.signal);
      if (!customTools.some((tool) => tool.name === "subagent")) customTools.push(subagentTool({ profile: command.profile,
        models: this.models, config: this.config, capacity: this.temporary, signal: controller.signal, tools: [...customTools] }));
      slot.session?.dispose();
      ({ session: slot.session } = await createAgentSession({ cwd: this.config.sessionDir, agentDir: this.config.sessionDir,
        modelRuntime: lease.runtime, model: lease.model, sessionManager: slot.manager, settingsManager: settings,
        resourceLoader: loader, tools: customTools.map((tool) => tool.name), customTools,
        thinkingLevel: thinkingLevel(command.profile.reasoningEffort) }));
      const session = slot.session;
      slot.state.profileId = command.profile.id; slot.state.profileRevision = command.profile.revision;
      slot.state.pendingRunId = command.runId;
      cancel = () => { void session.abort(); };
      controller.signal.addEventListener("abort", cancel, { once: true });
      if (command.limits?.timeoutSeconds !== null)
        timer = setTimeout(() => { timedOut = true; controller.abort(); }, Math.max(0.001, command.limits?.timeoutSeconds ?? 300) * 1000);
      const event = (payload: Record<string, unknown>) => this.emit({ type: "event", id: command.id, sessionId: command.sessionId, runId: command.runId, event: payload });
      await event({ type: "state", state: this.snapshot(slot) });
      // Agent listeners are awaited, unlike AgentSession listeners, so stdout backpressure reaches the producer.
      unsubscribe = session.agent.subscribe(async (update) => {
        if (update.type === "message_update" && update.assistantMessageEvent.type === "text_delta")
          await event({ type: "text_delta", delta: update.assistantMessageEvent.delta });
        if (update.type === "message_end") {
          if (update.message.role === "assistant") lastAssistant = update.message;
          slot!.bytes = this.fileBytes(slot!.manager, budget.retainedBytes ?? slot!.bytes);
          if (slot!.bytes > this.config.maxSessionBytes) {
            budget.failureCode = "session_limit";
            throw new HostError("session_limit", "The native session file exceeds the configured byte limit.");
          }
        }
      });
      controller.signal.throwIfAborted();
      await runBudget.run(budget,
        async () => session.prompt(command.message, { images, expandPromptTemplates: false }));
      const last = lastAssistant ?? [...session.messages].reverse().find((message) => message.role === "assistant");
      if (controller.signal.aborted) throw timedOut ? new HostError("timeout", "The run timed out.") : abortError();
      if (budget.failureCode) throw new HostError(budget.failureCode, last?.errorMessage ?? "The model request limit was exceeded.");
      if (budget.calls > budget.maxModelCalls) throw new HostError("model_call_limit", "The model call limit was exceeded.");
      if (last?.stopReason === "length") throw new HostError("output_token_limit", "The model response reached its output token limit.");
      if (!last || last.stopReason === "error" || last.stopReason === "aborted")
        throw new HostError(last?.stopReason === "aborted" ? "aborted" : "model_error", last?.errorMessage ?? "No assistant response was produced.");
      slot.state.pendingLeafId = slot.manager.getLeafId();
      const state = this.snapshot(slot);
      if (Object.values(usage).some((value) => value > 0)) await event({ type: "usage", usage });
      await event({ type: "state", state });
      return { text: session.getLastAssistantText() ?? "", state, ...(Object.values(usage).some((value) => value > 0) ? { usage } : {}) };
    } catch (error) {
      if (Object.values(usage).some((value) => value > 0))
        await this.emit({ type: "event", id: command.id, sessionId: command.sessionId, runId: command.runId, event: { type: "usage", usage } }).catch(() => {});
      if (slot) {
        this.rollback(slot);
        await this.emit({ type: "event", id: command.id, sessionId: command.sessionId, runId: command.runId, event: { type: "state", state: this.snapshot(slot) } }).catch(() => {});
      }
      if (timedOut) throw new HostError("timeout", "The run timed out.");
      if (controller.signal.aborted) throw abortError();
      throw error;
    } finally {
      if (timer) clearTimeout(timer);
      if (cancel) controller.signal.removeEventListener("abort", cancel);
      unsubscribe?.();
      if (slot) { slot.session?.dispose(); slot.session = undefined; slot.running = false; slot.lastUsed = Date.now(); }
      if (slot) slot.bytes = this.fileBytes(slot.manager, budget.retainedBytes ?? slot.bytes);
      if (slot && slot.bytes > this.config.maxSessionBytes) this.slots.delete(command.sessionId);
      releaseModel?.(); releaseRun?.(); releaseLock();
    }
  }
  private load(command: PromptCommand): Slot {
    const history = command.history ?? [];
    const historyBytes = Buffer.byteLength(JSON.stringify(history));
    if (historyBytes > this.config.maxSessionBytes) throw new HostError("session_limit", "The restored SQL history exceeds the configured byte limit.");
    const digest = historyDigest(history);
    let slot = this.slots.get(command.sessionId);
    if (slot?.state.pendingRunId) this.rollback(slot);
    const supplied = command.state;
    if (slot && !slot.state.rebuildHistory && slot.state.committedHistoryDigest === digest) { slot.priorHistory = history; return slot; }
    slot?.session?.dispose(); this.slots.delete(command.sessionId);
    let state: SessionState = supplied ? { ...supplied } : {};
    let manager: SessionManager | undefined;
    if (!state.rebuildHistory && state.committedHistoryDigest === digest && state.sessionFile) {
      const file = resolve(state.sessionFile);
      const relativePath = relative(realpathSync(this.config.sessionDir), existsSync(file) ? realpathSync(file) : file);
      if (isAbsolute(relativePath) || relativePath.startsWith("..")) throw new HostError("history_error", "The session path is outside the Host storage directory.");
      if (existsSync(file) && statSync(file).size <= this.config.maxSessionBytes) {
        manager = SessionManager.open(file, this.config.sessionDir, this.config.sessionDir);
        try {
          if (state.committedLeafId) manager.branch(state.committedLeafId); else manager.resetLeaf();
        } catch { manager = undefined; }
      }
    }
    if (!manager) {
      manager = SessionManager.create(this.config.sessionDir, this.config.sessionDir, { id: randomUUID() });
      for (const message of history) {
        if (message.role === "user") manager.appendMessage({ role: "user", content: this.historyContent(message.content), timestamp: Date.now() });
        else if (message.role === "assistant") manager.appendMessage(this.assistant(message.content, command));
        else throw new HostError("history_error", "Only completed user and assistant history is accepted.");
      }
      state = { sessionFile: manager.getSessionFile(), committedLeafId: manager.getLeafId(), committedHistoryDigest: digest };
    }
    delete state.pendingLeafId; delete state.pendingRunId;
    if (this.slots.size >= this.config.maxLoadedSessions) {
      const candidate = [...this.slots].filter(([, value]) => !value.running && !value.state.pendingRunId).sort((a,b) => a[1].lastUsed - b[1].lastUsed)[0];
      if (!candidate) throw new HostError("capacity", "All loaded conversations are running or awaiting confirmation.");
      candidate[1].session?.dispose(); this.slots.delete(candidate[0]);
    }
    slot = { manager, state, lastUsed: Date.now(), running: false, bytes: this.fileBytes(manager, historyBytes), priorHistory: history };
    this.slots.set(command.sessionId, slot); this.sweep(); return slot;
  }
  private assistant(content: unknown, command: PromptCommand): AssistantMessage {
    return { role: "assistant", content: this.historyContent(content).filter((part): part is TextContent => part.type === "text"),
      api: command.profile.api ?? "openai-completions", provider: command.profile.provider, model: command.profile.model,
      stopReason: "stop", timestamp: Date.now(), usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
  }
  private historyContent(content: unknown): (TextContent | ImageContent)[] {
    if (typeof content === "string") return [{ type: "text", text: content }];
    if (!Array.isArray(content)) throw new HostError("history_error", "Invalid history content.");
    return content.flatMap((part: unknown): (TextContent | ImageContent)[] => {
      if (!part || typeof part !== "object") throw new HostError("history_error", "Invalid history content block.");
      const block = part as Record<string, unknown>;
      if (block.type === "text" && typeof block.text === "string") return [{ type: "text", text: block.text }];
      if (block.type === "image_url" && block.image_url && typeof block.image_url === "object") {
        const url = (block.image_url as Record<string, unknown>).url;
        const match = typeof url === "string" ? /^data:(image\/[^;,]+);base64,([\s\S]+)$/.exec(url) : null;
        if (match) return [{ type: "image", mimeType: match[1]!, data: match[2]! }];
      }
      throw new HostError("history_error", "History images must be resolved to data URLs; unsupported content cannot be restored.");
    });
  }
  private async images(input: NonNullable<PromptCommand["images"]>, signal: AbortSignal): Promise<ImageContent[]> {
    const result: ImageContent[] = [];
    let bytes = 0;
    for (const image of input) {
      if (image.data && image.mimeType) { bytes += Buffer.byteLength(image.data); result.push({ type: "image", data: image.data, mimeType: image.mimeType }); }
      else {
        const match = /^data:(image\/[^;,]+);base64,([\s\S]+)$/.exec(image.url ?? "");
        if (match) { bytes += Buffer.byteLength(match[2]!); result.push({ type: "image", mimeType: match[1]!, data: match[2]! }); }
        else throw new HostError("invalid_request", "Images must be resolved to data URLs by the Server.");
      }
      if (signal.aborted) throw abortError();
      if (bytes > this.config.maxToolOutputBytes) throw new HostError("input_limit", "Image input exceeds the configured limit.");
    }
    return result;
  }
  private snapshot(slot: Slot): SessionState { return { ...slot.state, sessionFile: slot.manager.getSessionFile() }; }
  private fileBytes(manager: SessionManager, fallback: number): number {
    const file = manager.getSessionFile();
    return file && existsSync(file) ? statSync(file).size : fallback;
  }
  /** Reserve a whole active session before running, so concurrent growth cannot overcommit the cache. */
  private reserveCache(target: Slot): void {
    const reservedBytes = () => [...this.slots.values()].reduce((total, slot) => total + (slot.running ? this.config.maxSessionBytes : slot.bytes), 0);
    const idle = [...this.slots].filter(([, slot]) => slot !== target && !slot.running && !slot.state.pendingRunId).sort((a, b) => a[1].lastUsed - b[1].lastUsed);
    for (const [id, slot] of idle) {
      if (reservedBytes() <= this.config.maxCacheBytes) return;
      slot.session?.dispose(); this.slots.delete(id);
    }
    if (reservedBytes() > this.config.maxCacheBytes) throw new HostError("capacity", "The Host session cache byte limit was exceeded.");
  }
  private rollback(slot: Slot): void {
    if (slot.state.committedLeafId) slot.manager.branch(slot.state.committedLeafId); else slot.manager.resetLeaf();
    delete slot.state.pendingLeafId; delete slot.state.pendingRunId;
    slot.session?.dispose(); slot.session = undefined;
  }
  async abort(sessionId: string, runId?: string): Promise<void> {
    const runs = [...this.active.values()].filter((run) => run.sessionId === sessionId && (!runId || run.runId === runId));
    for (const run of runs) run.controller.abort();
    await Promise.allSettled(runs.map((run) => run.promise));
  }
  state(sessionId: string): SessionState | undefined { const slot = this.slots.get(sessionId); return slot ? this.snapshot(slot) : undefined; }
  confirm(sessionId: string, runId: string, content: string): SessionState {
    const slot = this.slots.get(sessionId);
    if (!slot || slot.running || slot.state.pendingRunId !== runId || !slot.currentMessage) throw new HostError("invalid_state", "No settled turn matches this confirmation.");
    const pendingText = [...slot.manager.buildSessionContext().messages].reverse().find((message) => message.role === "assistant");
    const nativeText = pendingText?.role === "assistant" ? pendingText.content.filter((part): part is TextContent => part.type === "text").map((part) => part.text).join("") : "";
    slot.state.rebuildHistory = nativeText !== content;
    slot.state.committedLeafId = slot.manager.getLeafId();
    slot.state.committedHistoryDigest = historyDigest([...slot.priorHistory, slot.currentMessage, { role: "assistant", content }]);
    delete slot.state.pendingLeafId; delete slot.state.pendingRunId;
    return this.snapshot(slot);
  }
  async discard(sessionId: string, runId?: string): Promise<SessionState | undefined> {
    await this.abort(sessionId, runId);
    const slot = this.slots.get(sessionId);
    if (slot && (!runId || slot.state.pendingRunId === runId)) this.rollback(slot);
    return slot ? this.snapshot(slot) : undefined;
  }
  async delete(sessionId: string): Promise<void> {
    await this.abort(sessionId); this.slots.get(sessionId)?.session?.dispose(); this.slots.delete(sessionId);
  }
  sweep(): void {
    const idle = [...this.slots].filter(([, slot]) => !slot.running && !slot.state.pendingRunId).sort((a, b) => a[1].lastUsed - b[1].lastUsed);
    let cachedBytes = [...this.slots.values()].reduce((total, slot) => total + slot.bytes, 0);
    for (const [key, slot] of idle)
      if (Date.now() - slot.lastUsed > this.config.sessionTtlSeconds * 1000 || this.slots.size > this.config.maxLoadedSessions || cachedBytes > this.config.maxCacheBytes) {
        slot.session?.dispose(); this.slots.delete(key);
        cachedBytes -= slot.bytes;
      }
    for (const key of this.locks.keys()) if (![...this.active.values()].some((run) => run.sessionId === key)) this.locks.delete(key);
    this.models.sweep();
  }
  async close(): Promise<void> {
    this.closed = true; clearInterval(this.timer);
    for (const run of this.active.values()) run.controller.abort();
    await Promise.allSettled([...this.active.values()].map((run) => run.promise));
    this.tools.close(); for (const slot of this.slots.values()) slot.session?.dispose(); this.slots.clear(); this.models.clear();
  }
}

import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { InMemoryCredentialStore, createAssistantMessageEventStream, type Model, type Api, type AssistantMessage } from "@earendil-works/pi-ai";
import { HostError, Semaphore, type ModelProfile } from "./protocol.js";

export interface RunUsage { input: number; output: number; cacheRead: number; cacheWrite: number; }
export interface RunBudget { signal: AbortSignal; maxModelCalls: number; calls: number; maxInputChars?: number; failureCode?: string; retainedBytes?: number; maxRetainedBytes?: number; usage?: RunUsage; }
export const runBudget = new AsyncLocalStorage<RunBudget>();
export interface ModelLease { runtime: ModelRuntime; model: Model<Api>; release: () => void; }
type RuntimeEntry = { promise: Promise<{ runtime: ModelRuntime; model: Model<Api> }>; users: number; lastUsed: number };

export function thinkingLevel(value?: string): "off" | "minimal" | "low" | "medium" | "high" | "xhigh" {
  if (value === "max") return "xhigh";
  return value === "minimal" || value === "low" || value === "medium" || value === "high" || value === "xhigh" ? value : "off";
}

export class ModelPool {
  private entries = new Map<string, RuntimeEntry>();
  private requests: Semaphore;
  constructor(maxRequests: number, private readonly maxCached: number, private readonly ttlMs: number) { this.requests = new Semaphore(maxRequests); }
  async acquire(profile: ModelProfile): Promise<ModelLease> {
    if (!profile || !profile.id || !profile.revision || !profile.provider || !profile.model || !profile.apiKey)
      throw new HostError("invalid_profile", "A complete authorized model profile is required.");
    const fingerprint = createHash("sha256").update(JSON.stringify(profile)).digest("hex");
    const key = `${profile.id}:${profile.revision}:${fingerprint}`;
    let entry = this.entries.get(key);
    if (!entry) {
      entry = { promise: this.create(profile), users: 0, lastUsed: Date.now() };
      this.entries.set(key, entry);
    }
    entry.users++;
    try {
      const resolved = await entry.promise;
      let released = false;
      return { ...resolved, release: () => {
        if (released) return;
        released = true; entry!.users--; entry!.lastUsed = Date.now(); this.sweep();
      } };
    } catch (error) { entry.users--; this.entries.delete(key); throw error; }
  }
  private async create(profile: ModelProfile): Promise<{ runtime: ModelRuntime; model: Model<Api> }> {
    const credentials = new InMemoryCredentialStore();
    await credentials.modify(profile.provider, async () => ({ type: "api_key", key: profile.apiKey }));
    const runtime = await ModelRuntime.create({ credentials, modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
    const api = profile.api ?? (profile.provider === "anthropic" ? "anthropic-messages" : "openai-completions");
    const builtin = runtime.getModel(profile.provider, profile.model);
    const baseUrl = profile.baseUrl ?? builtin?.baseUrl;
    if (!baseUrl) throw new HostError("invalid_profile", "A base URL is required for this model.");
    runtime.registerProvider(profile.provider, {
      api, baseUrl,
      models: [{ id: profile.model, name: profile.model, api, baseUrl,
        reasoning: Boolean(profile.reasoningEffort && profile.reasoningEffort !== "none" && profile.reasoningEffort !== "off"),
        input: profile.supportsVision ? ["text", "image"] : ["text"],
        cost: builtin?.cost ?? { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: profile.contextWindowTokens ?? builtin?.contextWindow ?? 128000,
        maxTokens: profile.maxOutputTokens ?? builtin?.maxTokens ?? 8192,
      }],
    });
    const model = runtime.getModel(profile.provider, profile.model);
    if (!model) throw new HostError("invalid_profile", "The model could not be registered.");
    const original = runtime.streamSimple.bind(runtime);
    runtime.streamSimple = (requestModel, context, options) => {
      const output = createAssistantMessageEventStream();
      const budget = runBudget.getStore();
      void (async () => {
        let release: (() => void) | undefined;
        const controller = new AbortController();
        const signals = [controller.signal, options?.signal, budget?.signal].filter((signal): signal is AbortSignal => signal !== undefined);
        const signal = AbortSignal.any(signals);
        let streamedBytes = 0;
        try {
          if (budget?.maxRetainedBytes && (budget.retainedBytes ?? 0) > budget.maxRetainedBytes)
            throw new HostError("session_limit", "The retained session history exceeds the configured byte limit.");
          if (budget?.maxInputChars) {
            let characters = 0;
            const count = (value: unknown): void => {
              if (typeof value === "string") { characters += value.length; return; }
              if (Array.isArray(value)) { for (const entry of value) count(entry); return; }
              if (!value || typeof value !== "object") return;
              const entry = value as Record<string, unknown>;
              if (entry.type === "image" || entry.type === "image_url") return;
              for (const child of Object.values(entry)) count(child);
            };
            count(context);
            if (characters > budget.maxInputChars) throw new HostError("input_too_large", "The model context exceeds the configured character limit.");
          }
          if (budget && ++budget.calls > budget.maxModelCalls) throw new HostError("model_call_limit", "The model call limit was exceeded.");
          release = await this.requests.acquire(signal);
          const source = original(requestModel, context, { ...options, signal });
          for await (const event of source) {
            if (budget?.maxRetainedBytes && "delta" in event && typeof event.delta === "string") {
              const bytes = Buffer.byteLength(event.delta);
              streamedBytes += bytes;
              budget.retainedBytes = (budget.retainedBytes ?? 0) + bytes;
              if (budget.retainedBytes > budget.maxRetainedBytes) throw new HostError("session_limit", "The streamed output exceeds the retained history byte limit.");
            }
            if (budget?.usage && (event.type === "done" || event.type === "error")) {
              const usage = (event.type === "done" ? event.message : event.error).usage;
              budget.usage.input += usage.input; budget.usage.output += usage.output;
              budget.usage.cacheRead += usage.cacheRead; budget.usage.cacheWrite += usage.cacheWrite;
            }
            if (budget?.maxRetainedBytes && (event.type === "done" || event.type === "error")) {
              budget.retainedBytes = (budget.retainedBytes ?? 0) + Math.max(0, Buffer.byteLength(JSON.stringify(event.type === "done" ? event.message : event.error)) - streamedBytes);
              if (budget.retainedBytes > budget.maxRetainedBytes) throw new HostError("session_limit", "The retained session history exceeds the configured byte limit.");
            }
            output.push(event);
          }
          output.end(await source.result());
        } catch (error) {
          controller.abort();
          if (budget && error instanceof HostError) budget.failureCode = error.code;
          const aborted = options?.signal?.aborted || budget?.signal.aborted;
          const message: AssistantMessage = { role: "assistant", api: requestModel.api, provider: requestModel.provider,
            model: requestModel.id, content: [], timestamp: Date.now(), stopReason: aborted ? "aborted" : "error",
            errorMessage: error instanceof Error ? error.message : "Model request failed.",
            usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
          output.push({ type: "error", reason: message.stopReason as "error" | "aborted", error: message });
          output.end(message);
        } finally { release?.(); }
      })();
      return output;
    };
    return { runtime, model };
  }
  sweep(): void {
    const idle = [...this.entries].filter(([, value]) => value.users === 0).sort((a, b) => a[1].lastUsed - b[1].lastUsed);
    for (const [key, value] of idle)
      if (Date.now() - value.lastUsed > this.ttlMs || this.entries.size > this.maxCached) this.entries.delete(key);
  }
  clear(): void { this.entries.clear(); }
}

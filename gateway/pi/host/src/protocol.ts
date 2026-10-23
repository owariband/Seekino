import { once } from "node:events";
import type { Writable } from "node:stream";

export interface HostConfig {
  sessionDir: string;
  maxActiveRuns: number;
  maxModelRequests: number;
  maxLoadedSessions: number;
  sessionTtlSeconds: number;
  maxQueueBytes: number;
  maxLineBytes: number;
  maxToolOutputBytes: number;
  maxTransientTasks: number;
  maxSessionBytes: number;
  maxCacheBytes: number;
}

export interface ModelProfile {
  id: string;
  revision: string;
  provider: string;
  model: string;
  apiKey: string;
  baseUrl?: string;
  api?: string;
  supportsVision?: boolean;
  maxOutputTokens?: number;
  contextWindowTokens?: number;
  reasoningEffort?: string;
}
export interface HistoryMessage { role: "user" | "assistant"; content: unknown; }
export interface SessionState {
  sessionFile?: string;
  committedLeafId?: string | null;
  pendingLeafId?: string | null;
  pendingRunId?: string;
  committedHistoryDigest?: string;
  profileId?: string;
  profileRevision?: string;
  rebuildHistory?: boolean;
}
export interface ToolSpec { name: string; description: string; inputSchema: Record<string, unknown>; }
export interface PromptCommand {
  id: string; type: "prompt"; sessionId: string; runId: string;
  profile: ModelProfile; message: string; history?: HistoryMessage[];
  systemPrompt?: string; tools?: ToolSpec[]; state?: SessionState;
  images?: { url?: string; data?: string; mimeType?: string }[];
  limits?: { maxModelCalls?: number; maxInputChars?: number; timeoutSeconds?: number | null };
}
export interface Command {
  id?: string; type: string; sessionId?: string; runId?: string;
  [key: string]: unknown;
}
export type Emit = (message: Record<string, unknown>) => Promise<void>;
export class HostError extends Error {
  constructor(public readonly code: string, message: string) { super(message); }
}
export function abortError(): HostError { return new HostError("aborted", "The run was cancelled."); }

/** Cancelled waiters are removed immediately; releasing never hands capacity to a stale waiter. */
export class Semaphore {
  private active = 0;
  private waiters: { resolve: (release: () => void) => void; reject: (error: Error) => void; signal?: AbortSignal; cancel: () => void }[] = [];
  constructor(private readonly limit: number) {}
  async acquire(signal?: AbortSignal): Promise<() => void> {
    if (signal?.aborted) throw abortError();
    const release = this.tryAcquire();
    if (release) return release;
    return new Promise((resolve, reject) => {
      const waiter = { resolve, reject, signal, cancel: () => {
        this.waiters = this.waiters.filter((entry) => entry !== waiter);
        reject(abortError());
      } };
      this.waiters.push(waiter);
      signal?.addEventListener("abort", waiter.cancel, { once: true });
    });
  }
  tryAcquire(): (() => void) | undefined {
    if (this.active >= this.limit) return undefined;
    this.active++; return this.releaseFunction();
  }
  private releaseFunction(): () => void {
    let released = false;
    return () => {
      if (released) return;
      released = true;
      const waiter = this.waiters.shift();
      if (waiter) {
        waiter.signal?.removeEventListener("abort", waiter.cancel);
        waiter.resolve(this.releaseFunction());
      } else this.active--;
    };
  }
}

/** Keep stdout exclusively JSONL. A blocked consumer cannot accumulate unbounded events. */
export class JsonlWriter {
  private pendingBytes = 0;
  private tail: Promise<void> = Promise.resolve();
  constructor(private readonly stream: Writable, private readonly maxBytes: number, private readonly maxLineBytes: number) {}
  send = (message: Record<string, unknown>): Promise<void> => {
    const line = `${JSON.stringify(message)}\n`;
    const bytes = Buffer.byteLength(line);
    if (bytes > this.maxLineBytes || this.pendingBytes + bytes > this.maxBytes)
      return Promise.reject(new HostError("backpressure", "The Host output buffer limit was exceeded."));
    this.pendingBytes += bytes;
    const queued = this.tail.then(async () => {
      if (!this.stream.write(line)) await once(this.stream, "drain");
    });
    this.tail = queued.catch(() => {}).finally(() => { this.pendingBytes -= bytes; });
    return queued;
  };
  async flush(): Promise<void> { await this.tail; }
}

export function validateConfig(value: unknown): HostConfig {
  if (!value || typeof value !== "object") throw new HostError("invalid_config", "Host config is required.");
  const config = value as HostConfig;
  if (typeof config.sessionDir !== "string" || !config.sessionDir) throw new HostError("invalid_config", "sessionDir is required.");
  for (const key of ["maxActiveRuns", "maxModelRequests", "maxLoadedSessions", "maxQueueBytes", "maxLineBytes", "maxToolOutputBytes", "maxTransientTasks", "maxSessionBytes", "maxCacheBytes"] as const)
    if (!Number.isSafeInteger(config[key]) || config[key] < 1) throw new HostError("invalid_config", `${key} must be a positive integer.`);
  if (!Number.isFinite(config.sessionTtlSeconds) || config.sessionTtlSeconds <= 0) throw new HostError("invalid_config", "sessionTtlSeconds must be positive.");
  if (config.maxCacheBytes < config.maxSessionBytes) throw new HostError("invalid_config", "maxCacheBytes must cover at least one maximum-size session.");
  return config;
}

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { ModelRuntime, SessionManager } from "@earendil-works/pi-coding-agent";
import { fauxProvider, fauxAssistantMessage, fauxToolCall, InMemoryCredentialStore, type Context, type SimpleStreamOptions } from "@earendil-works/pi-ai";
import { ModelPool } from "../src/models.js";
import { SessionRegistry } from "../src/sessions.js";
import { HostError, type Emit, type HostConfig, type ModelProfile, type PromptCommand, type SessionState } from "../src/protocol.js";

const profile: ModelProfile = { id: "tenant-a/profile", revision: "1", provider: "test", model: "faux", apiKey: "secret-never-on-disk" };
function config(sessionDir: string, overrides: Partial<HostConfig> = {}): HostConfig {
  return { sessionDir, maxActiveRuns: 2, maxModelRequests: 2, maxLoadedSessions: 4, sessionTtlSeconds: 900,
    maxQueueBytes: 32 * 1024 * 1024, maxLineBytes: 16 * 1024 * 1024, maxToolOutputBytes: 12 * 1024 * 1024,
    maxTransientTasks: 2, maxSessionBytes: 64 * 1024 * 1024, maxCacheBytes: 256 * 1024 * 1024, ...overrides };
}
function command(sessionId: string, runId = "run1", overrides: Partial<PromptCommand> = {}): PromptCommand {
  return { id: `request-${sessionId}-${runId}`, type: "prompt", sessionId, runId, message: `hello-${sessionId}`, profile, history: [], ...overrides };
}
async function setup(emit: Emit = async () => {}, overrides: Partial<HostConfig> = {}) {
  const dir = mkdtempSync(join(tmpdir(), "anivag-pi-test-"));
  const faux = fauxProvider({ provider: "test", models: [{ id: "faux" }], tokensPerSecond: 0 });
  const runtime = await ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null, refreshOnCreate: false });
  runtime.registerNativeProvider(faux.provider);
  const pool = new ModelPool(2, 4, 10000);
  pool.acquire = async () => ({ runtime, model: faux.getModel(), release: () => {} });
  const registry = new SessionRegistry(config(dir, overrides), emit, pool);
  return { dir, faux, registry, cleanup: async () => { await registry.close(); rmSync(dir, { recursive: true, force: true }); } };
}

test("independent conversations overlap while one conversation runs serially", async () => {
  const { registry, faux, cleanup } = await setup();
  let active = 0, peak = 0;
  faux.setResponses(Array.from({ length: 3 }, () => async (context: Context) => {
    peak = Math.max(peak, ++active);
    const text = JSON.stringify(context.messages);
    await new Promise((resolve) => setTimeout(resolve, 20));
    active--;
    return fauxAssistantMessage(text.includes("hello-a") ? "answer-a" : "answer-b");
  }));
  try {
    const first = registry.prompt(command("a"));
    const second = registry.prompt(command("b"));
    const third = registry.prompt(command("a", "run2"));
    const results = await Promise.all([first, second, third]);
    assert.equal(peak, 2);
    assert.equal((results[0] as { text: string }).text, "answer-a");
    assert.equal((results[1] as { text: string }).text, "answer-b");
  } finally { await cleanup(); }
});

test("native JSONL recovery preserves tool context; SQL canonical changes rebuild safely", async () => {
  let toolCalls = 0;
  let registry: SessionRegistry;
  const fixture = await setup(async (event) => {
    if (event.type === "tool_request") {
      toolCalls++;
      registry.tools.resolve({ type: "tool_result", toolCallId: event.toolCallId, sessionId: event.sessionId as string,
        runId: event.runId as string, result: { content: [{ type: "text", text: "tool-proof" }], details: {} } });
    }
  });
  registry = fixture.registry;
  const { faux, dir, cleanup } = fixture;
  const tools = [{ name: "lookup", description: "lookup", inputSchema: { type: "object", properties: {} } }];
  faux.setResponses([fauxAssistantMessage([fauxToolCall("lookup", {}, { id: "call1" })], { stopReason: "toolUse" }), fauxAssistantMessage("native answer")]);
  try {
    await registry.prompt(command("a", "one", { tools }));
    const state = registry.confirm("a", "one", "native answer");
    assert.equal(toolCalls, 1);
    assert.ok(state.sessionFile);
    assert.ok(!readFileSync(state.sessionFile!, "utf8").includes(profile.apiKey));
    const history = [{ role: "user" as const, content: "hello-a" }, { role: "assistant" as const, content: "native answer" }];
    await registry.delete("a");
    faux.setResponses([(context) => {
      assert.ok(context.messages.some((message) => message.role === "toolResult"));
      return fauxAssistantMessage("second answer");
    }]);
    await registry.prompt(command("a", "two", { history, state }));
    const changed = registry.confirm("a", "two", "canonical answer");
    assert.equal(changed.rebuildHistory, true);
    faux.setResponses([(context) => {
      assert.ok(!context.messages.some((message) => message.role === "toolResult"));
      assert.ok(JSON.stringify(context.messages).includes("canonical answer"));
      return fauxAssistantMessage("third answer");
    }]);
    await registry.prompt(command("a", "three", { state: changed, history: [...history, { role: "user", content: "hello-a" }, { role: "assistant", content: "canonical answer" }] }));
    assert.equal(toolCalls, 1);
    assert.ok(dir);
  } finally { await cleanup(); }
});

test("abort bypasses a queued prompt and leaves an unrelated conversation alive", async () => {
  const { registry, faux, cleanup } = await setup();
  let started!: () => void;
  const ready = new Promise<void>((resolve) => { started = resolve; });
  faux.setResponses([async (_context: Context, options: SimpleStreamOptions | undefined) => {
    started();
    await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true }));
    return fauxAssistantMessage("", { stopReason: "aborted" });
  }, fauxAssistantMessage("unrelated")]);
  try {
    const running = registry.prompt(command("a"));
    const runningFailure = assert.rejects(running, (error) => error instanceof HostError && error.code === "aborted");
    await ready;
    const queued = registry.prompt(command("a", "queued"));
    const queuedFailure = assert.rejects(queued, (error) => error instanceof HostError && error.code === "aborted");
    const other = registry.prompt(command("b"));
    await registry.abort("a");
    await Promise.all([runningFailure, queuedFailure]);
    assert.equal((await other as { text: string }).text, "unrelated");
    assert.equal(registry.state("a")?.pendingRunId, undefined);
  } finally { await cleanup(); }
});

test("unconfirmed history is rolled back without replaying tools", async () => {
  const { registry, faux, cleanup } = await setup();
  faux.setResponses([fauxAssistantMessage("uncommitted"), (context) => {
    assert.ok(!JSON.stringify(context.messages).includes("uncommitted"));
    return fauxAssistantMessage("replacement");
  }]);
  try {
    const initial = await registry.prompt(command("a")) as { state: SessionState };
    await registry.delete("a");
    const recovered = await registry.prompt(command("a", "next", { state: initial.state })) as { text: string };
    assert.equal(recovered.text, "replacement");
  } finally { await cleanup(); }
});

test("bounded cache evicts committed conversations and restores them", async () => {
  const { registry, faux, cleanup } = await setup(async () => {}, { maxLoadedSessions: 1 });
  faux.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("second"), fauxAssistantMessage("restored")]);
  try {
    await registry.prompt(command("a"));
    const state = registry.confirm("a", "run1", "first");
    await registry.prompt(command("b")); registry.confirm("b", "run1", "second");
    assert.equal(registry.state("a"), undefined);
    const result = await registry.prompt(command("a", "next", { state, history: [{ role: "user", content: "hello-a" }, { role: "assistant", content: "first" }] })) as { text: string };
    assert.equal(result.text, "restored");
  } finally { await cleanup(); }
});

test("one-off SDK subagents run inside the Host and release temporary context", async () => {
  const { registry, faux, cleanup } = await setup();
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall("subagent", { tasks: [{ task: "task-one" }, { task: "task-two" }] }, { id: "temporary" })], { stopReason: "toolUse" }),
    fauxAssistantMessage("one result"), fauxAssistantMessage("two result"),
    (context) => {
      assert.ok(JSON.stringify(context.messages).includes("one result"));
      assert.ok(JSON.stringify(context.messages).includes("two result"));
      return fauxAssistantMessage("done");
    },
  ]);
  try {
    assert.equal((await registry.prompt(command("a")) as { text: string }).text, "done");
    assert.equal(faux.state.callCount, 4);
    assert.equal(registry.state("task-one"), undefined);
  } finally { await cleanup(); }
});

test("unsupported history images fail instead of silently losing input", async () => {
  const { registry, cleanup } = await setup();
  try {
    await assert.rejects(registry.prompt(command("a", "run", { history: [{ role: "user", content: [{ type: "image_url", image_url: { url: "https://example.test/image.png" } }] }] })),
      (error) => error instanceof HostError && error.code === "history_error");
  } finally { await cleanup(); }
});

test("oversized native history is discarded before parsing and rebuilt from completed SQL", async () => {
  const { registry, faux, cleanup } = await setup(async () => {}, { maxSessionBytes: 8192, maxCacheBytes: 32768 });
  faux.setResponses([fauxAssistantMessage("first"), (context) => {
    assert.ok(!JSON.stringify(context.messages).includes("oversized-legacy"));
    return fauxAssistantMessage("recovered");
  }]);
  try {
    await registry.prompt(command("a"));
    const state = registry.confirm("a", "run1", "first");
    const manager = SessionManager.open(state.sessionFile!);
    manager.appendCustomEntry("oversized-legacy", { text: "x".repeat(20000) });
    await registry.delete("a");
    const result = await registry.prompt(command("a", "next", { state, history: [{ role: "user", content: "hello-a" }, { role: "assistant", content: "first" }] })) as { text: string; state: SessionState };
    assert.equal(result.text, "recovered");
    assert.notEqual(result.state.sessionFile, state.sessionFile);
  } finally { await cleanup(); }
});

test("cache byte reservations evict idle histories before new runs grow", async () => {
  const { registry, faux, cleanup } = await setup(async () => {}, { maxSessionBytes: 8192, maxCacheBytes: 9000, maxLoadedSessions: 4 });
  faux.setResponses([fauxAssistantMessage("x".repeat(1000)), fauxAssistantMessage("second")]);
  try {
    await registry.prompt(command("a")); registry.confirm("a", "run1", "x".repeat(1000));
    await registry.prompt(command("b"));
    assert.equal(registry.state("a"), undefined);
    assert.ok(registry.state("b"));
  } finally { await cleanup(); }
});

test("prompt queue is bounded by bytes while abort remains available", async () => {
  const { registry, faux, cleanup } = await setup(async () => {}, { maxQueueBytes: 1200 });
  let started!: () => void;
  const ready = new Promise<void>((resolve) => { started = resolve; });
  faux.setResponses([async (_context: Context, options: SimpleStreamOptions | undefined) => {
    started(); await new Promise<void>((resolve) => options?.signal?.addEventListener("abort", () => resolve(), { once: true }));
    return fauxAssistantMessage("", { stopReason: "aborted" });
  }]);
  try {
    const running = registry.prompt(command("a"));
    const cancelled = assert.rejects(running, /cancelled/);
    await ready;
    await assert.rejects(registry.prompt(command("b", "queued", { message: "x".repeat(1000) })), (error) => error instanceof HostError && error.code === "capacity");
    await registry.abort("a"); await cancelled;
  } finally { await cleanup(); }
});

test("input images consume the session byte budget before inference", async () => {
  const { registry, faux, cleanup } = await setup(async () => {}, { maxSessionBytes: 512, maxCacheBytes: 2048 });
  try {
    await assert.rejects(registry.prompt(command("a", "images", { images: [{ data: "x".repeat(600), mimeType: "image/png" }] })),
      (error) => error instanceof HostError && error.code === "session_limit");
    assert.equal(faux.state.callCount, 0);
  } finally { await cleanup(); }
});

test("explicit null overall timeout leaves a long run active until completion", async () => {
  const { registry, faux, cleanup } = await setup();
  faux.setResponses([async () => { await new Promise((resolve) => setTimeout(resolve, 20)); return fauxAssistantMessage("done"); }]);
  try {
    const result = await registry.prompt(command("a", "unlimited", { limits: { timeoutSeconds: null } })) as { text: string; usage?: unknown };
    assert.equal(result.text, "done"); assert.equal(result.usage, undefined);
  } finally { await cleanup(); }
});

test("truncated assistant output is not confirmed as a successful turn", async () => {
  const { registry, faux, cleanup } = await setup();
  faux.setResponses([fauxAssistantMessage("partial", { stopReason: "length" })]);
  try {
    await assert.rejects(registry.prompt(command("a")), (error) => error instanceof HostError && error.code === "output_token_limit");
    assert.equal(registry.state("a")?.pendingRunId, undefined);
  } finally { await cleanup(); }
});

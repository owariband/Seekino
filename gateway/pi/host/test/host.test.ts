import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough, Writable } from "node:stream";
import test from "node:test";
import { fauxProvider, fauxAssistantMessage, type Context } from "@earendil-works/pi-ai";
import { ModelPool, runBudget } from "../src/models.js";
import { serve } from "../src/main.js";
import { HostError, JsonlWriter, Semaphore, validateConfig, type HostConfig, type ModelProfile } from "../src/protocol.js";
import { ToolBroker } from "../src/tools.js";

const config: HostConfig = { sessionDir: "/unused", maxActiveRuns: 2, maxModelRequests: 2, maxLoadedSessions: 4,
  sessionTtlSeconds: 0.5, maxQueueBytes: 32 * 1024 * 1024, maxLineBytes: 16 * 1024 * 1024,
  maxToolOutputBytes: 12 * 1024 * 1024, maxTransientTasks: 2, maxSessionBytes: 64 * 1024 * 1024, maxCacheBytes: 256 * 1024 * 1024 };
const profile: ModelProfile = { id: "user-a", revision: "1", provider: "openai", model: "host-test", apiKey: "key-a", baseUrl: "https://example.test/v1" };

test("profiles isolate credentials, endpoints and revisions inside one Node process", async () => {
  const pool = new ModelPool(1, 4, 1000);
  const a = await pool.acquire(profile);
  const reused = await pool.acquire(profile);
  const b = await pool.acquire({ ...profile, id: "user-b", apiKey: "key-b", baseUrl: "https://other.test/v1" });
  const rotated = await pool.acquire({ ...profile, revision: "2", apiKey: "key-a2" });
  try {
    assert.equal(a.runtime, reused.runtime);
    assert.notEqual(a.runtime, b.runtime);
    assert.notEqual(a.runtime, rotated.runtime);
    assert.equal((await a.runtime.getAuth(a.model))?.auth.apiKey, "key-a");
    assert.equal((await b.runtime.getAuth(b.model))?.auth.apiKey, "key-b");
    assert.equal((await rotated.runtime.getAuth(rotated.model))?.auth.apiKey, "key-a2");
    assert.equal(a.model.baseUrl, "https://example.test/v1");
    assert.equal(b.model.baseUrl, "https://other.test/v1");
  } finally { a.release(); reused.release(); b.release(); rotated.release(); pool.clear(); }
});

test("global model permits cover stream completion across credential runtimes", async () => {
  const pool = new ModelPool(1, 4, 1000);
  const a = await pool.acquire(profile);
  const b = await pool.acquire({ ...profile, id: "user-b", apiKey: "key-b" });
  let active = 0, peak = 0;
  const run = async (lease: typeof a) => {
    const faux = fauxProvider({ provider: "openai", models: [{ id: "host-test" }], tokensPerSecond: 0 });
    faux.setResponses([async () => {
      peak = Math.max(peak, ++active);
      await new Promise((resolve) => setTimeout(resolve, 20)); active--;
      return fauxAssistantMessage("ok");
    }]);
    lease.runtime.registerNativeProvider(faux.provider);
    return lease.runtime.streamSimple(faux.getModel(), { messages: [] }).result();
  };
  try { await Promise.all([run(a), run(b)]); assert.equal(peak, 1); }
  finally { a.release(); b.release(); pool.clear(); }
});

test("full-context input limits include system, history and tool output but exclude image bytes", async () => {
  const pool = new ModelPool(1, 4, 1000);
  const lease = await pool.acquire(profile);
  const faux = fauxProvider({ provider: "openai", models: [{ id: "host-test", input: ["text", "image"] }], tokensPerSecond: 0 });
  lease.runtime.registerNativeProvider(faux.provider);
  faux.setResponses([fauxAssistantMessage("ok")]);
  try {
    const oversized: Context = { systemPrompt: "long system prompt", messages: [{ role: "user", content: "hello", timestamp: 1 }] };
    const failure = await runBudget.run({ signal: new AbortController().signal, calls: 0, maxModelCalls: 2, maxInputChars: 8 },
      () => lease.runtime.streamSimple(faux.getModel(), oversized).result());
    assert.equal(failure.stopReason, "error");
    assert.match(failure.errorMessage!, /character limit/);
    assert.equal(faux.state.callCount, 0);
    const result = await runBudget.run({ signal: new AbortController().signal, calls: 0, maxModelCalls: 2, maxInputChars: 40 },
      () => lease.runtime.streamSimple(faux.getModel(), { messages: [{ role: "user", content: [{ type: "image", data: "x".repeat(10000), mimeType: "image/png" }], timestamp: 1 }] }).result());
    assert.equal(result.stopReason, "stop");
  } finally { lease.release(); pool.clear(); }
});

test("model call limits reject before sending a provider request", async () => {
  const pool = new ModelPool(1, 4, 1000);
  const lease = await pool.acquire(profile);
  const faux = fauxProvider({ provider: "openai", models: [{ id: "host-test" }], tokensPerSecond: 0 });
  lease.runtime.registerNativeProvider(faux.provider);
  try {
    const result = await runBudget.run({ signal: new AbortController().signal, calls: 1, maxModelCalls: 1 },
      () => lease.runtime.streamSimple(faux.getModel(), { messages: [] }).result());
    assert.equal(result.stopReason, "error"); assert.match(result.errorMessage!, /call limit/); assert.equal(faux.state.callCount, 0);
  } finally { lease.release(); pool.clear(); }
});

test("output queue rejects oversized events and waits for drain", async () => {
  let releaseWrite!: () => void;
  const stream = new Writable({ highWaterMark: 1, write(_chunk, _encoding, callback) { releaseWrite = callback; } });
  const writer = new JsonlWriter(stream, 80, 80);
  const first = writer.send({ text: "x".repeat(40) });
  await Promise.resolve();
  await assert.rejects(writer.send({ text: "x".repeat(40) }), (error) => error instanceof HostError && error.code === "backpressure");
  releaseWrite(); await first;
  await assert.rejects(writer.send({ text: "x".repeat(100) }), /buffer limit/);
  stream.end();
});

test("cancelled semaphore waiters don't retain capacity", async () => {
  const capacity = new Semaphore(1);
  const release = await capacity.acquire();
  const controller = new AbortController();
  const waiting = capacity.acquire(controller.signal);
  controller.abort(); await assert.rejects(waiting, /cancelled/);
  release(); const next = await capacity.acquire(); next();
});

test("duplex initialize and shutdown settle without closing stdin first", async () => {
  const dir = mkdtempSync(join(tmpdir(), "anivag-pi-wire-"));
  const input = new PassThrough(); const output = new PassThrough(); const diagnostics = new PassThrough();
  let lines = ""; output.on("data", (chunk) => { lines += String(chunk); });
  const serving = serve(input, output, diagnostics);
  input.write(`${JSON.stringify({ id: "init", type: "initialize", protocolVersion: 1, config: { ...config, sessionDir: dir } })}\n`);
  await new Promise((resolve) => setTimeout(resolve, 10));
  input.write(`${JSON.stringify({ id: "close", type: "shutdown" })}\n`);
  try {
    await serving;
    const messages = lines.trim().split("\n").map((line) => JSON.parse(line) as { id: string; success: boolean; data?: { protocolVersion?: number } });
    assert.deepEqual(messages.map((message) => [message.id, message.success]), [["init", true], ["close", true]]);
    assert.equal(messages[0]?.data?.protocolVersion, 1);
  } finally { input.destroy(); output.destroy(); rmSync(dir, { recursive: true, force: true }); }
});

test("tool callbacks reject cross-conversation ownership and excessive results", async () => {
  let request: Record<string, unknown> | undefined;
  const broker = new ToolBroker(async (event) => { request = event; }, 80);
  const tool = broker.definitions([{ name: "tool", description: "test", inputSchema: { type: "object", properties: {} } }],
    { id: "r", type: "prompt", sessionId: "a", runId: "run", message: "hi", profile }, new AbortController().signal)[0]!;
  const result = tool.execute("native", {}, undefined, undefined, {} as never);
  assert.throws(() => broker.resolve({ type: "tool_result", toolCallId: request!.toolCallId, sessionId: "b", runId: "run" }), /different invocation/);
  broker.resolve({ type: "tool_result", toolCallId: request!.toolCallId, sessionId: "a", runId: "run", result: { content: [{ type: "text", text: "x".repeat(100) }] } });
  await assert.rejects(result, /configured limit/); broker.close();
});

test("protocol validates fractional TTL and rejects missing limits", () => {
  assert.equal(validateConfig(config).sessionTtlSeconds, 0.5);
  assert.throws(() => validateConfig({ sessionDir: "/tmp" }), /positive integer/);
});

test("tool outputs share the retained-byte budget across asynchronous callbacks", async () => {
  let request: Record<string, unknown> | undefined;
  const broker = new ToolBroker(async (event) => { request = event; }, 1000);
  const tool = broker.definitions([{ name: "tool", description: "test", inputSchema: { type: "object", properties: {} } }],
    { id: "r", type: "prompt", sessionId: "a", runId: "run", message: "hi", profile }, new AbortController().signal)[0]!;
  const budget = { signal: new AbortController().signal, calls: 0, maxModelCalls: 2, retainedBytes: 0, maxRetainedBytes: 100 };
  const invoke = () => runBudget.run(budget, () => tool.execute("native", {}, undefined, undefined, {} as never));
  const first = invoke();
  broker.resolve({ type: "tool_result", toolCallId: request!.toolCallId, sessionId: "a", runId: "run", result: { content: [{ type: "text", text: "x".repeat(15) }] } });
  await first;
  const second = invoke();
  broker.resolve({ type: "tool_result", toolCallId: request!.toolCallId, sessionId: "a", runId: "run", result: { content: [{ type: "text", text: "x".repeat(15) }] } });
  await assert.rejects(second, (error) => error instanceof HostError && error.code === "session_limit");
  broker.close();
});

test("SDK completion requests contribute usage and streamed bytes share one run budget", async () => {
  const pool = new ModelPool(1, 4, 1000);
  const lease = await pool.acquire(profile);
  const faux = fauxProvider({ provider: "openai", models: [{ id: "host-test" }], tokensPerSecond: 0 });
  lease.runtime.registerNativeProvider(faux.provider);
  const response = fauxAssistantMessage("known");
  faux.setResponses([response, fauxAssistantMessage("x".repeat(1000))]);
  const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
  try {
    await runBudget.run({ signal: new AbortController().signal, calls: 0, maxModelCalls: 2, usage },
      () => lease.runtime.completeSimple(faux.getModel(), { messages: [{ role: "user", content: "some input text", timestamp: 1 }] }));
    assert.ok(usage.input > 0); assert.ok(usage.output > 0);
    const oversized = await runBudget.run({ signal: new AbortController().signal, calls: 0, maxModelCalls: 2, retainedBytes: 0, maxRetainedBytes: 50 },
      () => lease.runtime.streamSimple(faux.getModel(), { messages: [] }).result());
    assert.equal(oversized.stopReason, "error"); assert.match(oversized.errorMessage!, /byte limit/);
  } finally { lease.release(); pool.clear(); }
});

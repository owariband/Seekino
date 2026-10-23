import { pathToFileURL } from "node:url";
import type { Readable, Writable } from "node:stream";
import { HostError, JsonlWriter, validateConfig, type Command, type HostConfig, type PromptCommand } from "./protocol.js";
import { SessionRegistry } from "./sessions.js";

export async function serve(input: Readable, output: Writable, diagnostics: Writable = process.stderr): Promise<void> {
  let registry: SessionRegistry | undefined;
  let config: HostConfig | undefined;
  let writer: JsonlWriter | undefined;
  let stopping = false;
  const pending = new Set<Promise<void>>();
  const send = (message: Record<string, unknown>) => {
    if (!writer) throw new HostError("not_initialized", "Initialize the Host first.");
    return writer.send(message);
  };
  const handle = async (command: Command): Promise<void> => {
    try {
      if (command.type === "tool_result") { registry?.tools.resolve(command); return; }
      if (!command.id || typeof command.id !== "string") throw new HostError("protocol_error", "A request id is required.");
      let data: unknown;
      if (command.type === "initialize") {
        if (registry) throw new HostError("already_initialized", "The Host is already initialized.");
        if (command.protocolVersion !== 1) throw new HostError("protocol_error", "Unsupported protocolVersion.");
        config = validateConfig(command.config);
        writer = new JsonlWriter(output, config.maxQueueBytes, config.maxLineBytes);
        registry = new SessionRegistry(config, send); data = { protocolVersion: 1 };
      } else {
        if (!registry || stopping) throw new HostError("not_initialized", "The Host is not accepting requests.");
        if (command.type !== "shutdown" && (!command.sessionId || typeof command.sessionId !== "string"))
          throw new HostError("invalid_request", "sessionId is required.");
        switch (command.type) {
          case "prompt": data = await registry.prompt(command as unknown as PromptCommand); break;
          case "abort": await registry.abort(command.sessionId!, command.runId); data = {}; break;
          case "get_state": data = { state: registry.state(command.sessionId!) ?? command.state ?? null }; break;
          case "confirm_turn":
            if (!command.runId || typeof command.content !== "string") throw new HostError("invalid_request", "runId and content are required.");
            data = { state: registry.confirm(command.sessionId!, command.runId, command.content) }; break;
          case "discard_turn": data = { state: await registry.discard(command.sessionId!, command.runId) }; break;
          case "delete_thread": await registry.delete(command.sessionId!); data = {}; break;
          case "shutdown": stopping = true; await registry.close(); data = {}; break;
          default: throw new HostError("unknown_command", "Unknown Host command.");
        }
      }
      await send({ type: "response", id: command.id, sessionId: command.sessionId, runId: command.runId, success: true, data });
    } catch (error) {
      const payload = { type: "response", id: command.id, sessionId: command.sessionId, runId: command.runId, success: false,
        error: { code: error instanceof HostError ? error.code : "host_error", message: error instanceof Error ? error.message : "Host request failed." } };
      if (!writer) writer = new JsonlWriter(output, 65536, 65536);
      await writer.send(payload).catch(() => { stopping = true; input.destroy(); });
    }
  };
  let buffered = Buffer.alloc(0);
  try {
    for await (const chunk of input) {
      buffered = Buffer.concat([buffered, Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as string)]);
      let boundary: number;
      while ((boundary = buffered.indexOf(10)) >= 0) {
        const line = buffered.subarray(0, boundary); buffered = buffered.subarray(boundary + 1);
        if (line.length > (config?.maxLineBytes ?? 1048576)) throw new HostError("protocol_error", "Input line exceeds the configured limit.");
        if (!line.length) continue;
        const value: unknown = JSON.parse(line.toString("utf8"));
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new HostError("protocol_error", "Commands must be JSON objects.");
        const command = value as Command;
        if (command.type === "initialize") await handle(command);
        else {
          if (pending.size >= (config?.maxLoadedSessions ?? 1) * 4 && command.type !== "abort" && command.type !== "tool_result" && command.type !== "shutdown") {
            await send({ type: "response", id: command.id, success: false, error: { code: "capacity", message: "The command queue is full." } });
            continue;
          }
          const operation = handle(command).finally(() => pending.delete(operation));
          pending.add(operation);
        }
      }
      if (buffered.length > (config?.maxLineBytes ?? 1048576)) throw new HostError("protocol_error", "Input line exceeds the configured limit.");
      if (stopping) break;
    }
    if (buffered.length && !stopping) throw new HostError("protocol_error", "The input ended in a partial JSONL command.");
  } catch (error) {
    if (!stopping) {
      diagnostics.write(`${error instanceof HostError ? error.code : "protocol_error"}: Host input closed.\n`);
      process.exitCode = 1;
    }
  } finally {
    await registry?.close();
    await Promise.allSettled(pending);
    await writer?.flush();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)
  await serve(process.stdin, process.stdout);

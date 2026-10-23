from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import suppress
from contextvars import Context, copy_context
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, Thread, current_thread
from typing import Any
from uuid import uuid4


class PiHostError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Pending:
    generation: int
    fields: dict[str, Any]
    future: asyncio.Future
    context: Context
    on_event: Callable[[dict[str, Any]], Any] | None = None
    on_tool: Callable[[str, dict[str, Any], str], Any] | None = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None
    tools: set[asyncio.Task] = field(default_factory=set)
    failure: Exception | None = None
    failure_task: asyncio.Task | None = None


class PiClient:
    """Multiplex synchronous callers over one managed Pi Host process."""

    def __init__(
        self, command: str = "node", *, args: Sequence[str] | None = None,
        cwd: str | None = None, env: Mapping[str, str] | None = None,
        config: Mapping[str, Any] | None = None, startup_timeout: float = 30,
        cancel_timeout: float = 10, shutdown_timeout: float = 5,
    ) -> None:
        if not command.strip():
            raise ValueError("Pi Host command must not be empty")
        if min(startup_timeout, cancel_timeout, shutdown_timeout) <= 0:
            raise ValueError("Pi Host timeouts must be greater than zero")
        self._command = command
        self._args = tuple(args) if args is not None else (
            str(Path(__file__).parent / "host" / "dist" / "src" / "main.js"),
        )
        self._cwd, self._env = cwd, self._child_environment(env)
        from gateway.config import PiHostConfig

        self._config = {**PiHostConfig().runtime_options(), **dict(config or {})}
        self._max_line_bytes = int(self._config["maxLineBytes"])
        self._max_queue_bytes = int(self._config["maxQueueBytes"])
        self._max_tool_output_bytes = int(self._config["maxToolOutputBytes"])
        callback_workers = int(self._config["maxModelRequests"])
        if min(self._max_line_bytes, self._max_queue_bytes, self._max_tool_output_bytes, callback_workers) <= 0:
            raise ValueError("Pi Host buffer and callback limits must be greater than zero")
        self._startup_timeout, self._cancel_timeout = startup_timeout, cancel_timeout
        self._shutdown_timeout = shutdown_timeout
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._run_loop, name="pi-host", daemon=True)
        self._lock = Lock()
        self._started = self._closed = False
        self._process: asyncio.subprocess.Process | None = None
        self._generation = 0
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[str, _Pending] = {}
        self._queue_bytes = 0
        self._write_bytes = 0
        self._stderr_bytes = 0
        self._callbacks = ThreadPoolExecutor(max_workers=callback_workers, thread_name_prefix="pi-callback")
        self._callback_capacity = asyncio.Semaphore(callback_workers)

    @staticmethod
    def _child_environment(overrides: Mapping[str, str] | None) -> dict[str, str]:
        environment = {**os.environ, **(overrides or {})}
        # Model credentials arrive in the authorized profile, never through an
        # inherited provider fallback. Preserve PATH, proxies and OS settings.
        for name in tuple(environment):
            upper = name.upper()
            if upper.endswith(("_API_KEY", "_API_TOKEN", "_AUTH_TOKEN", "_ACCESS_TOKEN", "_PASSWORD", "_SECRET")) or upper in {
                "ANTHROPIC_AUTH_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_API_KEY", "GEMINI_API_KEY",
                "PI_CODING_AGENT_DIR", "PI_PACKAGE_DIR", "NODE_OPTIONS",
            }:
                environment.pop(name)
        return environment

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.returncode is None else None

    @property
    def stderr_bytes(self) -> int:
        return self._stderr_bytes

    def __enter__(self) -> PiClient:
        self.open()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(self._loop)
            for task in tasks:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def _submit(self, coroutine: Any) -> Future:
        if current_thread() is self._thread:
            coroutine.close()
            raise RuntimeError("Do not block the Pi Host I/O thread")
        with self._lock:
            if self._closed:
                coroutine.close()
                raise PiHostError("closed", "Pi Host client is closed")
            if not self._started:
                self._started = True
                self._thread.start()
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def open(self) -> None:
        self._submit(self._ensure_process()).result()

    def request(self, type: str, *, timeout: float | None = None, **fields: Any) -> dict[str, Any]:
        fields = {"sessionId" if key == "session_id" else "runId" if key == "run_id" else key: value
                  for key, value in fields.items()}
        return self._submit(self._request(type, fields, timeout=timeout or self._startup_timeout)).result()

    def prompt(
        self, session_id: str, run_id: str, profile: Mapping[str, Any], message: str, *,
        history: Sequence[Mapping[str, Any]] = (), system_prompt: str | None = None,
        tools: Sequence[Mapping[str, Any]] = (), state: Mapping[str, Any] | None = None,
        limits: Mapping[str, Any] | None = None, images: Sequence[Mapping[str, Any]] = (),
        on_event: Callable[[dict[str, Any]], Any] | None = None,
        on_tool: Callable[[str, dict[str, Any], str], Any] | None = None,
        check_control: Callable[[], Any] | None = None, timeout: float | None = None,
    ) -> dict[str, Any]:
        if timeout is not None and timeout <= 0:
            raise ValueError("Pi prompt timeout must be greater than zero")
        if check_control is not None:
            check_control()
        fields = {
            "sessionId": session_id, "runId": run_id, "profile": dict(profile), "message": message,
            "history": list(history), "systemPrompt": system_prompt, "tools": list(tools),
            "state": dict(state) if state is not None else None, "limits": dict(limits or {}),
            "images": list(images),
        }
        future = self._submit(self._request("prompt", fields, on_event=on_event, on_tool=on_tool))
        deadline = time.monotonic() + timeout if timeout is not None else None
        try:
            while True:
                try:
                    return future.result(timeout=0.05)
                except FutureTimeout:
                    if future.done():
                        return future.result()
                    if check_control is not None:
                        check_control()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise PiHostError("timeout", "Pi Host prompt timed out")
        except BaseException:
            if not future.done():
                abort = None
                with suppress(Exception):
                    abort = self._submit(self._cancel_run(session_id, run_id))
                    abort.result(self._cancel_timeout + 0.5)
                if abort is not None:
                    abort.cancel()
                future.cancel()
            raise

    async def _cancel_run(self, session_id: str, run_id: str) -> None:
        # Cancellation during startup has no remote run yet. It must not start
        # another Host solely to send an abort after the original startup fails.
        pending = next((item for item in self._pending.values()
                        if item.generation == self._generation
                        and item.fields.get("sessionId") == session_id
                        and item.fields.get("runId") == run_id), None)
        if pending is not None:
            await self._abort_pending(pending)

    async def _ensure_process(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise PiHostError("closed", "Pi Host client is closed")
            if (self._process is not None and self._process.returncode is None
                    and self._reader is not None and not self._reader.done()):
                return
            if self._process is not None:
                await self._stop_process(self._process)
            try:
                process = await asyncio.create_subprocess_exec(
                    self._command, *self._args, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=self._cwd, env=self._env, limit=self._max_line_bytes + 1,
                )
            except OSError as exc:
                raise PiHostError("start_failed", "Unable to start Pi Host; check its command and build") from exc
            self._process = process
            self._generation += 1
            generation = self._generation
            self._reader = asyncio.create_task(self._read_messages(process, generation))
            self._stderr_task = asyncio.create_task(self._drain_stderr(process))
            try:
                result = await self._exchange("initialize", {"protocolVersion": 1, "config": self._config},
                                              timeout=self._startup_timeout)
                if result.get("protocolVersion") != 1:
                    raise PiHostError("protocol_error", "Unsupported Pi Host protocol version")
            except BaseException:
                await self._stop_process(process)
                raise

    async def _request(
        self, type: str, fields: dict[str, Any], *, timeout: float | None = None,
        on_event: Callable | None = None, on_tool: Callable | None = None,
    ) -> dict[str, Any]:
        await self._ensure_process()
        return await self._exchange(type, fields, timeout=timeout, on_event=on_event, on_tool=on_tool)

    async def _exchange(
        self, type: str, fields: dict[str, Any], *, timeout: float | None = None,
        on_event: Callable | None = None, on_tool: Callable | None = None,
    ) -> dict[str, Any]:
        request_id = f"{self._generation}:{uuid4().hex}"
        pending = _Pending(self._generation, fields, self._loop.create_future(), copy_context(), on_event, on_tool)
        self._pending[request_id] = pending
        pending.worker = asyncio.create_task(self._deliver(pending))
        try:
            await self._write({**fields, "id": request_id, "type": type}, pending.generation)
            if timeout is None:
                return await pending.future
            try:
                return await asyncio.wait_for(pending.future, timeout)
            except TimeoutError:
                raise PiHostError("timeout", f"Pi Host {type} timed out") from None
        finally:
            self._pending.pop(request_id, None)
            pending.worker.cancel()
            if pending.failure_task is not None:
                pending.failure_task.cancel()
            for task in tuple(pending.tools):
                task.cancel()
            await asyncio.gather(pending.worker, *pending.tools,
                                 *([pending.failure_task] if pending.failure_task is not None else []),
                                 return_exceptions=True)
            while not pending.queue.empty():
                size, _message = pending.queue.get_nowait()
                self._queue_bytes -= size

    async def _write(self, message: dict[str, Any], generation: int) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > self._max_line_bytes:
            raise PiHostError("message_too_large", "Pi Host message exceeds maxLineBytes")
        if self._write_bytes + len(encoded) > self._max_queue_bytes:
            raise PiHostError("backpressure", "Pi Host outgoing queue is full")
        self._write_bytes += len(encoded)
        try:
            async with self._write_lock:
                process = self._process
                if generation != self._generation or process is None or process.returncode is not None:
                    raise PiHostError("host_exited", "Pi Host process exited")
                try:
                    process.stdin.write(encoded)
                    await asyncio.wait_for(process.stdin.drain(), self._startup_timeout)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    raise PiHostError("host_exited", "Pi Host pipe is unavailable") from None
        finally:
            self._write_bytes -= len(encoded)

    async def _read_messages(self, process: asyncio.subprocess.Process, generation: int) -> None:
        error = PiHostError("host_exited", "Pi Host process exited")
        try:
            while line := await process.stdout.readline():
                if len(line) > self._max_line_bytes:
                    raise PiHostError("message_too_large", "Pi Host output exceeds maxLineBytes")
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    raise PiHostError("protocol_error", "Pi Host emitted invalid JSONL") from None
                if not isinstance(message, dict) or message.get("type") not in {"response", "event", "tool_request"}:
                    raise PiHostError("protocol_error", "Pi Host emitted an invalid message")
                if not isinstance(message.get("id"), str):
                    raise PiHostError("protocol_error", "Pi Host emitted an invalid request ID")
                pending = self._pending.get(message.get("id"))
                if pending is None or pending.generation != generation or pending.future.done():
                    continue
                for key in ("sessionId", "runId"):
                    if key in message and message[key] != pending.fields.get(key):
                        raise PiHostError("protocol_error", "Pi Host returned mismatched request ownership")
                if self._queue_bytes + len(line) > self._max_queue_bytes:
                    pending.future.set_exception(PiHostError("backpressure", "Pi Host incoming queue is full"))
                    if pending.fields.get("runId"):
                        asyncio.create_task(self._abort_pending(pending))
                    continue
                self._queue_bytes += len(line)
                pending.queue.put_nowait((len(line), message))
        except asyncio.CancelledError:
            return
        except PiHostError as exc:
            error = exc
        except (ValueError, OSError):
            error = PiHostError("protocol_error", "Pi Host output stream failed")
        finally:
            for pending in tuple(self._pending.values()):
                if pending.generation == generation and not pending.future.done():
                    pending.future.set_exception(pending.failure or error)
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()

    async def _abort_pending(self, pending: _Pending) -> None:
        if pending.generation != self._generation:
            return
        with suppress(Exception):
            await self._exchange("abort", {key: pending.fields[key] for key in ("sessionId", "runId")},
                                 timeout=self._cancel_timeout)

    def _fail_callback(self, pending: _Pending, error: Exception) -> None:
        if pending.failure is None and not pending.future.done():
            pending.failure = error
            pending.failure_task = asyncio.create_task(self._settle_callback_failure(pending))

    async def _settle_callback_failure(self, pending: _Pending) -> None:
        abort = asyncio.create_task(self._abort_pending(pending))
        try:
            # Pi reports its final usage while aborting. Keep this request's
            # reader alive briefly, then return the original callback failure.
            await asyncio.wait_for(asyncio.shield(pending.future), self._cancel_timeout)
        except TimeoutError:
            if not pending.future.done():
                pending.future.set_exception(pending.failure)
        except Exception:
            pass
        finally:
            abort.cancel()
            await asyncio.gather(abort, return_exceptions=True)

    async def _deliver(self, pending: _Pending) -> None:
        try:
            while True:
                size, message = await pending.queue.get()
                if message["type"] == "event":
                    self._queue_bytes -= size
                    event = message.get("event", {})
                    if pending.failure is not None and event.get("type") != "usage":
                        continue
                    if pending.on_event is not None:
                        try:
                            await self._callback(pending.context, pending.on_event, event)
                        except Exception as exc:
                            self._fail_callback(pending, exc)
                elif message["type"] == "tool_request":
                    if pending.failure is not None:
                        self._queue_bytes -= size
                        continue
                    task = asyncio.create_task(self._tool_result(pending, message))
                    pending.tools.add(task)
                    task.add_done_callback(pending.tools.discard)
                    task.add_done_callback(lambda _task, size=size: setattr(self, "_queue_bytes", self._queue_bytes - size))
                else:
                    self._queue_bytes -= size
                    if pending.failure is not None:
                        if not pending.future.done():
                            pending.future.set_exception(pending.failure)
                        return
                    if message.get("success") is not True:
                        error = message.get("error") or {}
                        if not isinstance(error, dict):
                            error = {}
                        raise PiHostError(str(error.get("code", "request_failed")),
                                          str(error.get("message", "Pi Host request failed")))
                    result = message.get("data") or {}
                    if not isinstance(result, dict):
                        raise PiHostError("protocol_error", "Pi Host returned invalid response data")
                    if not pending.future.done():
                        pending.future.set_result(result)
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not pending.future.done():
                pending.future.set_exception(pending.failure or exc)

    async def _callback(self, context: Context, callback: Callable, *args: Any) -> Any:
        await self._callback_capacity.acquire()
        release_here = True
        try:
            future = self._callbacks.submit(context.copy().run, callback, *args)
            # Cancelling an asyncio waiter cannot stop a synchronous tool. Keep
            # its capacity reserved until the worker has actually returned.
            try:
                result = await asyncio.shield(asyncio.wrap_future(future))
            except asyncio.CancelledError:
                release_here = False

                def finished(worker: Future) -> None:
                    with suppress(Exception):
                        returned = worker.result()
                        if isinstance(returned, Future):
                            returned.cancel()
                        elif inspect.iscoroutine(returned):
                            returned.close()
                    if not self._loop.is_closed():
                        with suppress(RuntimeError):
                            self._loop.call_soon_threadsafe(self._callback_capacity.release)

                future.add_done_callback(finished)
                raise
            if not isinstance(result, Future) and not inspect.isawaitable(result):
                return result

            async def resolve(value: Any) -> Any:
                if isinstance(value, Future):
                    value = await asyncio.wrap_future(value)
                while inspect.isawaitable(value):
                    value = await value
                return value

            return await asyncio.create_task(resolve(result), context=context.copy())
        finally:
            if release_here:
                self._callback_capacity.release()

    async def _tool_result(self, pending: _Pending, message: dict[str, Any]) -> None:
        is_error = False
        try:
            if pending.on_tool is None:
                raise PiHostError("tool_unavailable", "No tool callback is registered")
            result = await self._callback(pending.context, pending.on_tool, message["name"],
                                          message.get("arguments", {}), message["toolCallId"])
            if not isinstance(result, dict):
                raise ValueError("tool callback must return a content object")
            result = dict(result)
            is_error = bool(result.pop("isError", False))
            if len(json.dumps(result, ensure_ascii=False).encode()) > self._max_tool_output_bytes:
                raise PiHostError("tool_output_too_large", "Tool output exceeds maxToolOutputBytes")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The adapter returns ordinary tool errors as content. Exceptions
            # retain cancellation, authorization and business failure semantics.
            self._fail_callback(pending, exc)
            return
        try:
            await self._write({
                "type": "tool_result", "sessionId": pending.fields.get("sessionId"),
                "runId": pending.fields.get("runId"), "toolCallId": message["toolCallId"],
                "result": result, "isError": is_error,
            }, pending.generation)
        except Exception as exc:
            asyncio.create_task(self._abort_pending(pending))
            if not pending.future.done():
                pending.future.set_exception(exc)

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        while chunk := await process.stderr.read(4096):
            # Do not retain stderr: SDK failures may contain request secrets.
            self._stderr_bytes += len(chunk)

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        if process.stdin is not None:
            process.stdin.close()
        for action in (None, process.terminate, process.kill):
            if action is not None:
                with suppress(ProcessLookupError):
                    action()
            try:
                await asyncio.wait_for(process.wait(), self._shutdown_timeout)
                break
            except TimeoutError:
                if process.returncode is not None:
                    break
        if process.returncode is None:
            raise PiHostError("shutdown_failed", "Pi Host did not exit after kill")
        if self._process is process:
            self._process = None
            for task in (self._reader, self._stderr_task):
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
            await asyncio.gather(*(task for task in (self._reader, self._stderr_task)
                                   if task is not None and task is not asyncio.current_task()), return_exceptions=True)
        if process.returncode is not None:
            process._transport.close()

    def close(self) -> None:
        if current_thread() is self._thread:
            raise RuntimeError("Close Pi Host outside its I/O thread")
        with self._lock:
            if self._closed:
                return
            self._closed = True
            started = self._started
        try:
            if started:
                asyncio.run_coroutine_threadsafe(self._close(), self._loop).result()
            else:
                self._loop.close()
        finally:
            self._callbacks.shutdown(wait=False, cancel_futures=True)
            if started:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join()

    async def _close(self) -> None:
        async with self._start_lock:
            process = self._process
            if process is not None:
                with suppress(Exception):
                    await self._exchange("shutdown", {}, timeout=self._shutdown_timeout)
                await self._stop_process(process)
        for pending in tuple(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(PiHostError("closed", "Pi Host client is closed"))

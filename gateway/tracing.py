from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import select
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry import trace
    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from gateway.config import TracingConfig, _tracing_base_url
from gateway.usage import ModelCallTracker, ModelCallUsage, TokenUsage, capture_model_usage, model_call_context

_logger = logging.getLogger(__name__)
_CONTEXT_FIELDS = frozenset({
    "tenant_id", "series_id", "series_ids", "video_id", "video_ids",
    "video_parse_task_id", "video_parse_task_ids", "chunk_parse_task_id",
    "interval_parse_task_id", "video_chunk_id", "sequence_no", "category", "stage",
    "lease_version", "attempt_count", "retry_count", "conversation_id", "user_message_id",
    "assistant_message_id", "session_id", "run_id", "task_id", "job_id", "execution_id",
    "trace_id", "parent_span_id", "retrieval_contract_id", "index_generation",
    "ledger_call_id",
})
_MAX_FRAME_BYTES = 65_536


def _context_attributes(context: Mapping[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for name in _CONTEXT_FIELDS:
        value = context.get(name)
        if isinstance(value, (str, int, bool)) or isinstance(value, float) and math.isfinite(value):
            attributes[name] = value[:256] if isinstance(value, str) else value
        elif isinstance(value, (list, tuple)):
            items = [
                item[:256] if isinstance(item, str) else item
                for item in value[:64] if isinstance(item, (str, int, bool))
            ]
            while items and len(json.dumps(items, ensure_ascii=False)) > 4096:
                items.pop()
            attributes[name] = json.dumps(items, ensure_ascii=False)
    return attributes


def _parent_context(context: Mapping[str, Any], parent: trace.Span | None = None) -> Context:
    from opentelemetry import trace
    from opentelemetry.context import Context
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    if parent is not None:
        return trace.set_span_in_context(parent, Context())
    trace_id, span_id = context.get("trace_id"), context.get("parent_span_id")
    if (
        isinstance(trace_id, str) and re.fullmatch(r"[0-9a-f]{32}", trace_id)
        and isinstance(span_id, str) and re.fullmatch(r"[0-9a-f]{16}", span_id)
        and int(trace_id, 16) and int(span_id, 16)
    ):
        remote = NonRecordingSpan(SpanContext(
            trace_id=int(trace_id, 16), span_id=int(span_id, 16),
            is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ))
        return trace.set_span_in_context(remote, Context())
    return Context()


def _usage_details(usage: TokenUsage) -> dict[str, int]:
    """Convert inclusive provider counts into Langfuse's exclusive price buckets."""

    read, write = usage.cached_input_tokens, usage.cache_write_input_tokens
    details = {
        "input": usage.input_tokens - (read or 0) - (write or 0),
        "output": usage.output_tokens,
        "total": usage.total_tokens,
    }
    if read is not None:
        details["input_cached_tokens"] = read
    if write is not None:
        ttl_counts = {
            "input_cache_creation_5m_tokens": usage.cache_write_5m_input_tokens,
            "input_cache_creation_1h_tokens": usage.cache_write_1h_input_tokens,
        }
        reported_ttls = {name: value for name, value in ttl_counts.items() if value is not None}
        if reported_ttls and sum(reported_ttls.values()) == write:
            details.update(reported_ttls)
        else:
            details["input_cache_creation_tokens"] = write
    modalities = {
        "input_text_tokens": usage.text_input_tokens,
        "input_image_tokens": usage.image_input_tokens,
        "input_audio_tokens": usage.audio_input_tokens,
    }
    reported_modalities = {name: value for name, value in modalities.items() if value is not None}
    # A cached token may also be a text/image/audio token. Without their reported
    # intersections, preserve those dimensions as metadata rather than charge twice.
    if not read and not write and sum(reported_modalities.values()) <= details["input"]:
        details.update(reported_modalities)
        details["input"] -= sum(reported_modalities.values())
    return details


def _record_usage(
    span: trace.Span, usage: TokenUsage | None, *, billable: bool = True,
    cache_write_ttl_required: bool = False,
) -> None:
    span.set_attribute("langfuse.observation.metadata.usage_status", "known" if usage is not None else "unknown")
    if usage is None:
        return
    if billable:
        span.set_attribute("langfuse.observation.usage_details", json.dumps(_usage_details(usage)))
        span.set_attribute("langfuse.observation.metadata.usage_details_semantics", "exclusive_buckets")
        modality_total = sum(value or 0 for value in (
            usage.text_input_tokens, usage.image_input_tokens, usage.audio_input_tokens,
        ))
        incomplete = modality_total > usage.input_tokens or (
            modality_total > 0 and bool(usage.cached_input_tokens or usage.cache_write_input_tokens)
        )
        if cache_write_ttl_required and usage.cache_write_input_tokens:
            incomplete |= (
                (usage.cache_write_5m_input_tokens or 0) + (usage.cache_write_1h_input_tokens or 0)
                != usage.cache_write_input_tokens
            )
        span.set_attribute("langfuse.observation.metadata.usage_pricing_status", "incomplete" if incomplete else "complete")
    subsets = {
        name: getattr(usage, name) for name in usage.__dataclass_fields__
        if name not in {"input_tokens", "output_tokens", "total_tokens"}
        and getattr(usage, name) is not None
    }
    if subsets:
        span.set_attribute("langfuse.observation.metadata.usage_subsets", json.dumps(subsets))
        span.set_attribute("langfuse.observation.metadata.usage_subsets_semantics", "reasoning_tokens:output_subset;cache_write_TTLs:cache_write_subset;other_fields:input_subsets;overlaps_allowed")


class _ProcessExporter:
    """Only IPC runs in the host; an owned process performs HTTP exports."""

    def __init__(self, settings: TracingConfig, public_key: str, secret_key: str, base_url: str) -> None:
        self._settings = settings
        self._queue: Queue[bytes] = Queue(settings.queue_size)
        self._stop, self._closing = Event(), Event()
        self._lock = Lock()
        self._submitted = self._processed = self._exported = self._failed = self._dropped = 0
        self.report: dict[str, Any] | None = None
        self._socket, child = socket.socketpair()
        child_environment = {
            key: value for key, value in os.environ.items()
            if key.upper() in {"PATH", "PYTHONPATH", "SYSTEMROOT", "WINDIR", "SSL_CERT_FILE",
                               "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        }
        process = None
        try:
            self._socket.setblocking(False)
            self._process = subprocess.Popen(
                [sys.executable, "-m", "gateway.tracing", str(child.fileno())],
                cwd=Path(__file__).resolve().parent.parent, pass_fds=(child.fileno(),),
                env=child_environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            process = self._process
            configuration = json.dumps({
                "endpoint": f"{base_url}/api/public/otel/v1/traces",
                "authorization": "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode(),
                "timeout": settings.export_timeout_seconds,
            }).encode()
            configuration_frame = struct.pack("!I", len(configuration)) + configuration
            self._thread = Thread(target=self._pump, args=(configuration_frame,), name="anivag-trace-ipc", daemon=True)
            self._thread.start()
        except Exception:
            self._closing.set()
            self._stop.set()
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
            if process is not None:
                deadline = time.monotonic() + settings.close_timeout_seconds
                self._reap_process(deadline)
            thread = getattr(self, "_thread", None)
            if thread is not None and thread.is_alive():
                thread.join(max(0.001, deadline - time.monotonic()))
            raise
        finally:
            child.close()

    def _reap_process(self, deadline: float) -> None:
        if self._process.poll() is None:
            try:
                self._process.terminate()
            except OSError:
                pass
        try:
            self._process.wait(timeout=max(0.001, (deadline - time.monotonic()) / 2))
        except subprocess.TimeoutExpired:
            try:
                self._process.kill()
            except OSError:
                pass
            try:
                self._process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _logger.error("Model trace exporter process did not exit within the configured close deadline")

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        from opentelemetry.sdk.trace.export import SpanExportResult

        success = True
        with self._lock:
            if self._closing.is_set():
                self._dropped += len(spans)
                return SpanExportResult.FAILURE
            for span in spans:
                payload = encode_spans([span]).SerializeToString()
                if len(payload) > _MAX_FRAME_BYTES:
                    self._dropped += 1
                    success = False
                    continue
                try:
                    self._queue.put_nowait(struct.pack("!I", len(payload)) + payload)
                    self._submitted += 1
                except Full:
                    self._dropped += 1
                    success = False
            if not success:
                _logger.warning("Model trace export queue rejected observations; model result is preserved")
        return SpanExportResult.SUCCESS if success else SpanExportResult.FAILURE

    def _pump(self, configuration_frame: bytes) -> None:
        pending, replies = configuration_frame, b""
        end_sent = False
        while not self._stop.is_set():
            if not pending and not end_sent:
                try:
                    pending = self._queue.get_nowait()
                except Empty:
                    if self._closing.is_set():
                        pending, end_sent = bytes(4), True
            try:
                readable, writable, _ = select.select(
                    [self._socket], [self._socket] if pending else [], [], 0.02,
                )
                if readable:
                    chunk = self._socket.recv(4096)
                    if not chunk:
                        break
                    replies += chunk
                    while len(replies) >= 24:
                        self._processed, self._exported, self._failed = struct.unpack("!QQQ", replies[:24])
                        replies = replies[24:]
                if writable:
                    sent = self._socket.send(pending)
                    pending = pending[sent:]
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                break

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        deadline = time.monotonic() + timeout_millis / 1000
        while self._processed < self._submitted and time.monotonic() < deadline:
            if self._process.poll() is not None:
                return False
            self._stop.wait(min(0.02, max(0, deadline - time.monotonic())))
        return self._processed == self._submitted and self._failed == 0 and self._dropped == 0

    def shutdown(self) -> None:
        with self._lock:
            if self.report is not None or self._closing.is_set():
                return
            started = time.monotonic()
            deadline = started + self._settings.close_timeout_seconds
            reserve = min(0.2, self._settings.close_timeout_seconds / 3)
            self._closing.set()
        self._thread.join(max(0, deadline - reserve - time.monotonic()))
        try:
            self._process.wait(timeout=max(0.001, deadline - reserve - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        forced = self._process.poll() is None
        self._stop.set()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._reap_process(deadline)
        self._thread.join(max(0.001, deadline - time.monotonic()))
        self._socket.close()
        with self._lock:
            self.report = {
                "exported": self._exported, "failed": self._failed,
                "dropped": self._dropped, "unconfirmed": self._submitted - self._processed,
                "forced": forced, "worker_exited": self._process.poll() is not None,
                "ipc_thread_exited": not self._thread.is_alive(),
                "elapsed_seconds": time.monotonic() - started,
            }
            if forced or self.report["failed"] or self.report["dropped"] or self.report["unconfirmed"]:
                _logger.warning(
                    "Model tracing closed with incomplete export: forced=%s failed=%s dropped=%s unconfirmed=%s worker_exited=%s ipc_thread_exited=%s",
                    forced, self.report["failed"], self.report["dropped"], self.report["unconfirmed"],
                    self.report["worker_exited"], self.report["ipc_thread_exited"],
                )
            if not self.report["worker_exited"] or not self.report["ipc_thread_exited"]:
                _logger.error("Model tracing resources did not exit within the configured close deadline")


class LangfuseTracing:
    """Translate Gateway records to Langfuse OTLP without owning business state."""

    def __init__(self, settings: TracingConfig, *, environ: Mapping[str, str] | None = None,
                 exporter: SpanExporter | None = None) -> None:
        self._settings, self._environ = settings, os.environ if environ is None else environ
        self._exporter = exporter
        self._provider: TracerProvider | None = None
        self._tracer: trace.Tracer | None = None
        self._lock = Lock()
        self._closed = self._unavailable = False
        self._scope_span: ContextVar[Any] = ContextVar("gateway_trace_scope", default=None)

    def _ensure_tracer(self) -> trace.Tracer | None:
        with self._lock:
            if self._closed or self._unavailable or not self._settings.enabled:
                return None
            if self._tracer is not None:
                return self._tracer
            try:
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace import SpanLimits, TracerProvider
                from opentelemetry.sdk.trace.export import SimpleSpanProcessor

                if self._exporter is None:
                    base_url = self._settings.base_url or self._environ.get("LANGFUSE_BASE_URL")
                    public_key = self._environ.get(self._settings.public_key_env)
                    secret_key = self._environ.get(self._settings.secret_key_env)
                    if not base_url or not public_key or not secret_key:
                        raise ValueError("missing tracing configuration")
                    self._exporter = _ProcessExporter(
                        self._settings, public_key, secret_key, _tracing_base_url(base_url),
                    )
                self._provider = TracerProvider(
                    resource=Resource({"service.name": "anivag-gateway"}), shutdown_on_exit=False,
                    span_limits=SpanLimits(max_attributes=128, max_attribute_length=4096, max_events=0),
                )
                self._provider.add_span_processor(SimpleSpanProcessor(self._exporter))
                self._tracer = self._provider.get_tracer("anivag.gateway", "1")
            except Exception:
                self._unavailable = True
                if self._exporter is not None:
                    try:
                        self._exporter.shutdown()
                    except Exception:
                        _logger.warning("Model tracing initialization cleanup failed")
                _logger.warning("Model tracing initialization failed; model result and ledger are preserved")
            return self._tracer

    @contextmanager
    def scope(self, context: Mapping[str, Any]) -> Iterator[None]:
        parent = self._scope_span.get()
        span = None
        token = None
        try:
            tracer = self._ensure_tracer()
            if tracer is not None and parent is None:
                attributes = {
                    "langfuse.observation.type": "span", "langfuse.trace.name": "anivag.execute",
                    "langfuse.environment": self._settings.environment,
                }
                if self._settings.release:
                    attributes["langfuse.release"] = self._settings.release
                for name, value in _context_attributes(context).items():
                    attributes[f"langfuse.trace.metadata.{name}"] = value
                    attributes[f"langfuse.observation.metadata.{name}"] = value
                session = context.get("session_id") or context.get("conversation_id")
                if isinstance(session, (str, int)):
                    attributes["session.id"] = str(session)[:256]
                    attributes["langfuse.session.id"] = str(session)[:256]
                span = tracer.start_span("anivag.execute", context=_parent_context(context), attributes=attributes)
                token = self._scope_span.set(span)
        except Exception:
            _logger.warning("Model tracing scope creation failed; model result and ledger are preserved")
        try:
            with model_call_context(context), capture_model_usage(self, combine=True):
                yield
        except BaseException:
            if span is not None:
                try:
                    from opentelemetry.trace import Status, StatusCode

                    span.set_attribute("langfuse.observation.level", "ERROR")
                    span.set_status(Status(StatusCode.ERROR, "execution_failed"))
                except Exception:
                    _logger.warning("Model tracing scope status failed; business error is preserved")
            raise
        finally:
            if span is not None:
                try:
                    span.end()
                except Exception:
                    _logger.warning("Model tracing scope completion failed; model result and ledger are preserved")
            if token is not None:
                self._scope_span.reset(token)

    def __call__(self, call: ModelCallUsage) -> None:
        # A failed start hook must not create a second observation at the wrong time.
        pass

    def on_start(self, tracker: ModelCallTracker):
        tracer = self._ensure_tracer()
        if tracer is None:
            return self
        attributes = {
            "langfuse.observation.type": (
                "span" if tracker.operation == "speaker_diarization" else
                "embedding" if tracker.operation in {"text_embedding", "image_embedding", "video_embedding", "speaker_embedding"} else "generation"
            ),
            "langfuse.observation.model.name": tracker.model[:256],
            "langfuse.environment": self._settings.environment,
            "langfuse.observation.metadata.call_id": tracker.call_id,
            "langfuse.observation.metadata.provider": tracker.provider[:256],
            "langfuse.observation.metadata.operation": tracker.operation[:256],
        }
        if self._settings.release:
            attributes["langfuse.release"] = self._settings.release
        for name, value in _context_attributes(tracker.context).items():
            attributes[f"langfuse.observation.metadata.{name}"] = value
            attributes[f"langfuse.trace.metadata.{name}"] = value
        session = tracker.context.get("session_id") or tracker.context.get("conversation_id")
        if isinstance(session, (str, int)):
            attributes["session.id"] = str(session)[:256]
            attributes["langfuse.session.id"] = str(session)[:256]
        span = tracer.start_span(
            tracker.operation[:256], context=_parent_context(tracker.context, self._scope_span.get()),
            start_time=tracker.started_at * 1_000_000, attributes=attributes,
        )

        def finish(call: ModelCallUsage) -> None:
            from opentelemetry.trace import Status, StatusCode

            try:
                span.set_attributes({
                    "langfuse.observation.model.name": call.model[:256],
                    "langfuse.observation.metadata.status": call.status,
                    "langfuse.observation.metadata.granularity": call.granularity,
                    "langfuse.observation.metadata.usage_status": "known" if call.usage is not None else "unknown",
                })
                if call.request_id is not None:
                    span.set_attribute("langfuse.observation.metadata.request_id", call.request_id[:256])
                if call.usage_source is not None:
                    span.set_attribute("langfuse.observation.metadata.usage_source", call.usage_source[:256])
                if call.usage_scope is not None:
                    span.set_attribute("langfuse.observation.metadata.usage_scope", call.usage_scope[:256])
                if call.sdk_model_usage:
                    span.set_attribute("langfuse.observation.type", "span")
                _record_usage(span, call.usage, billable=not call.sdk_model_usage,
                              cache_write_ttl_required=call.provider == "claude_code")
                for index, model_usage in enumerate(call.sdk_model_usage):
                    model_attributes = {
                        **attributes,
                        "langfuse.observation.type": "generation",
                        "langfuse.observation.model.name": model_usage.model[:256],
                        "langfuse.observation.metadata.status": call.status,
                        "langfuse.observation.metadata.granularity": "sdk_turn_run",
                        "langfuse.observation.metadata.usage_source": "claude_model_usage",
                        "langfuse.observation.metadata.usage_scope": "sdk_model",
                        "langfuse.observation.metadata.sdk_model_index": index,
                        "langfuse.observation.metadata.cost_source": "sdk_estimate" if model_usage.cost_usd is not None else "unknown",
                    }
                    child = tracer.start_span(
                        f"{call.operation[:240]}.model", context=_parent_context(call.context, span),
                        start_time=call.started_at * 1_000_000, attributes=model_attributes,
                    )
                    try:
                        _record_usage(child, model_usage.usage, cache_write_ttl_required=call.provider == "claude_code")
                        if model_usage.cost_usd is not None:
                            child.set_attribute("langfuse.observation.cost_details", json.dumps({"total": model_usage.cost_usd}))
                        if call.status == "failed":
                            child.set_attribute("langfuse.observation.level", "ERROR")
                            child.set_status(Status(StatusCode.ERROR, "model_call_failed"))
                    finally:
                        child.end(end_time=call.ended_at * 1_000_000 if call.ended_at is not None else None)
                if call.status == "failed":
                    span.set_attribute("langfuse.observation.level", "ERROR")
                    span.set_status(Status(StatusCode.ERROR, "model_call_failed"))
            finally:
                span.end(end_time=call.ended_at * 1_000_000 if call.ended_at is not None else None)

        return finish

    @property
    def close_report(self) -> Mapping[str, Any] | None:
        return self._exporter.report if isinstance(self._exporter, _ProcessExporter) else None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            provider = self._provider
        if provider is not None:
            try:
                provider.shutdown()
            except Exception:
                _logger.warning("Model tracing close failed; model result and ledger are preserved")


def _read_exact(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            return b""
        result.extend(chunk)
    return bytes(result)


def _export_worker(fd: int) -> None:
    import httpx

    connection = socket.socket(fileno=fd)
    header = _read_exact(connection, 4)
    if not header:
        return
    size = struct.unpack("!I", header)[0]
    if size > _MAX_FRAME_BYTES:
        return
    configuration = json.loads(_read_exact(connection, size))
    processed = exported = failed = 0
    with connection, httpx.Client(timeout=configuration["timeout"]) as client:
        while True:
            header = _read_exact(connection, 4)
            if not header:
                break
            size = struct.unpack("!I", header)[0]
            if size == 0 or size > _MAX_FRAME_BYTES:
                break
            payload = _read_exact(connection, size)
            if not payload:
                break
            try:
                response = client.post(configuration["endpoint"], content=payload, headers={
                    "Authorization": configuration["authorization"],
                    "Content-Type": "application/x-protobuf",
                    "x-langfuse-ingestion-version": "4",
                })
                response.raise_for_status()
                if response.content:
                    if "json" in response.headers.get("content-type", ""):
                        result = response.json()
                        partial = result.get("partialSuccess", result.get("partial_success", {}))
                        rejected = int(partial.get("rejectedSpans", partial.get("rejected_spans", 0)))
                    else:
                        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

                        result = ExportTraceServiceResponse.FromString(response.content)
                        rejected = result.partial_success.rejected_spans
                    if rejected:
                        raise ValueError("collector rejected spans")
                exported += 1
            except Exception:
                failed += 1
            processed += 1
            connection.sendall(struct.pack("!QQQ", processed, exported, failed))


if __name__ == "__main__":
    _export_worker(int(sys.argv[1]))

import asyncio
import json
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gateway.config import TracingConfig
from gateway.tracing import LangfuseTracing
from gateway.usage import SDKModelUsage, TokenUsage, capture_model_usage, track_model_call


def test_disabled_and_missing_configuration_keep_ledger_without_resources(caplog) -> None:
    for settings in (TracingConfig(), TracingConfig(enabled=True)):
        tracing = LangfuseTracing(settings, environ={})
        calls = []
        with capture_model_usage(calls.append), tracing.scope({"run_id": "run"}):
            with track_model_call(provider="provider", model="model", operation="generate"):
                pass
        tracing.close()
        assert len(calls) == 1
        assert tracing._provider is None
        assert tracing.close_report is None
    assert "missing tracing configuration" not in caplog.text
    assert "initialization failed" in caplog.text


def test_generation_preserves_parent_actual_model_time_usage_and_opaque_context() -> None:
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(TracingConfig(enabled=True, environment="test", release="revision"), exporter=exporter)
    global_provider = trace.get_tracer_provider()
    calls = []
    remote_trace, remote_parent = "1" * 32, "2" * 16
    with capture_model_usage(calls.append), tracing.scope({
        "run_id": "outer", "session_id": "conversation", "trace_id": remote_trace,
        "parent_span_id": remote_parent, "prompt": "PRIVATE-PROMPT", "cost_cny": "1.50",
        "cost_currency": "CNY", "job_id": "job",
    }), tracing.scope({"stage": "understand"}):
        with track_model_call(provider="provider", model="requested", operation="generate") as tracker:
            tracker.model = "actual"
            tracker.request_id = "request"
            tracker.usage = TokenUsage(100, 30, 130, 40, 20, audio_input_tokens=10)
    spans = exporter.get_finished_spans()
    tracing.close()
    assert len(spans) == 2
    generation, root = spans
    call = calls[0]
    assert generation.parent.span_id == root.context.span_id
    assert generation.context.trace_id == int(remote_trace, 16)
    assert root.parent.span_id == int(remote_parent, 16)
    assert root.parent.is_remote
    assert root.attributes["langfuse.environment"] == "test"
    assert root.attributes["langfuse.release"] == "revision"
    assert generation.start_time == call.started_at * 1_000_000
    assert generation.end_time == call.ended_at * 1_000_000
    attributes = generation.attributes
    assert attributes["langfuse.observation.model.name"] == "actual"
    assert attributes["langfuse.observation.metadata.call_id"] == call.call_id
    assert attributes["langfuse.observation.metadata.usage_source"] == "provider_usage"
    assert attributes["langfuse.observation.metadata.stage"] == "understand"
    assert attributes["langfuse.trace.metadata.run_id"] == "outer"
    assert attributes["session.id"] == "conversation"
    assert json.loads(attributes["langfuse.observation.usage_details"]) == {"input": 60, "input_cached_tokens": 40, "output": 30, "total": 130}
    assert json.loads(attributes["langfuse.observation.metadata.usage_subsets"])["cached_input_tokens"] == 40
    assert "langfuse.observation.cost_details" not in attributes
    assert attributes["langfuse.observation.metadata.job_id"] == "job"
    assert "langfuse.observation.metadata.cost_currency" not in attributes
    assert "langfuse.observation.metadata.cost_cny" not in attributes
    assert "PRIVATE-PROMPT" not in str(attributes)
    assert "langfuse.observation.input" not in attributes
    assert "langfuse.observation.output" not in attributes
    assert trace.get_tracer_provider() is global_provider


def test_failed_native_aggregate_preserves_unknown_without_zero() -> None:
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(TracingConfig(enabled=True), exporter=exporter)
    with tracing.scope({}), pytest.raises(ValueError):
        with track_model_call(provider="codex", model="model", operation="agent",
                              granularity="sdk_turn_run", usage_source="codex_thread_total"):
            raise ValueError("PRIVATE-ERROR")
    generation = exporter.get_finished_spans()[0]
    tracing.close()
    assert generation.attributes["langfuse.observation.metadata.granularity"] == "sdk_turn_run"
    assert generation.attributes["langfuse.observation.metadata.usage_status"] == "unknown"
    assert generation.attributes["langfuse.observation.level"] == "ERROR"
    assert "langfuse.observation.usage_details" not in generation.attributes
    assert "PRIVATE-ERROR" not in str(generation.attributes)


def test_embedding_keeps_explicit_zero_and_embedding_type() -> None:
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(TracingConfig(enabled=True), exporter=exporter)
    with tracing.scope({}):
        with track_model_call(provider="ark", model="model", operation="text_embedding") as tracker:
            tracker.usage = TokenUsage(0, 0, 0, None, None)
    embedding = exporter.get_finished_spans()[0]
    tracing.close()
    assert embedding.attributes["langfuse.observation.type"] == "embedding"
    assert json.loads(embedding.attributes["langfuse.observation.usage_details"]) == {"input": 0, "output": 0, "total": 0}


@pytest.mark.parametrize("usage,expected", [
    (TokenUsage(20, 4, 24, 5, None, cache_write_input_tokens=3), {
        "input": 12, "input_cached_tokens": 5, "input_cache_creation_tokens": 3, "output": 4, "total": 24,
    }),
    (TokenUsage(20, 4, 24, 5, None, cache_write_input_tokens=3,
                cache_write_5m_input_tokens=1, cache_write_1h_input_tokens=2), {
        "input": 12, "input_cached_tokens": 5, "input_cache_creation_5m_tokens": 1,
        "input_cache_creation_1h_tokens": 2, "output": 4, "total": 24,
    }),
    (TokenUsage(20, 0, 20, None, None, text_input_tokens=3, image_input_tokens=17), {
        "input": 0, "input_text_tokens": 3, "input_image_tokens": 17, "output": 0, "total": 20,
    }),
    (TokenUsage(20, 4, 24, 5, None, text_input_tokens=20, cache_write_input_tokens=3), {
        "input": 12, "input_cached_tokens": 5, "input_cache_creation_tokens": 3, "output": 4, "total": 24,
    }),
])
def test_price_buckets_are_exclusive_and_unknown_cache_modality_intersections_stay_metadata(usage, expected) -> None:
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(TracingConfig(enabled=True), exporter=exporter)
    with tracing.scope({}):
        with track_model_call(provider="provider", model="model", operation="generate") as tracker:
            tracker.usage = usage
    generation = exporter.get_finished_spans()[0]
    tracing.close()
    details = json.loads(generation.attributes["langfuse.observation.usage_details"])
    assert details == expected
    assert generation.attributes["langfuse.observation.metadata.usage_details_semantics"] == "exclusive_buckets"
    assert generation.attributes["langfuse.observation.metadata.usage_pricing_status"] == (
        "incomplete" if usage.text_input_tokens and usage.cached_input_tokens else "complete"
    )
    assert sum(value for key, value in details.items() if key != "total") == usage.total_tokens
    if usage.text_input_tokens is not None:
        assert json.loads(generation.attributes["langfuse.observation.metadata.usage_subsets"])["text_input_tokens"] == usage.text_input_tokens


def test_sdk_model_records_preserve_one_admission_actual_models_and_usd_without_charging_parent() -> None:
    from gateway.usage import control_model_calls

    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(TracingConfig(enabled=True), exporter=exporter)
    events, calls = [], []

    class Control:
        def acquire(self, tracker):
            events.append(("acquire", tracker.call_id))

        def release(self, tracker):
            events.append(("release", tracker.call_id))

    with control_model_calls(Control()), capture_model_usage(calls.append), tracing.scope({"job_id": "job"}):
        with track_model_call(provider="claude_code", model="requested", operation="agent", granularity="sdk_turn_run") as tracker:
            tracker.usage = TokenUsage(10, 2, 12, 0, None)
            tracker.usage_scope = "main_loop"
            tracker.sdk_model_usage = (
                SDKModelUsage("actual-sonnet", TokenUsage(10, 2, 12, 0, None), 0.01),
                SDKModelUsage("actual-haiku", TokenUsage(5, 1, 6, 1, None), 0.002),
            )
    model_one, model_two, run, root = exporter.get_finished_spans()
    tracing.close()
    assert len(calls) == 1
    assert events == [("acquire", calls[0].call_id), ("release", calls[0].call_id)]
    assert run.attributes["langfuse.observation.type"] == "span"
    assert "langfuse.observation.usage_details" not in run.attributes
    assert "langfuse.observation.cost_details" not in run.attributes
    assert run.parent.span_id == root.context.span_id
    for child, model, cost in ((model_one, "actual-sonnet", 0.01), (model_two, "actual-haiku", 0.002)):
        assert child.parent.span_id == run.context.span_id
        assert child.attributes["langfuse.observation.model.name"] == model
        assert child.attributes["langfuse.observation.metadata.call_id"] == calls[0].call_id
        assert child.attributes["langfuse.observation.metadata.usage_scope"] == "sdk_model"
        assert child.attributes["langfuse.observation.metadata.cost_source"] == "sdk_estimate"
        assert json.loads(child.attributes["langfuse.observation.cost_details"]) == {"total": cost}


def test_claude_cache_write_without_ttl_does_not_claim_complete_price_inputs() -> None:
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(TracingConfig(enabled=True), exporter=exporter)
    with tracing.scope({}):
        with track_model_call(provider="claude_code", model="actual", operation="agent") as tracker:
            tracker.usage = TokenUsage(20, 4, 24, 5, None, cache_write_input_tokens=3)
    generation = exporter.get_finished_spans()[0]
    tracing.close()
    assert generation.attributes["langfuse.observation.metadata.usage_pricing_status"] == "incomplete"
    assert json.loads(generation.attributes["langfuse.observation.usage_details"])["input_cache_creation_tokens"] == 3


def test_missing_sdk_model_values_remain_unknown_and_failed_run_keeps_partial_spend() -> None:
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(TracingConfig(enabled=True), exporter=exporter)
    with tracing.scope({}), pytest.raises(ValueError):
        with track_model_call(provider="claude_code", model="requested", operation="agent") as tracker:
            tracker.sdk_model_usage = (SDKModelUsage("actual", None, 0.0), SDKModelUsage("unknown", None))
            raise ValueError("invalid response")
    known_cost, unknown, run, _root = exporter.get_finished_spans()
    tracing.close()
    assert json.loads(known_cost.attributes["langfuse.observation.cost_details"]) == {"total": 0.0}
    assert "langfuse.observation.usage_details" not in known_cost.attributes
    assert "langfuse.observation.cost_details" not in unknown.attributes
    assert unknown.attributes["langfuse.observation.metadata.usage_status"] == "unknown"
    assert unknown.attributes["langfuse.observation.metadata.cost_source"] == "unknown"
    assert known_cost.attributes["langfuse.observation.level"] == "ERROR"
    assert run.attributes["langfuse.observation.type"] == "span"


def test_to_thread_scopes_keep_separate_execution_identities() -> None:
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(TracingConfig(enabled=True), exporter=exporter)

    async def execute(run_id):
        with tracing.scope({"run_id": run_id}):
            await asyncio.to_thread(call)

    def call():
        with track_model_call(provider="provider", model="model", operation="generate"):
            pass

    async def main():
        await asyncio.gather(execute("one"), execute("two"))

    asyncio.run(main())
    spans = exporter.get_finished_spans()
    tracing.close()
    generations = [span for span in spans if span.attributes["langfuse.observation.type"] == "generation"]
    assert {span.attributes["langfuse.observation.metadata.run_id"] for span in generations} == {"one", "two"}
    assert len({span.context.trace_id for span in generations}) == 2


@pytest.mark.parametrize("failure", ["ensure", "start", "end"])
def test_scope_failures_preserve_business_result_and_ledger(monkeypatch, failure) -> None:
    tracing = LangfuseTracing(TracingConfig(enabled=True))

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE-CREDENTIAL")

    span = SimpleNamespace(end=fail if failure == "end" else lambda: None)
    tracer = SimpleNamespace(start_span=fail if failure == "start" else lambda *args, **kwargs: span)
    monkeypatch.setattr(tracing, "_ensure_tracer", fail if failure == "ensure" else lambda: tracer)
    calls = []
    with capture_model_usage(calls.append), tracing.scope({}):
        with track_model_call(provider="provider", model="model", operation="generate"):
            result = "model-result"
    assert result == "model-result"
    assert len(calls) == 1
    tracing.close()


def test_completion_mapping_failure_still_ends_span_and_keeps_ledger(monkeypatch) -> None:
    tracing = LangfuseTracing(TracingConfig(enabled=True))
    ended, calls = [], []

    def fail(attributes):
        raise RuntimeError("PRIVATE-CREDENTIAL")

    span = SimpleNamespace(set_attributes=fail, end=lambda **kwargs: ended.append(kwargs))
    tracer = SimpleNamespace(start_span=lambda *args, **kwargs: span)
    monkeypatch.setattr(tracing, "_ensure_tracer", lambda: tracer)
    with capture_model_usage(calls.append), tracing.scope({}):
        with track_model_call(provider="provider", model="model", operation="generate"):
            result = "model-result"
    assert result == "model-result" and len(calls) == 1
    assert len(ended) == 2
    assert ended[0]["end_time"] == calls[0].ended_at * 1_000_000
    tracing.close()


def test_scope_business_failure_preserves_original_error_and_marks_root() -> None:
    exporter = InMemorySpanExporter()
    tracing = LangfuseTracing(TracingConfig(enabled=True), exporter=exporter)
    error = RuntimeError("PRIVATE-BODY")
    with pytest.raises(RuntimeError) as failure:
        with tracing.scope({}):
            with track_model_call(provider="provider", model="model", operation="generate"):
                pass
            raise error
    generation, root = exporter.get_finished_spans()
    tracing.close()
    assert failure.value is error
    assert generation.attributes["langfuse.observation.metadata.status"] == "completed"
    assert root.attributes["langfuse.observation.level"] == "ERROR"
    assert root.status.description == "execution_failed"
    assert "PRIVATE-BODY" not in str(root.attributes)

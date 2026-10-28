"""Run independent Video summaries without holding the parse completion path."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from agent.utils.tool import AgentTool, as_langchain_tools
from agent.utils.video_frames import build_inspect_video_frames_tool
from agent.video_summarize import (
    SummaryCompletion, VideoSummarizeAgent, VideoSummarizeInput,
    VideoSummarizeLimits, VideoSummaryResult,
)
from gateway.config import GatewayConfig, ModelBinding
from gateway.model_factory import GatewayClient
from gateway.multimodal import TextInput, adapter_capabilities
from gateway.usage import model_call_context

from ..config.video_summarize import VideoSummarizeConfig
from ..model.dto.jobDTO import VideoSummarizeLease
from ..storage.artifact import ArtifactStore
from ..utils.execution import ExecutionCalls, execution_calls, run_blocking
from ..utils.retry import task_retry_delay
from ..utils.time import now_utc
from .parse_usage import ParseUsageService
from .video_parse_errors import VideoParseLeaseLostError
from .video_parse_model_calls import VideoParseModelCallManager
from .video_parse_persistence import VideoParsePersistenceService


_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SummaryExecution:
    lease: VideoSummarizeLease
    config: VideoSummarizeConfig
    model: ModelBinding
    manager: VideoParseModelCallManager


class VideoSummarizeService:
    def __init__(
        self,
        persistence: VideoParsePersistenceService,
        artifact_store: ArtifactStore,
        gateway_client: GatewayClient,
        *,
        gateway_config: GatewayConfig,
        model: ModelBinding,
        config: VideoSummarizeConfig,
        worker_id: str,
        lease_duration: timedelta,
        heartbeat_interval: timedelta,
        parse_usage: ParseUsageService | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        if not timedelta(0) < heartbeat_interval < lease_duration:
            raise ValueError("heartbeat_interval must be positive and below lease_duration")
        if not isinstance(model, ModelBinding) or model.kind not in {None, "generative"}:
            raise ValueError("summarize requires a generative model binding")
        model = model.model_copy(update={"kind": "generative"})
        for binding in (model, *([model.fallback] if model.fallback is not None else [])):
            if (binding.supports_vision_input is not True
                    or adapter_capabilities(gateway_config.source(binding.source).adapter).visual_input_mode is None):
                raise ValueError("summarize requires a vision-capable model and fallback")
        self._persistence = persistence
        self._artifact_store = artifact_store
        self._gateway = gateway_client
        self._config = config
        self._snapshot = {"model": model.model_dump(mode="json", exclude_none=True), "limits": asdict(config)}
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._parse_usage = parse_usage
        self._execution: ContextVar[_SummaryExecution | None] = ContextVar("video_summary_execution", default=None)
        self.agent = VideoSummarizeAgent(runtime=self)

    async def run_next(self) -> bool:
        if not self._config.enabled:
            return False
        lease = await asyncio.to_thread(
            self._persistence.claim_next_summary, self._worker_id,
            lease_duration=self._lease_duration, config_snapshot=self._snapshot,
        )
        if lease is None:
            return False
        await self._execute(lease)
        return True

    async def _execute(self, lease: VideoSummarizeLease) -> None:
        limits = dict(lease.config_snapshot["limits"])
        # Retried tasks may retain the retired call caps in their frozen snapshot.
        for name in ("max_model_calls", "max_tool_calls", "maximum_frame_inspections"):
            limits.pop(name, None)
        config = VideoSummarizeConfig(**limits)
        model = ModelBinding.model_validate(lease.config_snapshot["model"])

        def check_active() -> None:
            if not self._persistence.heartbeat_summary(lease, lease_duration=self._lease_duration):
                raise VideoParseLeaseLostError("Video summary lease is no longer current")

        manager = VideoParseModelCallManager(
            self._persistence, execution_lease=lease,
            lease_duration=self._lease_duration, check_active=check_active,
        )
        binding = _SummaryExecution(lease, config, model, manager)
        binding_token = self._execution.set(binding)
        calls = ExecutionCalls()
        calls_token = execution_calls.set(calls)
        stopped = asyncio.Event()

        async def heartbeat() -> None:
            try:
                while True:
                    try:
                        await asyncio.wait_for(stopped.wait(), self._heartbeat_interval.total_seconds())
                        return
                    except TimeoutError:
                        pass
                    if not await asyncio.to_thread(manager.heartbeat):
                        raise VideoParseLeaseLostError("Video summary model reservation expired")
            except Exception:
                manager.cancel()

        pulse = asyncio.create_task(heartbeat())
        usage = (
            self._parse_usage.capture_video(lease, lease.task_input, sequence_no=None, category="parse")
            if self._parse_usage is not None else nullcontext()
        )
        deadline = asyncio.timeout(config.execution_timeout_seconds)
        try:
            with usage, model_call_context({
                "stage": "summarize", "execution_kind": "summarize",
                "execution_task_id": lease.video_parse_task_id, "retry_count": lease.retry_count,
            }), manager.capture():
                async with deadline:
                    inputs = await asyncio.to_thread(self._persistence.load_summary_input, lease)
                    source = await run_blocking(self._artifact_store.resolve, lease.task_input.original_source_uri)
                    frames = build_inspect_video_frames_tool(
                        source_path=source, read_bounds=(0, inputs.duration_ms),
                        max_height=config.frame_max_height, timeout_seconds=config.frame_timeout_seconds,
                        check_control=manager.check_active,
                        trace_context={"video_parse_task_id": lease.video_parse_task_id,
                                       "video_id": lease.video_id, "source_fingerprint": lease.source_fingerprint},
                    )
                    result = await self.agent.InvokeSummarize(
                        inputs, inspect_video_frames=frames,
                        limits=VideoSummarizeLimits(
                            maximum_result_bytes=config.maximum_result_bytes,
                            maximum_read_chars=config.maximum_read_chars,
                            maximum_catalog_items=config.maximum_catalog_items,
                        ), check_control=manager.check_active,
                    )
                    manager.check_active()
                    cover_uri = await run_blocking(self._publish_cover, result, binding)
                    # The transaction checks the lease and source versions again.
                    await asyncio.to_thread(self._persistence.complete_summary, lease, result, cover_uri=cover_uri)
        except asyncio.CancelledError:
            manager.cancel()
            if deadline.expired():
                # BlockingCallCancelled is a CancelledError subclass that Python
                # 3.12's timeout context does not convert into TimeoutError.
                await self._retry_after_exit(binding, calls, "TimeoutError")
                return
            release = lambda: self._persistence.release_summary(lease)
            if not calls.defer_cleanup(release):
                await asyncio.to_thread(release)
            raise
        except Exception as error:
            manager.cancel()
            await self._retry_after_exit(binding, calls, type(error).__name__)
        finally:
            manager.cancel()
            stopped.set()
            await asyncio.gather(pulse, return_exceptions=True)
            execution_calls.reset(calls_token)
            self._execution.reset(binding_token)

    async def _retry_after_exit(self, execution: _SummaryExecution, calls: ExecutionCalls, error_type: str) -> None:
        lease, config = execution.lease, execution.config
        retry_at = now_utc() + task_retry_delay(
            config.retry_initial_delay_seconds, config.retry_max_delay_seconds, lease.retry_count + 1,
        )
        retry = lambda: self._persistence.retry_summary(
            lease, error=error_type, retry_at=retry_at, max_retries=config.max_retries,
        )
        if not calls.defer_cleanup(retry):
            await asyncio.to_thread(retry)
        _logger.warning("Video summary failed task=%s lease=%s error_type=%s",
                        lease.video_parse_task_id, lease.lease_version, error_type)

    async def generate_video_summary(
        self, inputs: VideoSummarizeInput, *, prompt: str, tools: list[AgentTool],
    ) -> object:
        execution = self._execution.get()
        if execution is None or inputs.video_id != execution.lease.video_id:
            raise VideoParseLeaseLostError("Video summary runtime is not bound to this source")

        def generate() -> SummaryCompletion:
            execution.manager.check_active()
            model = self._gateway.create_generative_model(execution.model, mcp=False)
            try:
                return model.generate(
                    [TextInput(prompt)], schema_name="VideoSummaryCompletion", result_type=SummaryCompletion,
                    tools=as_langchain_tools(tools),
                    request_timeout_seconds=execution.config.execution_timeout_seconds,
                    check_control=execution.manager.check_active,
                )
            finally:
                model.close()

        return await run_blocking(generate)

    def _publish_cover(self, result: VideoSummaryResult, execution: _SummaryExecution) -> str | None:
        if result.cover_image is None:
            return None
        execution.manager.check_active()
        header, payload = result.cover_image.data_url.split(",", 1)
        extensions = {"data:image/jpeg;base64": ".jpg", "data:image/png;base64": ".png", "data:image/webp;base64": ".webp"}
        if header not in extensions:
            raise ValueError("summary cover must be an inspected image")
        data = base64.b64decode(payload, validate=True)
        if len(data) != result.cover_image.size_bytes:
            raise ValueError("summary cover bytes do not match the inspection receipt")
        filename = "summary-" + hashlib.sha256(data).hexdigest() + extensions[header]
        with TemporaryDirectory(prefix="seekino-summary-cover-") as directory:
            path = Path(directory) / filename
            path.write_bytes(data)
            execution.manager.check_active()
            return self._artifact_store.publish_video_artifact(
                path, series_id=execution.lease.task_input.series_id,
                video_id=execution.lease.video_id, category="keyframes", filename=filename,
            ).uri

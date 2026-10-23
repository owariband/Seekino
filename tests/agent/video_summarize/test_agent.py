from __future__ import annotations

import asyncio

import pytest

from agent.video_summarize import SummaryCompletion, VideoSummarizeAgent
from tests.agent.video_summarize.test_tools import _frame_tool, _inputs, _limits, _read_all, _summary


def test_completion_response_without_submit_is_not_success():
    class Runtime:
        async def generate_video_summary(self, inputs, *, prompt, tools):
            assert "EVERY chunk" in prompt
            assert len(tools) == 3
            return SummaryCompletion(completed=True)

    with pytest.raises(RuntimeError, match="without a successful submit"):
        asyncio.run(VideoSummarizeAgent(runtime=Runtime()).InvokeSummarize(
            _inputs(), inspect_video_frames=_frame_tool(), limits=_limits(), check_control=lambda: None,
        ))


def test_only_validated_submission_is_returned_and_calls_have_separate_state():
    class Runtime:
        async def generate_video_summary(self, inputs, *, prompt, tools):
            tools = {tool.name: tool for tool in tools}
            assert _read_all(tools, inputs).success
            assert tools["submit_video_summary"].execute(**_summary()).success
            return {"title": "Ignore untrusted final model output"}

    agent = VideoSummarizeAgent(runtime=Runtime())

    async def run():
        return await asyncio.gather(*[agent.InvokeSummarize(
            _inputs(), inspect_video_frames=_frame_tool(), limits=_limits(), check_control=lambda: None,
        ) for _ in range(2)])

    first, second = asyncio.run(run())
    assert first.title == second.title == "出発"
    assert first is not second and first.submission is not second.submission


def test_host_cancellation_after_submission_prevents_returning_a_result():
    cancelled = False

    class Runtime:
        async def generate_video_summary(self, inputs, *, prompt, tools):
            nonlocal cancelled
            tools = {tool.name: tool for tool in tools}
            _read_all(tools, inputs)
            assert tools["submit_video_summary"].execute(**_summary()).success
            cancelled = True

    def check_control():
        if cancelled:
            raise TimeoutError("deadline expired")

    with pytest.raises(TimeoutError, match="deadline expired"):
        asyncio.run(VideoSummarizeAgent(runtime=Runtime()).InvokeSummarize(
            _inputs(), inspect_video_frames=_frame_tool(), limits=_limits(), check_control=check_control,
        ))

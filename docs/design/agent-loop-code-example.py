"""Run the production Harness offline with fake models and nonempty evidence.

From the repository root:
    PYTHONPATH=. .venv/bin/python docs/design/agent-loop-code-example.py

This demonstrates two business Turns, advice, evidence reuse and incremental
callbacks. It makes no Provider, SQL or HTTP requests and does not play a video.
The formal regression tests are in tests/server/test_agent_runtime.py.
"""

from __future__ import annotations

import json
from datetime import datetime

from server.agent import AgentRuntime
from server.agent.state import RunInputs
from server.schema import (
    AgentReply, SelectionReason, VideoChunkCandidate, VideoChunkEvidence,
    VideoChunkSearchHit, VideoChunkSearchResult,
)

SERIES_ID = "11111111-1111-4111-8111-111111111111"
VIDEO_ID = "22222222-2222-4222-8222-222222222222"
CHUNK_ID = "33333333-3333-4333-8333-333333333333"


def search(query, *, workspace_id, series_ids, top_k):
    evidence = VideoChunkEvidence(
        series_id=SERIES_ID, workspace_id=workspace_id, series_name="离线示例",
        series_description=None, series_status="READY", important_extra_data=None,
        character_bible=[], video_id=VIDEO_ID, episode_order=1,
        original_filename="demo.mp4", original_source_uri="artifact:demo.mp4",
        inference_proxy_uri=None, content_hash="offline-demo", duration_ms=9000,
        upload_status="COMPLETED", parse_status="COMPLETED", video_chunk_id=CHUNK_ID,
        sequence_no=0, start_ms=1000, end_ms=2000, analysis_window_start_ms=0,
        analysis_window_end_ms=3000, chunk_media_ref="artifact:chunk.mp4",
        analysis_window_media_ref="artifact:window.mp4", transcript=[],
        baseline_visual_caption="人物将杯子摔在地上。", keyframe_uris=[],
        all_time_events=[], all_time_event_text="人物将杯子摔在地上。",
        retrieval_text="人物将杯子摔在地上。", video_compact="杯子落地破碎。",
        context_before_compact="人物拿着杯子。", context_after_compact="人物转身离开。",
        window_summary="杯子落地破碎。", model_provider="fake", model_name="offline-parser",
        pipeline_version="offline-v1", is_fallback=False, parse_provenance=None,
        parsed_at=datetime(2026, 9, 15),
    )
    hit = VideoChunkSearchHit(
        candidate=VideoChunkCandidate(video_chunk_id=CHUNK_ID, fused_rank=1, fused_score=1),
        selection_reason=SelectionReason(
            summary="画面描述包含摔杯子动作。", matched_evidence=[], limitations=[],
        ),
        evidence=evidence,
    )
    return VideoChunkSearchResult(
        query=query, series_ids=series_ids,
        agent_reply=AgentReply(message="检索完成。", result_count=1), hits=[hit],
    )


class FakeQuery:
    provider_name = "fake"
    model_name = "offline-query"

    def run_query(self, prompt, *, tools, result_type, max_model_calls, check_control=None):
        if check_control is not None:
            check_control()
        context = json.loads(prompt.rsplit("\n\n", 1)[1])
        number = context["turn_number"]
        if number == 1:
            tools[0].invoke({"query": "摔杯子，杯子落地破碎", "top_k": 1})
        else:
            assert context["original_plan"] == "检索摔杯子的画面证据。"
            previous = context["turns"][-1]
            assert previous["review"]["advice"]
            assert previous["retrieval_calls"][0]["result"]["hits"]
            # Reuse the recorded evidence; no second retrieval is needed.
        return result_type.model_validate({
            "plan_context": "检索摔杯子的画面证据。" if number == 1 else "复用证据，补充动作与范围。",
            "message": "找到一个片段。" if number == 1 else "第 1 集 1–2 秒，人物将杯子摔在地上。",
            "selected_video_chunk_ids": [CHUNK_ID],
        })


class FakeEvaluator:
    provider_name = "fake"
    model_name = "offline-evaluator"

    def generate(self, inputs, *, schema_name, result_type):
        context = json.loads(inputs[0].text.rsplit("\n\n", 1)[1])
        first = not context["turns"]
        return result_type.model_validate({
            "action": "continue" if first else "complete",
            "advice": "请明确摔杯子的动作和已有片段范围。" if first else "草稿与证据一致。",
        })


class FakeChat:
    provider_name = "fake"
    model_name = "offline-chat"

    def stream_text(self, inputs):
        yield "找到一个摔杯子的片段：第 1 集 1–2 秒。"
        yield "人物将杯子摔在地上，可以打开视频卡片核验。"


if __name__ == "__main__":
    runtime = AgentRuntime(FakeQuery(), FakeEvaluator(), FakeChat(), search)
    inputs = RunInputs(
        conversation_id="offline-conversation", assistant_message_id="offline-answer",
        workspace_id="offline-workspace", series_ids=[SERIES_ID], series_context=[],
        user_message="帮我找到摔杯子的画面。", history=[], requested_top_k=1,
    )
    result = runtime.kick(
        inputs, emit=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True),
    )
    print(json.dumps({"final_output": result.model_dump(mode="json", by_alias=True)}, ensure_ascii=False))

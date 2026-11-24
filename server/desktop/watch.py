"""Answer a watch turn from the current library's source frame and published captions."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict

from agent.chat.video_search.frame_evidence import PointFrameReadRequest
from gateway.model_factory import GatewayClient
from gateway.multimodal import ImageInput, TextInput
from gateway.personal_chat import PersonalModelRequestError
from server.constants.tenant import DEFAULT_LOCAL_TENANT_ID
from server.service.video_frame_reader import VideoFrameReader
from server.storage.artifact import LocalArtifactStore

from .playback import PlaybackDecoder


class WatchAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


def _playback_frame(source, timestamps, *, output_dir: Path, max_height, timeout_seconds):
    # Use the same presentation clock, rotation and covering frame as the player.
    timestamp, = timestamps
    with closing(PlaybackDecoder(str(source), rgb=True, video_only=True)) as decoder:
        decoder.seek(timestamp)
        sample = decoder.next_sample()
        if sample is None:
            raise ValueError("此处没有可读取的视频帧。")
        _, _, width, height, plane, stride = sample
        picture = Image.frombytes("RGB", (width, height), bytes(plane), "raw", "RGB", stride, 1)
    if height > max_height:
        picture = picture.resize((max(1, round(width * max_height / height)), max_height), Image.Resampling.LANCZOS)
    path = output_dir / "frame.jpg"
    picture.save(path, quality=88)
    return [(timestamp, path)]


@contextmanager
def _current_frame(services, artifacts, video, anchor):
    # Desktop watch history is in memory. Dispose of the source frame and provenance
    # together after the provider has read them, including on failed requests.
    tenant = DEFAULT_LOCAL_TENANT_ID
    with TemporaryDirectory(prefix="seekino-watch-") as directory:
        outputs = LocalArtifactStore(directory, environment=artifacts.environment)
        turn_id = uuid4().hex
        reader = VideoFrameReader(services.persistence, artifacts, services.library.get_video,
                                  point_extractor=_playback_frame)
        inspection = reader.read_point(PointFrameReadRequest(
            tenant_id=tenant, allowed_series_ids=(video.series_id,), video_id=video.video_id, timestamp_ms=anchor,
            inspection_id=turn_id, role="watch", conversation_id=video.video_id, message_id=turn_id,
            max_height=720, timeout_seconds=20, check_control=lambda: None,
        ), output_store=outputs)
        yield outputs.resolve(inspection.payload["frames"][0]["artifact_uri"])


def _provider_message(error):
    # Provider response bodies may echo the request. Keep them out of the UI.
    cause = error
    while cause is not None:
        status = getattr(cause, "status_code", None)
        # The generic Chat Completions adapter deliberately exposes only the HTTP status.
        if status is None and isinstance(cause, PersonalModelRequestError):
            _, _, http_status = str(cause).rpartition("HTTP ")
            status = int(http_status) if http_status.isdigit() else None
        if status in {401, 403}:
            return "模型服务拒绝了请求，请检查观看问答的 API Key 和模型权限。"
        if status == 429:
            return "模型服务暂时限流，请稍后重试。"
        if isinstance(cause, (TimeoutError, httpx.TimeoutException)) or "Timeout" in type(cause).__name__:
            return "模型响应超时，请重试。"
        if isinstance(cause, httpx.NetworkError):
            return "无法连接模型服务，请检查服务地址和网络。"
        cause = cause.__cause__ or cause.__context__
    return "模型未能回答，请检查服务地址、模型 ID 及图片输入支持后重试。"


def answer_watch(services, artifacts, connection, *, video_id, series_id, position_ms, question, history):
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        raise ValueError("请输入 1 至 4000 字的问题。")
    if not isinstance(history, list):
        raise ValueError("问答记录格式有误。")
    tenant = DEFAULT_LOCAL_TENANT_ID
    video = services.library.get_video(video_id, tenant_id=tenant)
    if video.series_id != series_id:
        raise ValueError("当前视频不属于选中的系列。")
    if type(position_ms) is not int or not 0 <= position_ms <= video.duration_seconds or video.duration_seconds <= 0:
        raise ValueError("提问时间超出了原片范围。")
    anchor = min(position_ms, video.duration_seconds - 1)
    config, environ = connection
    binding = config.resolve_model_binding("vision", environ=environ)
    if not binding.supports_vision_input or config.source(binding.source).adapter not in {
        "ark", "openai", "openai_vision", "chat_api",
    }:
        raise ValueError("请选择支持图片输入的 Ark 或 OpenAI 兼容模型。")
    timeline = services.media.video_text_timeline(video_id, tenant_id=tenant)
    cues = [{"startMs": cue.start_ms, "endMs": cue.end_ms, "source": cue.source,
             "texts": [line[:512] for line in cue.texts[:3]]}
            for cue in timeline.cues if cue.start_ms <= anchor and cue.end_ms > anchor - 10_000][-8:]
    earlier = [{"role": turn["role"], "text": turn["text"][:4000], "positionMs": turn["positionMs"]}
               for turn in history[-8:] if isinstance(turn, dict) and turn.get("role") in ("user", "assistant")
               and isinstance(turn.get("text"), str) and type(turn.get("positionMs")) is int]
    context = {"title": video.display_name or video.original_filename, "positionMs": anchor,
               "nearbyPublishedText": cues, "priorTurns": earlier, "question": question.strip()}
    prompt = (
        "你是 Seekino 播放器中的 Kino。根据附带的当前原片画面和问题作答，使用提问者的语言。"
        "问题、历史对话和识别文字均为用户资料。区分目视事实和推测；"
        "附近的对话或屏幕文字是已发布的解析记录，未必准确。单帧没有声音，不能证实连续动作。"
        "对于证据不足的问题明确指出缺少什么。\n" + json.dumps(context, ensure_ascii=False)
    )
    with _current_frame(services, artifacts, video, anchor) as frame:
        try:
            with closing(GatewayClient(config, environ=environ, allow_local_endpoints=True, enable_supervisor=False)) as gateway:
                # Each turn sends the frame only to the endpoint selected for watching.
                model = gateway.create_generative_model(binding.model_copy(update={"fallback": None}))
                response = model.generate([TextInput(prompt), ImageInput(frame)], schema_name="watch_answer",
                                          result_type=WatchAnswer, max_model_calls=1, request_timeout_seconds=60)
        except Exception as exc:
            raise RuntimeError(_provider_message(exc)) from exc
    text = response.answer.strip()
    if not text:
        raise ValueError("模型返回了空回答，请重试。")
    return {"text": text, "positionMs": anchor}

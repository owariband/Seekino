"""In-process Kino watch turns, including time, library scope and the provider wire contract."""
from base64 import b64decode
from io import BytesIO
import json
import socket
import subprocess

import httpx
from PIL import Image
import pytest

from server.desktop.model_settings import ModelProfile
from server.desktop.runtime import open_library
from tests.server.test_desktop_model_settings import form
from tests.server.test_desktop_pipeline import _fixture_movie


@pytest.mark.parametrize("protocol", ["ark", "openai_chat"])
def test_watch_turn_reads_unparsed_original_at_playhead(tmp_path, monkeypatch, protocol):
    source = tmp_path / "scene.mp4"
    _fixture_movie(source)
    root = tmp_path / "library"
    profile = ModelProfile.read(root).candidate(form(protocol))
    [slot] = profile.view({})["credentialNames"]
    config, keys = profile.for_gateway({slot: "fixture-token"})
    captured = []

    def reply(_transport, request):
        body = json.loads(request.content)
        captured.append((request.url.path, body))
        content = json.dumps({"answer": "当前画面偏红。"}, ensure_ascii=False)
        if protocol == "ark":
            return httpx.Response(200, json={"id": "recorded", "status": "completed", "model": body["model"],
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": content}]}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    def unexpected(*_args, **_kwargs):
        raise AssertionError("watching must not spawn a process or open a real connection")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reply)
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    monkeypatch.setattr(subprocess, "Popen", unexpected)
    with open_library(root, gateway_config=config, gateway_environ=keys, start_consumer=False) as library:
        sid = library.create_series("样片").series_id
        video_id = library.import_file(source, series_id=sid, episode_order=1, idempotency_key="watch-one").video_id
        turn = dict(video_id=video_id, series_id=sid, position_ms=1250, question="现在什么颜色？",
                    history=[{"role": "user", "text": "上一帧呢", "positionMs": 0}])
        for changes, error in (({"series_id": "wrong"}, "系列"), ({"position_ms": 3000}, "时间")):
            with pytest.raises(ValueError, match=error):
                library.ask_watch((config, keys), **(turn | changes))
        assert not captured
        answer = library.ask_watch((config, keys), **turn)
        assert answer == {"text": "当前画面偏红。", "positionMs": 1250}
        [(path, body)] = captured
        assert body["model"] == "test-vision"
        if protocol == "ark":
            assert path.endswith("/responses")
            assert body["text"]["format"]["name"] == "watch_answer"
            assert body["text"]["format"]["schema"]["additionalProperties"] is False
            parts = [part for item in body["input"] for part in item["content"]]
            data = next(p["image_url"] for p in parts if p["type"] == "input_image")
        else:
            assert path.endswith("/chat/completions")
            parts = body["messages"][-1]["content"]
            data = next(p["image_url"]["url"] for p in parts if p["type"] == "image_url")
        prompt = next(p["text"] for p in parts if "priorTurns" in p.get("text", ""))
        assert '"positionMs": 1250' in prompt and "上一帧呢" in prompt and '"nearbyPublishedText": []' in prompt
        # The 1.2 s frame covers 1.25 s. The next frame at 1.3 s is brighter.
        image = Image.open(BytesIO(b64decode(data.split(",", 1)[1])))
        r, g, b = image.getpixel((20, 20))
        assert abs(r - 96) < 5 and abs(g - 10) < 5 and abs(b - 40) < 5
        assert not list(root.rglob("point-*.jpg")) and not list(root.rglob("point-*.json"))

        def denied(_transport, request):
            return httpx.Response(401, json={"error": {"message": "sensitive-provider-body"}})

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
        with pytest.raises(RuntimeError, match="API Key") as denied_error:
            library.ask_watch((config, keys), **turn)
        assert "sensitive-provider-body" not in str(denied_error.value)
        assert not list(root.rglob("point-*.jpg")) and not list(root.rglob("point-*.json"))


def test_packaged_watch_model_loads_without_cloud_agent():
    # A clean interpreter is essential: the cloud test suite also imports the full Supervisor.
    import sys
    probe = """
import sys
sys.modules['deepagents'] = None
from gateway.registered_multimodal import RegisteredMultimodalModel
from server.desktop.watch import WatchAnswer
assert 'gateway.agent_model.deepagents' not in sys.modules
assert 'gateway.personal_models' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", probe], check=True, timeout=30)


def test_persist_images_turns_and_recover_interrupted_answer(tmp_path, monkeypatch):
    from server.desktop.app import Application
    from server.desktop.watch_settings import save_watch

    source = tmp_path / "scene.mp4"
    _fixture_movie(source)
    root = tmp_path / "library"
    root.mkdir()
    save_watch(root, {"protocol": "openai_chat", "auth": "none", "model": "vision",
                      "baseUrl": "http://localhost:7878/v1"}, {})
    image = tmp_path / "reference.png"
    Image.new("RGB", (25, 20), "blue").save(image)
    received = []

    def reply(_transport, request):
        parts = json.loads(request.content)["messages"][-1]["content"]
        received.append(parts)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                              "content": '{"answer":"红色画面与蓝色附件。"}'}}]})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reply)
    app = Application(root, {})
    try:
        series = app.library.create_series("片库").series_id
        video = app.library.import_file(source, series_id=series, episode_order=1, idempotency_key="first").video_id
        alternate = source.with_name("alternate.mp4")
        alternate.write_bytes(source.read_bytes() + bytes.fromhex("0000000866726565"))  # Empty MP4 free atom.
        second = app.library.import_file(alternate, series_id=series, episode_order=2, idempotency_key="second").video_id
        turn = dict(videoID=video, seriesID=series, positionMs=300, question="比较两张画面", threadID="",
                    attachmentPath=str(image))
        initial = app.call("askWatch", turn)["watch"]
        assert len(initial["messages"]) == 2 and initial["messages"][0]["attachments"][0]["previewUrl"].startswith("file:")
        assert sum(part["type"] == "image_url" for part in received[0]) == 2
        thread = initial["threadID"]
        result = app.call("askWatch", turn | {"question": "上一轮说了什么？", "attachmentPath": "", "threadID": thread})
        assert len(result["watch"]["messages"]) == 4 and "蓝色附件" in received[1][0]["text"]
        with pytest.raises(ValueError, match="不属于当前视频"):
            app.call("watchHistory", {"videoID": second, "threadID": thread})
        app.library.watch_history.start(video, [series], thread, "未完成", 600, "", "", "vision")
    finally:
        app.call("close", {})

    app = Application(root, {})
    try:
        recovered = app.call("watchHistory", {"videoID": video})
        assert recovered["threadID"] == thread
        assert recovered["messages"][:4] == result["watch"]["messages"]
        assert recovered["messages"][-1]["failed"] and "中断" in recovered["messages"][-1]["text"]
        assert app.call("watchHistory", {"videoID": video, "threadID": thread, "delete": True})["messages"] == []
        assert not app.library.watch_history.artifacts.list_attachment_conversations()
    finally:
        app.call("close", {})


def test_stop_cancels_active_provider_and_rejects_late_result():
    from threading import Event, Thread

    from desktop.watch_control import WatchCallControl
    from gateway.usage import ModelCallCancelledError, control_model_calls, track_model_call

    control = WatchCallControl()
    entered, stopped = Event(), Event()
    result = []

    def provider():
        with control_model_calls(control):
            try:
                with track_model_call(provider="fixture", model="test", operation="watch_answer") as call:
                    call.add_cancel_callback(stopped.set)
                    entered.set()
                    assert stopped.wait(3)
            except ModelCallCancelledError:
                result.append("stopped")

    worker = Thread(target=provider)
    worker.start()
    assert entered.wait(3)
    control.cancel()
    worker.join(3)
    assert result == ["stopped"] and not worker.is_alive()

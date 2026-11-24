"""Both native Kino protocols publish visible text before the HTTP turn finishes."""
from threading import Event
from time import monotonic, sleep
import json

import httpx
import pytest
from PySide6.QtCore import QCoreApplication

from desktop.qt_controller import Workspace
from gateway.constants import ARK_OPENAI_BASE_URL
from server.desktop.runtime import open_library
from server.desktop.watch_settings import save_watch, watch_credentials
from tests.desktop.test_workspace import MemoryCredentials
from tests.server.test_desktop_pipeline import _fixture_movie


@pytest.mark.parametrize("protocol", ["ark", "openai_chat"])
@pytest.mark.parametrize("stop", [False, True])
def test_live_answer_prefix_and_stopped_history(tmp_path, monkeypatch, protocol, stop):
    app = QCoreApplication.instance() or QCoreApplication([])
    release = Event()
    first, last = "第一段", "第二段"
    root = tmp_path / "library"
    source = tmp_path / "real.mp4"
    _fixture_movie(source)
    with open_library(root, start_consumer=False) as lib:
        series = lib.create_series("样片")
        original = lib.import_file(source, series_id=series.series_id, episode_order=1, idempotency_key="stream")
    selection = {"protocol": protocol, "baseUrl": ARK_OPENAI_BASE_URL if protocol == "ark" else "http://localhost:8999/v1",
                 "auth": "key" if protocol == "ark" else "none", "model": "test-stream"}
    keys = MemoryCredentials({slot: "test-token" for slot in watch_credentials(selection)["credentialNames"]})
    save_watch(root, selection, keys)
    monkeypatch.setattr("server.desktop.app.enforce_single_process", lambda: None)

    def event(value):
        return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()

    class Wire(httpx.SyncByteStream):
        def __iter__(self):
            if protocol == "ark":
                def packet(idx, text):
                    return event(dict(type="response.output_text.delta", sequence_number=idx, output_index=0,
                                      content_index=0, item_id="msg", delta=text))
                yield packet(0, first)
                assert release.wait(8)
                yield packet(1, last)
                response = {"id": "resp", "model": "test-stream", "status": "completed",
                            "output": [{"type": "message", "id": "msg", "role": "assistant", "status": "completed",
                                        "content": [{"type": "output_text", "text": first + last}]}]}
                yield event(dict(type="response.completed", sequence_number=2, response=response))
            else:
                def packet(text, finish=None):
                    return event(dict(id="res", model="test-stream", object="chat.completion.chunk", created=0,
                                      choices=[dict(index=0, delta={"content": text}, finish_reason=finish)]))
                yield packet(first)
                assert release.wait(8)
                yield packet(last)
                yield packet("", "stop")
                yield b"data: [DONE]\n\n"

        def close(self):
            release.set()

    def send(transport, request):
        payload = json.loads(request.content)
        assert payload["stream"] is True and payload["model"] == selection["model"]
        if protocol == "ark":
            assert request.url.path.endswith("/responses")
            assert any(row["type"] == "input_image" for row in payload["input"][0]["content"])
        else:
            assert request.url.path.endswith("/chat/completions") and "authorization" not in request.headers
            assert any(row["type"] == "image_url" for row in payload["messages"][0]["content"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Wire())

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    workspace = Workspace(root, credentials=keys)

    def wait(predicate):
        deadline = monotonic() + 7
        while not predicate():
            app.processEvents()
            assert not workspace.error, workspace.error
            assert monotonic() < deadline, (workspace.watchBusy, workspace.watchMessages, workspace.watchError)
            sleep(.004)

    phases, outcomes = [], []
    workspace.watchPhaseChanged.connect(lambda: phases.append(workspace.watchPhase))
    workspace.watchTurnFinished.connect(outcomes.append)
    try:
        workspace.start()
        wait(lambda: workspace.ready)
        workspace.openVideo(original.video_id, "真实原片")
        wait(lambda: workspace.playbackContext.get("videoID") == original.video_id)
        assert workspace.askWatch("描述画面", 200)
        wait(lambda: len(workspace.watchMessages) == 2 and workspace.watchMessages[-1]["text"] == first)
        assert workspace.watchBusy and not release.is_set()
        assert phases == ["QUEUED", "RETRIEVING", "EVALUATING", "RESPONDING"]
        if stop:
            workspace.stopWatch()
        else:
            release.set()
        wait(lambda: not workspace.watchBusy)
        last_message = workspace.watchMessages[-1]
        assert last_message["text"] == (first if stop else first + last)
        assert last_message["failed"] is stop
        assert bool(workspace.watchError) is stop
        assert phases[-1] == "" and outcomes == [stop]
    finally:
        release.set()
        workspace.shutdown()
    with open_library(root, start_consumer=False) as lib:
        assert lib.watch_history.view(original.video_id)["messages"][-1] == last_message

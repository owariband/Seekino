from array import array
from fractions import Fraction
import json
import re
import socket
import subprocess
import sys
import wave
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from gateway.ark.multimodal import ArkMultimodalModel
from gateway.model_factory import GenerativeModel
from gateway.multimodal import TextInput

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="desktop media uses VideoToolbox")


def _fixture_movie(path):
    import av
    from PIL import Image

    with av.open(str(path), "w") as movie:
        video = movie.add_stream("mpeg4", rate=10)
        video.width, video.height, video.pix_fmt = 128, 96, "yuv420p"
        audio = movie.add_stream("aac", rate=24000)
        audio.layout = "mono"
        for number in range(20):
            frame = av.VideoFrame.from_image(Image.new("RGB", (128, 96), (number * 8, 10, 40)))
            frame.pts, frame.time_base = number, Fraction(1, 10)
            movie.mux(video.encode(frame))
            samples = av.AudioFrame(format="s16", layout="mono", samples=2400)
            # Two audible bursts, 400–600 ms and 900–1100 ms, locate both audio boundaries.
            values = array("h", ((8000 if n % 48 < 24 else -8000) if number in (4, 5, 9, 10) else 0
                                 for n in range(2400)))
            samples.planes[0].update(values.tobytes())
            samples.sample_rate = 24000
            samples.pts, samples.time_base = number * 2400, Fraction(1, 24000)
            movie.mux(audio.encode(samples))
        movie.mux(video.encode(None))
        movie.mux(audio.encode(None))


def _wait_for_completion(library, task_id, calls):
    deadline = monotonic() + 45
    while True:
        task = library.task(task_id).task
        if task.status == "COMPLETE":
            return
        assert not task.last_error and monotonic() < deadline, (task.status, task.last_error, calls)
        sleep(0.2)


def _fixed_model_responses(monkeypatch, *, on_chunk=None):
    from arkruntime.resources.multimodal_embeddings.multimodal_embeddings import MultimodalEmbeddings

    calls = []
    description = "一只手举起红旗"

    def generate(_model, inputs, *, schema_name, result_type, **_options):
        calls.append(schema_name)
        if schema_name == "candidate_transcript":
            data = {"transcript": []}
        elif schema_name == "video_content_type":
            data = {"content_kind": "script"}
        elif schema_name == "video_parse_v2_chunk":
            start, end = 0, 2000
            if on_chunk is not None:
                prompt = "\n".join(item.text for item in inputs if isinstance(item, TextInput))
                match = re.search(r"(?:authorized half-open source-video range|Visual events must stay inside) "
                                  r"\[(\d+), (\d+)\)", prompt)
                assert match, "missing authorized source-video range"
                start, end = map(int, match.groups())
                on_chunk()
            data = {"persons": [], "timeline": [{
                "event_type": "ACTION", "start_seconds": start + 300, "end_seconds": min(end, start + 1200),
                "description": description, "participants": [], "visible_persons": [],
            }]}
        elif schema_name == "IntervalRefinementResult":
            prompt = next(item.text for item in inputs if isinstance(item, TextInput))
            members = json.loads(prompt.split("Input: ", 1)[1])["members"]
            data = {"members": [{"sequence_no": item["sequence_no"], **item["content"]} for item in members]}
        else:
            raise AssertionError(f"unexpected model schema {schema_name}")
        return result_type.model_validate(data)

    def embedding(_client, *, model, input, dimensions, **_options):
        calls.append(input[0]["type"])
        data = SimpleNamespace(embedding=[1.0] + [0.0] * (dimensions - 1),
                               sparse_embedding=[SimpleNamespace(index=1, value=1.0)])
        return SimpleNamespace(model=model, data=data)

    monkeypatch.setenv("ARK_API_KEY", "fixture")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture")
    monkeypatch.setattr(GenerativeModel, "generate", generate)
    monkeypatch.setattr(ArkMultimodalModel, "generate", generate)
    monkeypatch.setattr(MultimodalEmbeddings, "create", embedding)
    return calls


def test_import_parse_search_and_open_original(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    from engine.common.pyav_media import PyAVMedia
    from PIL import Image
    from server.desktop.runtime import open_library
    from server.service.mcp_evidence import StaleEvidenceError

    calls = _fixed_model_responses(monkeypatch)
    source = tmp_path / "episode.mp4"
    _fixture_movie(source)
    external_calls = []
    connect = socket.socket.connect

    def guard_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            external_calls.append("network")
            raise AssertionError("the fixed model replay must stay offline")
        return connect(sock, address)

    def guard_process(*_args, **_kwargs):
        external_calls.append("child process")
        raise AssertionError("desktop video processing must run in process")

    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(subprocess.Popen, "__init__", guard_process)
    root = tmp_path / "library"
    with open_library(root) as library:
        series = library.create_series("测试影片")
        imported = library.import_file(source, series_id=series.series_id, episode_order=1, idempotency_key="episode-1")
        repeated = library.import_file(source, series_id=series.series_id, episode_order=1, idempotency_key="episode-1")
        assert (repeated.video_id, repeated.video_parse_task_id) == (imported.video_id, imported.video_parse_task_id)
        _wait_for_completion(library, imported.video_parse_task_id, calls)
        assert {"video_parse_v2_chunk", "IntervalRefinementResult", "text", "video_url"} <= set(calls)
        for query, lexical in (("红旗", True), ("挥动旗帜", False)):
            before = len(calls)
            results = library.search(query, series_ids=[series.series_id], keywords_only=lexical)
            assert ("text" in calls[before:]) == (not lexical)
            assert len(results.hits) == 1
            hit = results.hits[0]
            assert (hit.video_id, hit.start_ms, hit.end_ms) == (imported.video_id, 300, 1200)
            playback = library.open_result(hit.source_ref)
            assert playback.path.is_relative_to(root)
            assert playback.path.read_bytes() == source.read_bytes()
            assert (playback.start_ms, playback.end_ms) == (300, 1200)
        destination = tmp_path / "hit.jpg"
        PyAVMedia().frame_at(playback.path, destination, at_ms=playback.start_ms, max_height=96, check_active=lambda: None)
        with Image.open(destination) as hit_frame:
            red, _, _ = hit_frame.getpixel((64, 48))
            assert abs(red - 24) <= 6  # The source ramps red by eight every 100 ms.
        wav_path = tmp_path / "hit.wav"
        PyAVMedia().audio(playback.path, wav_path, start_ms=playback.start_ms, end_ms=playback.end_ms,
                          check_active=lambda: None)
        with wave.open(str(wav_path), "rb") as sound:
            assert (sound.getnchannels(), sound.getframerate(), sound.getsampwidth(), sound.getnframes()) == (1, 16000, 2, 14400)
            pcm = array("h", sound.readframes(14400))
        for offset_ms, audible in ((30, False), (200, True), (450, False), (700, True), (850, False)):
            start = offset_ms * 16
            assert (max(abs(v) for v in pcm[start:start + 320]) > 1000) == audible
    before = list(calls)
    with open_library(root) as reopened:
        restored = reopened.search("红旗", series_ids=[series.series_id], keywords_only=True)
        assert [item.source_ref for item in restored.hits] == [hit.source_ref]
        assert reopened.open_result(hit.source_ref) == playback
        assert calls == before
        replacement = reopened.reparse(imported.session.upload_session_id, expected_job_version=1)
        assert replacement.version == 2
        _wait_for_completion(reopened, replacement.video_parse_task_id, calls)
        with pytest.raises(StaleEvidenceError):
            reopened.open_result(hit.source_ref)
        current = reopened.search("红旗", series_ids=[series.series_id], keywords_only=True)
        assert len(current.hits) == 1 and current.hits[0].source_ref != hit.source_ref
        assert reopened.open_result(current.hits[0].source_ref) == playback
    assert external_calls == []


def test_resume_two_chunks_and_refine_their_shared_interval(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    from engine.common.config import VideoParseRuntimeConfig
    from server.desktop import runtime

    model_entered, release_model = Event(), Event()

    def on_chunk():
        model_entered.set()
        assert release_model.wait(timeout=10)

    calls = _fixed_model_responses(monkeypatch, on_chunk=on_chunk)
    # Keep the production factory and consumer. Shorten the window for this two-second fixture.
    monkeypatch.setattr(runtime, "VideoParseRuntimeConfig", lambda **kwargs: VideoParseRuntimeConfig(
        chunk_duration_seconds=1, context_overlap_seconds=0, **kwargs,
    ))
    source = tmp_path / "two-chunks.mp4"
    _fixture_movie(source)
    with runtime.open_library(tmp_path / "library") as library:
        series = library.create_series("两段素材")
        result = library.import_file(source, series_id=series.series_id, episode_order=1, idempotency_key="two")
        task_id = result.video_parse_task_id
        try:
            assert model_entered.wait(15)
            running = library.task(task_id).task
            assert running.chunk_count == 2
            paused = library.set_paused(task_id, paused=True, expected_version=running.version,
                                        expected_lease_version=running.lease_version).task
            assert paused.detail["execution_control"]["paused"]
        finally:
            release_model.set()
        resumed = library.set_paused(task_id, paused=False, expected_version=paused.version,
                                     expected_lease_version=paused.lease_version).task
        assert not resumed.detail["execution_control"]["paused"]
        _wait_for_completion(library, task_id, calls)
        assert "IntervalRefinementResult" in calls
        hits = library.search("红旗", series_ids=[series.series_id], keywords_only=True).hits
        assert sorted((hit.start_ms, hit.end_ms) for hit in hits) == [(300, 1000), (1300, 2000)]
        assert all(library.open_result(hit.source_ref).start_ms == hit.start_ms for hit in hits)

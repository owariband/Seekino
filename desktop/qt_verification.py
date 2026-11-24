"""Verify the packaged player's rendered output against an indexed local fixture."""

import os
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickWindow

from .qt_controller import _request
from .qt_player import VideoSurface
from server.desktop.playback import PlaybackDecoder


def _first_samples(path, target):
    decoder = PlaybackDecoder(path, rgb=True)
    try:
        decoder.seek(target)
        image = None
        video_ms = audio_ms = None
        while video_ms is None or audio_ms is None:
            sample = decoder.next_sample()
            if sample is None:
                raise ValueError("验收原片缺少画面或音轨")
            if sample[0] == 0 and video_ms is None:
                _, timestamp, width, height, pixels, stride = sample
                image = QImage(pixels, width, height, stride, QImage.Format.Format_RGB888).copy()
                video_ms = round(timestamp / 1000)
            elif sample[0] == 1 and audio_ms is None:
                assert sample[2] > 0 and len(sample[3]) == sample[2] * 4
                audio_ms = round(sample[1] / 48)
        return video_ms, audio_ms, image
    finally:
        decoder.close()


def _wait(player, condition):
    deadline = monotonic() + 8
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(12)

    def poll():
        if condition() or player.error or monotonic() >= deadline:
            loop.quit()

    timer.timeout.connect(poll)
    timer.start()
    loop.exec()
    if player.error:
        raise ValueError(player.error)
    if not condition():
        raise TimeoutError("播放器在验收时间内没有到达预期状态")


def run(app, library: Path, query: str) -> int:
    _request("open", root=str(library))
    try:
        series = _request("seriesList")
        jobs = _request("tasks")
        result = _request("search", query=query, keywords=True, seriesIDs=[s["id"] for s in series])
        hit = result["hits"][0]
        video_id = hit["video_id"]
        assert any(job["latest"] and job["status"] == "COMPLETE" and job["videoID"] == video_id for job in jobs)
        assert any(item["id"] == video_id for s in series for item in _request("seriesDetail", id=s["id"]))
        source = _request("openResult", sourceRef=hit["source_ref"])
        full = _request("openVideo", videoID=video_id)
        path, start, end, duration = (source[k] for k in ("path", "start_ms", "end_ms", "duration_ms"))
        assert (start, end) == (hit["start_ms"], hit["end_ms"])
        assert full["path"] == path and (full["start_ms"], full["end_ms"]) == (0, duration)
        video_ms, audio_ms, _ = _first_samples(path, start)
        assert start <= video_ms < end and start <= audio_ms < end

        window = QQuickWindow()
        window.resize(320, 192)
        surface = VideoSurface(window.contentItem())
        surface.setWidth(320)
        surface.setHeight(192)
        surface.muted = True
        try:
            window.show()
            surface.open(path, start, duration)
            _wait(surface, lambda: surface.hasFrame and surface.positionMs >= start + 150)
            played = surface.positionMs
            surface.setPlaying(False)
            target = max(0, start - 55)  # The 10-fps fixture covers this time between two frame PTS.
            surface.seek(target)
            _wait(surface, lambda: surface.hasFrame)
            assert surface.positionMs == target and not surface.playing
            rendered = window.grabWindow()
            assert not rendered.isNull(), "Qt Quick 没有绘制验收窗口"
            _, _, frame = _first_samples(path, target)
            actual = rendered.pixelColor(rendered.width() // 2, rendered.height() // 2).getRgb()[:3]
            expected = frame.pixelColor(frame.width() // 2, frame.height() // 2).getRgb()[:3]
            assert max(abs(a - b) for a, b in zip(actual, expected)) <= 12, (actual, expected)
            surface.setPlaying(True)
            _wait(surface, lambda: surface.positionMs >= target + 120)
            print(f"pid={os.getpid()} hit=[{start},{end})ms video={video_ms}ms audio={audio_ms}ms "
                  f"played={played}ms pausedSeek={target}ms paintedRGB={actual} resumed={surface.positionMs}ms",
                  flush=True)
        finally:
            surface.close()
            window.hide()
    finally:
        _request("close")
    return 0

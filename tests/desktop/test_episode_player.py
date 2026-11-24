"""Native playback controls use source milliseconds and published per-episode evidence."""
from array import array
from math import pi, sin

import pytest

from desktop.episode_find import episode_matches


def test_find_searches_variants_and_respects_visible_people():
    def scene(start, text, people, kind="SCENE"):
        return {"start_ms": start, "end_ms": start + 500, "kind": kind,
                "description": text, "visible_persons": people}
    timeline = {
        "text": {"ready": True, "cues": [
            {"start_ms": 2100, "end_ms": 2600, "source": "dialogue", "texts": ["海邊的鐘錶店"]},
            {"start_ms": 3100, "end_ms": 3500, "source": "screen_text", "texts": ["Clock Shop", "鐘 錶 店"]},
        ]},
        "visual": {"ready": True, "known_persons": ["青山燈"], "cues": [
            scene(300, "鐘錶店門口", ["青山燈"]), scene(350, "有人說話", [], "DIALOGUE"),
        ]},
    }
    rows, semantic = episode_matches(timeline, "钟表店", True)
    assert [(r["source"], r["positionMs"]) for r in rows] == [
        ("visual", 550), ("dialogue", 2100), ("screen_text", 3100),
    ] and semantic
    expanded, semantic = episode_matches(timeline, "钟楼", True, expansions=["钟表店", "Clock"])
    assert [(r["source"], r["positionMs"]) for r in expanded] == [
        ("visual", 550), ("dialogue", 2100), ("screen_text", 3100),
    ] and not semantic
    assert episode_matches(timeline, "青 山 燈", True)[0][0]["positionMs"] == 550
    related = [scene(6000, "出现另一人", []), scene(9000, "门外", ["青山燈"])]
    rows, _ = episode_matches(timeline, "青山 在 店", True, related)
    assert [(r["source"], r["positionMs"]) for r in rows] == [("visual", 550), ("related", 9250)]
    rows, _ = episode_matches(timeline, "钟表店", False, related)
    assert [r["source"] for r in rows] == ["dialogue", "screen_text"]


@pytest.mark.parametrize("rate", [0.5, 0.75, 1.25, 1.5, 2.0])
def test_pcm_tempo_preserves_tone_and_scales_duration(rate):
    pytest.importorskip("PySide6")
    from desktop.qt_player import _AudioTempo

    hz = 48000
    frames = 480
    tone = array("h", (int(16000 * sin(2 * pi * 500 * i / hz)) for i in range(frames) for _ in range(2)))
    processor = _AudioTempo(rate)
    blocks = [packet for i in range(200) for packet in processor.push(i*frames, frames, tone.tobytes())]
    blocks.extend(processor.finish())
    length = sum(packet[2] for packet in blocks)
    assert abs(length / hz - 2 / rate) < 0.08
    assert all(a[1] + a[2] == b[1] for a, b in zip(blocks, blocks[1:]))
    samples = array("h", b"".join(packet[3] for packet in blocks))[::2]
    crossings = sum(a < 0 <= b for a, b in zip(samples, samples[1:]))
    assert abs(crossings * hz / length - 500) < 6

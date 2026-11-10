from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from engine.parsev2.camera import CameraReference, camera_replay
from engine.parsev2.camera_evidence import camera_motion_hints, ground_camera_pans
from engine.parsev2.schemas import VideoParseResult
from evals.clip_camera_reference import panorama, sample


@pytest.mark.parametrize(
    "case, expected",
    [
        ("right", "PAN_RIGHT"),
        ("left", "PAN_LEFT"),
        ("subject", None),
        ("cut", None),
        ("wipe", None),
        ("blank", None),
    ],
)
def test_sampled_translation_requires_distributed_non_cut_motion(tmp_path: Path, case, expected):
    coast = panorama()
    frames = []
    for i in range(12):
        if case == "wipe":
            image = sample(coast, "right", 0)
            other = sample(coast, "right", 11)
            edge = round(image.width * i / 11)
            image.paste(other.crop((0, 0, edge, image.height)), (0, 0))
        elif case == "blank":
            image = Image.new("RGB", (200, 200), "#758899")
            ImageDraw.Draw(image).rectangle((i * 9, 80, i * 9 + 20, 100), fill="white")
        else:
            image = sample(coast, case, i)
        path = tmp_path / f"{i}.jpg"
        image.save(path, quality=82)
        frames.append((65000 + round(i * 1000 / 3), path))
    hints = camera_motion_hints(frames)
    if expected is None:
        assert not hints
        return
    assert len(hints) == 1 and hints[0]["if_background_is_fixed"] == expected
    assert hints[0]["sampled_timing"] == "EASE_IN_OUT"
    assert hints[0]["first_sample_ms"] == 65000 and hints[0]["last_sample_ms"] == 68667
    assert 0.97 < hints[0]["travel_in_frame_widths"] < 1.03


def test_replay_distinguishes_unsupported_motion_unmeasured_easing_and_partial_measurements():
    ref = CameraReference(
        movement="PAN_LEFT",
        timing="UNKNOWN",
        start_framing="港口",
        end_framing="灯塔",
        visual_evidence="屋顶向右经过",
    )
    partial = dict(
        first_sample_ms=1250,
        last_sample_ms=1750,
        if_background_is_fixed="PAN_LEFT",
        sampled_timing="STEADY",
        travel_in_frame_widths=0.12,
    )
    assert camera_replay(ref, 1000, 3000, [partial])["easing_basis"] == "editor_default"
    full = {**partial, "first_sample_ms": 1000, "last_sample_ms": 2850, "travel_in_frame_widths": 0.43}
    resolved = camera_replay(ref, 1000, 3000, [full])
    assert (
        resolved["easing_basis"] == "sampled_background"
        and resolved["params"]["travel_in_view_widths"] == 0.43
    )
    assert camera_replay(ref, 1000, 3000, [{**full, "if_background_is_fixed": "PAN_RIGHT"}]) is None
    prior = {**full, "first_sample_ms": 100, "last_sample_ms": 1000, "if_background_is_fixed": "PAN_RIGHT"}
    assert camera_replay(ref, 1000, 3000, [prior, full]) == resolved
    assert camera_replay(ref.model_copy(update={"movement": "ORBIT"}), 1000, 3000, []) is None
    assert camera_replay(ref.model_copy(update={"timing": "STEPPED"}), 1000, 3000, []) is None


def test_sampled_replay_clips_both_boundaries_without_retiming_interior_frames():
    ref = CameraReference(movement="PAN_LEFT", timing="STEADY", start_framing="船",
                          end_framing="灯塔", visual_evidence="房屋共同向右移动")
    source = [(at, -at / 1000) for at in range(0, 1001, 100)]
    run = dict(first_sample_ms=0, last_sample_ms=1000, if_background_is_fixed="PAN_LEFT",
               sampled_timing="STEADY", travel_in_frame_widths=1, samples=source)
    replay = camera_replay(ref, 50, 950, [run])
    assert replay["sampled_background"]["travel_in_frame_widths"] == .8
    assert replay["sampled_background"]["sample_count"] == 9
    assert replay["params"]["travel_in_view_widths"] == .9
    progress = replay["params"]["progress_samples"]
    assert progress[0] == [0, 0] and progress[-1] == [1, 1]
    assert progress[1] == pytest.approx([50 / 900, 50 / 900])
    assert progress[-2] == pytest.approx([850 / 900, 850 / 900])


@pytest.mark.parametrize("second_start, second_end, move, expected", [
    (1600, 2700, "PAN_RIGHT", True), (1700, 2700, "PAN_RIGHT", False), (1600, 2700, "STATIC", False),
    (1600, 2700, "PAN_LEFT", False), (1600, 2100, "PAN_RIGHT", False),
])
def test_one_continuous_camera_for_adjacent_visual_beats(second_start, second_end, move, expected):
    def beat(start, end, movement, description):
        return dict(event_type="SCENE", start_seconds=start, end_seconds=end, participants=[], visible_persons=[],
                    description=description, camera_ref=dict(movement=movement, timing="STEADY",
                    start_framing="画面中可见灯塔", end_framing=description, visual_evidence="固定背景经过画面边缘"))

    a = beat(600, 1600, "PAN_RIGHT", "灯塔亮起")
    b = beat(second_start, second_end, move, "码头与船入画")
    result = VideoParseResult.model_validate({"persons": [], "timeline": [a, b]})
    run = dict(first_sample_ms=0, last_sample_ms=4000, if_background_is_fixed="PAN_RIGHT",
               sampled_timing="EASE_IN_OUT", travel_in_frame_widths=1,
               samples=[(i * 400, i / 10) for i in range(11)])
    grounded = ground_camera_pans(result, [run])
    observation = [e for e in grounded.timeline if e.camera_ref is not None]
    if not expected:
        assert len(observation) == 2 and len(grounded.timeline) == 2
        return
    assert len(observation) == 1
    event, = observation
    assert (event.start_seconds, event.end_seconds) == (0, 4000)
    assert (event.camera_ref.movement, event.camera_ref.timing) == ("PAN_RIGHT", "EASE_IN_OUT")
    assert event.camera_ref.end_framing == "码头与船入画"
    assert [e.description for e in grounded.timeline if e.camera_ref is None] == ["灯塔亮起", "码头与船入画"]
    assert camera_replay(event.camera_ref, 0, 4000, [run])["travel_basis"] == "sampled_background"
    assert ground_camera_pans(grounded, [run]) == grounded

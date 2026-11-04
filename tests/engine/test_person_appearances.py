from __future__ import annotations

import pytest

from engine.parsev2.schemas import VideoParseResult
from engine.rag.retrieval.person_appearances import PersonTimelineChunk, list_person_appearances

ANON_TRADITIONAL = "千早愛音"
ANON_SIMPLIFIED = "千早爱音"
TOMORI = "高松灯"


def _dialogue(speaker: str, start: int, end: int, *, visible: tuple[str, ...] = ()) -> dict:
    return dict(
        event_type="DIALOGUE", start_seconds=start, end_seconds=end,
        description=f"{speaker}的对白", participants=[speaker],
        visible_persons=list(visible), speaker=speaker,
        text_variants=[dict(language="ja", role="ORIGINAL", text="テスト")],
    )


def _scene(
    visible: tuple[str, ...], start: int, end: int, *, participants: tuple[str, ...] = (),
) -> dict:
    return dict(
        event_type="SCENE", start_seconds=start, end_seconds=end,
        description="人物出现在画面中", participants=list(participants),
        visible_persons=list(visible),
    )


def _chunk(
    video_id: str, video_chunk_id: str, persons: tuple[str, ...], events: list[dict],
) -> PersonTimelineChunk:
    return PersonTimelineChunk(
        video_id=video_id, video_chunk_id=video_chunk_id,
        result=VideoParseResult.model_validate({"persons": list(persons), "timeline": events}),
    )


def test_offscreen_speaker_counts_as_speaking_without_visible_role():
    chunk = _chunk("video", "chunk", (ANON_SIMPLIFIED,), [_dialogue(ANON_SIMPLIFIED, 0, 1000)])
    appearance, = list_person_appearances([chunk], [ANON_SIMPLIFIED])
    source, = appearance.sources
    assert source.matched_name == ANON_SIMPLIFIED
    assert source.timeline_index == 0
    assert source.roles == ("speaking", "participant")
    assert "visible" not in appearance.roles
    assert (appearance.start_seconds, appearance.end_seconds) == (0, 1000)


def test_visible_only_and_every_role_keep_the_fixed_order():
    visible_only = _chunk("video", "visible", (ANON_TRADITIONAL,), [
        _scene((ANON_TRADITIONAL,), 0, 1000),
    ])
    appearance, = list_person_appearances([visible_only], [ANON_TRADITIONAL])
    assert appearance.sources[0].roles == ("visible",)
    assert appearance.roles == ("visible",)

    participant_only = _chunk("video", "participant", (ANON_SIMPLIFIED,), [
        _scene((), 0, 1000, participants=(ANON_SIMPLIFIED,)),
    ])
    appearance, = list_person_appearances([participant_only], [ANON_SIMPLIFIED])
    assert appearance.sources[0].roles == ("participant",)

    spoken_and_seen = _chunk("video", "seen", (ANON_SIMPLIFIED,), [
        _dialogue(ANON_SIMPLIFIED, 0, 1000, visible=(ANON_SIMPLIFIED,)),
    ])
    appearance, = list_person_appearances([spoken_and_seen], [ANON_SIMPLIFIED])
    assert appearance.sources[0].roles == ("speaking", "visible", "participant")
    assert appearance.roles == ("speaking", "visible", "participant")


@pytest.mark.parametrize("second_start, expected", [
    (3000, [(0, 4000)]),
    (3001, [(0, 1000), (3001, 4001)]),
])
def test_merge_gap_boundary_is_inclusive_at_2000_ms(second_start, expected):
    chunk = _chunk("video", "chunk", (ANON_TRADITIONAL,), [
        _scene((ANON_TRADITIONAL,), 0, 1000),
        _scene((ANON_TRADITIONAL,), second_start, second_start + 1000),
    ])
    appearances = list_person_appearances([chunk], [ANON_TRADITIONAL], merge_gap_ms=2000)
    assert [(item.start_seconds, item.end_seconds) for item in appearances] == expected


def test_neighbouring_chunks_merge_across_the_same_video():
    first = _chunk("video", "chunk-1", (ANON_TRADITIONAL,), [
        _scene((ANON_TRADITIONAL,), 0, 1000),
    ])
    second = _chunk("video", "chunk-2", (ANON_TRADITIONAL,), [
        _scene((ANON_TRADITIONAL,), 3000, 4000),
    ])
    appearance, = list_person_appearances([second, first], [ANON_TRADITIONAL], merge_gap_ms=2000)
    assert (appearance.start_seconds, appearance.end_seconds) == (0, 4000)
    # Sources order by time even when the later Chunk arrives first.
    assert [source.video_chunk_id for source in appearance.sources] == ["chunk-1", "chunk-2"]


def test_duplicate_event_from_neighbouring_chunks_merges_in_chunk_order():
    duplicate = _scene((ANON_TRADITIONAL,), 1000, 2000)
    first = _chunk("video", "chunk-1", (ANON_TRADITIONAL,), [duplicate])
    second = _chunk("video", "chunk-2", (ANON_TRADITIONAL,), [duplicate])
    appearance, = list_person_appearances([first, second], [ANON_TRADITIONAL])
    assert (appearance.start_seconds, appearance.end_seconds) == (1000, 2000)
    assert [source.video_chunk_id for source in appearance.sources] == ["chunk-1", "chunk-2"]


def test_different_videos_never_merge_and_follow_first_input_order():
    later = _chunk("video-b", "chunk-b", (ANON_TRADITIONAL,), [
        _scene((ANON_TRADITIONAL,), 5000, 6000),
    ])
    earlier = _chunk("video-a", "chunk-a", (ANON_TRADITIONAL,), [
        _scene((ANON_TRADITIONAL,), 0, 1000),
    ])
    appearances = list_person_appearances([later, earlier], [ANON_TRADITIONAL])
    assert [(item.video_id, item.start_seconds, item.end_seconds) for item in appearances] == [
        ("video-b", 5000, 6000), ("video-a", 0, 1000),
    ]


def test_both_spellings_match_and_stay_verbatim_in_first_appearance_order():
    chunk = _chunk("video", "chunk", (ANON_TRADITIONAL, ANON_SIMPLIFIED), [
        _dialogue(ANON_TRADITIONAL, 0, 1000),
        _dialogue(ANON_SIMPLIFIED, 1000, 2000),
    ])
    appearance, = list_person_appearances([chunk], [ANON_TRADITIONAL, ANON_SIMPLIFIED])
    assert appearance.matched_names == (ANON_TRADITIONAL, ANON_SIMPLIFIED)
    assert [source.matched_name for source in appearance.sources] == [
        ANON_TRADITIONAL, ANON_SIMPLIFIED,
    ]


def test_two_character_substring_matches_but_single_character_does_not():
    chunk = _chunk("video", "chunk", (ANON_SIMPLIFIED, TOMORI), [
        _dialogue(ANON_SIMPLIFIED, 0, 1000),
        _dialogue(TOMORI, 3000, 4000),
    ])
    assert [item.matched_names for item in list_person_appearances([chunk], ["爱音"])] == [
        (ANON_SIMPLIFIED,),
    ]
    assert list_person_appearances([chunk], ["灯"]) == ()
    tomori = list_person_appearances([chunk], ["高松灯"])
    assert [item.matched_names for item in tomori] == [(TOMORI,)]
    assert tomori[0].sources[0].timeline_index == 1
    assert tomori[0].sources[0].event_type == "DIALOGUE"


@pytest.mark.parametrize("names", [[], [""], [" ", "　"]])
def test_names_without_any_nonblank_value_are_rejected(names):
    with pytest.raises(ValueError, match="names"):
        list_person_appearances([], names)


@pytest.mark.parametrize("merge_gap_ms", [-1, 1.5, True, "2000"])
def test_merge_gap_must_be_a_nonnegative_integer(merge_gap_ms):
    with pytest.raises(ValueError, match="merge_gap_ms"):
        list_person_appearances([], [ANON_SIMPLIFIED], merge_gap_ms=merge_gap_ms)


def test_repeated_calls_return_identical_results():
    chunk = _chunk("video", "chunk", (ANON_TRADITIONAL, ANON_SIMPLIFIED), [
        _dialogue(ANON_TRADITIONAL, 0, 1000),
        _dialogue(ANON_SIMPLIFIED, 4000, 5000),
    ])
    first = list_person_appearances([chunk], [ANON_TRADITIONAL, ANON_SIMPLIFIED])
    second = list_person_appearances([chunk], [ANON_TRADITIONAL, ANON_SIMPLIFIED])
    assert first == second
    assert [item.model_dump(mode="json") for item in first] == [
        item.model_dump(mode="json") for item in second
    ]

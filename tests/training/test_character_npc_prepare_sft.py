from __future__ import annotations

import hashlib
import json

import pytest

from training.character_NPC.prepare_sft import prepare


def _dialogue(speaker, text, start, end):
    return {
        "event_type": "DIALOGUE",
        "speaker": speaker,
        "text_variants": [{"language": "zh-Hans", "text": text}],
        "start_seconds": start,
        "end_seconds": end,
    }


def _record(events, *, chunk_id="chunk-0", indices=None, video_id="train-video"):
    return {
        "source": {
            "video_id": video_id,
            "parse_task_id": f"task-{video_id}",
            "chunk_id": chunk_id,
            "content_version": 3,
            "timeline_indices": list(range(len(events))) if indices is None else indices,
        },
        "timeline": events,
    }


def _prepare_records(tmp_path, records):
    source = tmp_path / "timeline-events.jsonl"
    validation = _record(
        [
            _dialogue("高松燈", "验证集问题", 1000, 1500),
            _dialogue("千早愛音", "验证集回答", 1600, 2000),
        ],
        video_id="validation-video",
    )
    source.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [*records, validation]),
        encoding="utf-8",
    )
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {"episodes": [
                {"video_id": "train-video", "episode_order": 3},
                {"video_id": "validation-video", "episode_order": 1},
            ]}
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "prepared"
    report = prepare(source, destination, {1})
    rows = [
        json.loads(line)
        for line in (destination / "train.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    return source, destination, rows, report


def test_prepare_merges_complete_turns_before_building_targets(tmp_path):
    record = _record(
        [
            _dialogue("千早愛音", "回答后半句", 3000, 3900),
            _dialogue("高松燈", "问题前半句", 1000, 1500),
            {"event_type": "ACTION", "start_seconds": 1750, "end_seconds": 1900},
            _dialogue("高松燈", "问题后半句", 2000, 2500),
            _dialogue("Anon", "回答前半句", 2600, 2800),
            _dialogue("長崎素世", "换人提问", 4000, 4500),
            _dialogue("千早愛音", "下一次回答", 4600, 4900),
        ],
        indices=[90, 13, 76, 14, 21, 22, 23],
    )

    source, destination, rows, report = _prepare_records(tmp_path, [record])

    assert report["counts"] == {"train": 2, "validation": 1}
    assert report["merged_dialogue_fragments"] == 2
    first, second = rows
    assert [message["role"] for message in first["messages"]] == [
        "system", "user", "assistant"
    ]
    assert first["messages"][1]["content"] == "高松燈：问题前半句\n问题后半句"
    assert first["messages"][2]["content"] == "回答前半句\n回答后半句"
    assert "回答前半句" not in first["messages"][1]["content"]
    assert first["timeline_index"] == 21
    assert first["timeline_indices"] == [21, 90]
    assert (first["start_ms"], first["end_ms"]) == (2600, 3900)
    assert first["context"][0]["timeline_index"] == 13
    assert first["context"][0]["timeline_indices"] == [13, 14]
    assert first["context"][0]["end_ms"] == 2500
    assert second["messages"][2]["content"] == "下一次回答"
    assert [turn["text"] for turn in second["context"]] == [
        "问题前半句\n问题后半句", "回答前半句\n回答后半句", "换人提问"
    ]
    assert second["context"][1]["timeline_indices"] == [21, 90]
    assert first["source"] == {
        key: value for key, value in record["source"].items() if key != "timeline_indices"
    }
    raw_lines = (destination / "train.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 2
    assert "回答前半句\\n回答后半句" in raw_lines[0]
    assert report["time_unit"] == "ms"
    assert report["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert json.loads(source.read_text(encoding="utf-8").splitlines()[0]) == record


@pytest.mark.parametrize(
    "boundary",
    ["scene", "unknown_speaker", "ambiguous_chinese", "missing_chinese", "gap", "overlap"],
)
def test_prepare_does_not_merge_or_reuse_context_across_boundaries(tmp_path, boundary):
    events = [
        _dialogue("高松燈", "原来的问题", 1000, 1500),
        _dialogue("Anon", "边界前回答", 1600, 2000),
    ]
    next_start = 3000
    if boundary == "scene":
        events.append({"event_type": "SCENE", "start_seconds": 2100, "end_seconds": 2500})
    elif boundary in {"unknown_speaker", "ambiguous_chinese", "missing_chinese"}:
        event = _dialogue("長崎素世", "无法可靠使用", 2100, 2500)
        if boundary == "unknown_speaker":
            event["speaker"] = None
        elif boundary == "ambiguous_chinese":
            event["text_variants"].append({"language": "zh-Hans", "text": "另一种内容"})
        else:
            event["text_variants"] = [{"language": "ja", "text": "日本語だけ"}]
        events.append(event)
    elif boundary == "gap":
        next_start = 17001
    else:
        next_start = 1999
    events.extend([
        _dialogue("Anon", "无上下文前半句", next_start, next_start + 100),
        _dialogue("千早愛音", "无上下文后半句", next_start + 200, next_start + 300),
        _dialogue("高松燈", "重新提问", next_start + 400, next_start + 500),
        _dialogue("Anon", "重新回答", next_start + 600, next_start + 700),
    ])

    _, _, rows, report = _prepare_records(tmp_path, [_record(events)])

    assert [row["messages"][2]["content"] for row in rows] == [
        "边界前回答", "重新回答"
    ]
    assert report["skipped"]["target_without_local_context"] == 2
    assert report["merged_dialogue_fragments"] == 1
    assert "原来的问题" not in rows[1]["messages"][1]["content"]
    assert "边界前回答" not in rows[1]["messages"][1]["content"]
    if boundary in {"unknown_speaker", "ambiguous_chinese", "missing_chinese"}:
        assert report["skipped"]["unknown_speaker_or_ambiguous_chinese"] == 1


@pytest.mark.parametrize("gap_ms", [0, 15000])
def test_prepare_merges_at_inclusive_gap_limits(tmp_path, gap_ms):
    events = [
        _dialogue("高松燈", "问题", 1000, 1500),
        _dialogue("Anon", "前句", 1600, 2000),
        _dialogue("Anon", "后句", 2000 + gap_ms, 2100 + gap_ms),
    ]

    _, _, rows, report = _prepare_records(tmp_path, [_record(events)])

    assert len(rows) == 1
    assert rows[0]["messages"][2]["content"] == "前句\n后句"
    assert (rows[0]["start_ms"], rows[0]["end_ms"]) == (1600, 2100 + gap_ms)
    assert report["merged_dialogue_fragments"] == 1


def test_prepare_keeps_stable_order_for_equal_start_times(tmp_path):
    record = _record(
        [
            _dialogue("高松燈", "先出现的片段", 1000, 1000),
            _dialogue("高松燈", "后出现的片段", 1000, 1000),
            _dialogue("Anon", "回答", 1100, 1200),
        ],
        indices=[55, 14, 91],
    )

    _, _, rows, _ = _prepare_records(tmp_path, [record])

    assert rows[0]["context"][0]["text"] == "先出现的片段\n后出现的片段"
    assert rows[0]["context"][0]["timeline_indices"] == [55, 14]
    assert rows[0]["context"][0]["timeline_index"] == 55


def test_prepare_limits_context_by_merged_turns(tmp_path):
    events = []
    for index, speaker in enumerate(["高松燈", "長崎素世", "高松燈", "長崎素世", "高松燈"]):
        start = 1000 + index * 1000
        events.extend([
            _dialogue(speaker, f"第{index}轮前句", start, start + 100),
            _dialogue(speaker, f"第{index}轮后句", start + 200, start + 300),
        ])
    events.append(_dialogue("Anon", "最终回答", 6000, 6500))

    _, _, rows, report = _prepare_records(tmp_path, [_record(events)])

    context = rows[0]["context"]
    assert len(context) == 4
    assert [turn["timeline_indices"] for turn in context] == [[2, 3], [4, 5], [6, 7], [8, 9]]
    assert [turn["text"] for turn in context] == [
        f"第{index}轮前句\n第{index}轮后句" for index in range(1, 5)
    ]
    assert "第0轮" not in rows[0]["messages"][1]["content"]
    assert report["merged_dialogue_fragments"] == 5


def test_prepare_keeps_chunk_and_episode_boundaries(tmp_path):
    records = [
        _record([
            _dialogue("高松燈", "第一块问题", 1000, 1500),
            _dialogue("Anon", "第一块回答", 1600, 2000),
        ]),
        _record([
            _dialogue("Anon", "第二块孤立前句", 2100, 2200),
            _dialogue("Anon", "第二块孤立后句", 2300, 2400),
            _dialogue("高松燈", "第二块问题", 2500, 2600),
            _dialogue("Anon", "第二块回答", 2700, 2800),
        ], chunk_id="chunk-1"),
    ]

    _, destination, rows, report = _prepare_records(tmp_path, records)

    assert [row["source"]["chunk_id"] for row in rows] == ["chunk-0", "chunk-1"]
    assert rows[0]["messages"][2]["content"] == "第一块回答"
    assert "第一块" not in rows[1]["messages"][1]["content"]
    assert report["skipped"]["target_without_local_context"] == 2
    validation = [
        json.loads(line)
        for line in (destination / "validation.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {row["episode"] for row in rows} == {3}
    assert {row["episode"] for row in validation} == {1}
    assert {row["source"]["video_id"] for row in rows} == {"train-video"}
    assert {row["source"]["video_id"] for row in validation} == {"validation-video"}


def test_prepare_refuses_to_overwrite_existing_dataset(tmp_path):
    record = _record([
        _dialogue("高松燈", "问题", 1000, 1500),
        _dialogue("Anon", "回答", 1600, 2000),
    ])
    source, destination, _, _ = _prepare_records(tmp_path, [record])
    before = {path.name: path.read_bytes() for path in destination.iterdir()}

    with pytest.raises(FileExistsError):
        prepare(source, destination, {1})

    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before

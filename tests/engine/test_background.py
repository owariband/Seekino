import base64
import json
from pathlib import Path

import pytest
from gateway.multimodal import ImageInput, TextInput

from engine.parse.background import (
    BackgroundImage,
    build_background_image_inputs,
    extract_background_image_ids,
    prepare_background_text,
)


def test_background_image_references_are_deduplicated_in_document_order() -> None:
    document = (
        "![灯](asset://tomori_01)\n"
        "![爱音][anon]\n\n"
        "[anon]: asset://anon-02\n"
        '{"portrait": "asset://tomori_01", "other": "asset://ricky_03"}\n'
        "![再次出现爱音](asset://anon-02)"
    )

    assert extract_background_image_ids(document) == [
        "tomori_01",
        "anon-02",
        "ricky_03",
    ]
    assert extract_background_image_ids(
        "![未托管](https://example.com/image.png) ![本地](images/tomori.png)"
    ) == []


def test_markdown_background_keeps_the_complete_document_and_character_fields() -> None:
    document = (
        "# 人物与术语\n\n"
        "这是用户补充的校园设定。\n\n"
        "![灯](asset://tomori_01)\n\n"
        "## 用户自定义说明\n\n不要把参考图动作当作视频事件。"
    )
    characters = [
        {
            "name": "高松灯",
            "aliases": ["灯"],
            "appearance": {"hair": "灰色短发", "portrait": "asset://tomori_01"},
        }
    ]

    text = prepare_background_text(
        characters,
        {"format": "markdown", "content": document},
    )

    assert text is not None
    assert document in text
    assert "高松灯" in text
    assert '"appearance"' in text
    assert "灰色短发" in text
    assert extract_background_image_ids(text) == ["tomori_01"]


@pytest.mark.parametrize("suffix", [".png", "/foo", "#fragment", "?size=1", "=", "%2F"])
def test_background_references_cannot_be_truncated_to_an_existing_image(suffix) -> None:
    with pytest.raises(ValueError, match="invalid background image reference"):
        extract_background_image_ids(f"![人物](asset://portrait_01{suffix})")


def test_background_reference_requires_a_nonempty_image_id() -> None:
    with pytest.raises(ValueError, match="invalid background image reference"):
        extract_background_image_ids('"asset://"')


@pytest.mark.parametrize(
    "document",
    [
        {
            "customSchema": {
                "type": "object",
                "properties": {"band": {"type": "string", "enum": ["MyGO!!!!!"]}},
            },
            "world": {"terms": ["羽丘", "月之森"], "relationships": None},
            "portrait": "asset://tomori_01",
        },
        ["用户自定义根数组", {"portrait": "asset://anon-02", "active": True}, 42],
    ],
)
def test_json_background_preserves_arbitrary_nested_data(document) -> None:
    text = prepare_background_text(
        [],
        {"format": "json", "content": json.dumps(document, ensure_ascii=False)},
    )

    assert text is not None
    assert json.loads(text) == document


def test_json_background_decodes_escaped_slashes_before_extracting_references() -> None:
    text = prepare_background_text(
        [],
        {
            "format": "json",
            "content": (
                r'{"characters": [{"name": "灯", "portrait": "asset:\/\/tomori_01"}],'
                r' "secondPortrait": "asset:\/\/anon-02"}'
            ),
        },
    )

    assert text is not None
    assert json.loads(text)["characters"][0]["portrait"] == "asset://tomori_01"
    assert extract_background_image_ids(text) == ["tomori_01", "anon-02"]


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e309"])
def test_json_background_rejects_nonfinite_numbers(number) -> None:
    with pytest.raises(ValueError):
        prepare_background_text(
            [], {"format": "json", "content": '{"custom": ' + number + "}"},
        )


def test_json_reference_followed_by_escaped_newline_is_not_part_of_the_id() -> None:
    text = prepare_background_text([], {
        "format": "json",
        "content": json.dumps({"note": "参考 asset://tomori_01\n人物说明"}, ensure_ascii=False),
    })
    assert extract_background_image_ids(text) == ["tomori_01"]


def test_document_wrapper_preserves_arbitrary_extra_fields() -> None:
    text = prepare_background_text([], {
        "format": "markdown", "content": "完整人物背景",
        "customSchema": {"properties": {"portrait": {"type": "string"}}},
        "cast": [{"portrait": "asset://tomori_01"}],
    })

    assert text is not None
    assert "完整人物背景" in text
    extra = json.loads(text.split("\n\n", 1)[1])
    assert extra["customSchema"]["properties"]["portrait"]["type"] == "string"
    assert extra["cast"][0]["portrait"] == "asset://tomori_01"
    assert extract_background_image_ids(text) == ["tomori_01"]


def test_legacy_series_bible_keeps_extra_fields_and_character_information() -> None:
    document = "# 旧角色背景\n![灯](asset://tomori_01)"
    text = prepare_background_text(
        [{"name": "高松灯", "aliases": ["灯"]}],
        {
            "seriesBible": document,
            "sourceUri": "assets/characters.md",
            "customSchema": {"speaker": {"type": "string"}},
        },
    )

    assert text is not None
    assert document in text
    assert "assets/characters.md" in text
    assert '"customSchema"' in text
    assert '"speaker"' in text
    assert "高松灯" in text
    assert '"aliases"' in text
    assert extract_background_image_ids(text) == ["tomori_01"]


def test_legacy_arbitrary_json_is_not_restricted_to_series_bible_fields() -> None:
    data = {
        "format": {"type": "user-defined", "version": 2},
        "customSchema": {"properties": {"emotion": {"enum": ["困惑", "平静"]}}},
        "cast": [{"nickname": "灯", "portrait": "asset://tomori_01"}],
        "productionNotes": ["只描述当前视频证据", {"version": 2}],
    }

    text = prepare_background_text([], data)

    assert text is not None
    assert json.loads(text) == data
    assert extract_background_image_ids(text) == ["tomori_01"]


def test_absent_background_remains_optional() -> None:
    assert prepare_background_text([], None) is None
    assert build_background_image_inputs([]) == []


def test_image_inputs_pair_each_stable_reference_with_its_actual_file(tmp_path) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+aWZkAAAAASUVORK5CYII="
    )
    images = []
    for image_id, filename in [("tomori_01", "first.png"), ("anon-02", "second.png")]:
        path = tmp_path / filename
        path.write_bytes(png)
        images.append(BackgroundImage(image_id=image_id, path=path))

    inputs = build_background_image_inputs(images)

    assert len(inputs) == 4
    for offset, image in enumerate(images):
        label, image_input = inputs[2 * offset : 2 * offset + 2]
        assert isinstance(label, TextInput)
        assert f"asset://{image.image_id}" in label.text
        assert isinstance(image_input, ImageInput)
        assert Path(image_input.path) == image.path
        assert image.path.read_bytes() == png

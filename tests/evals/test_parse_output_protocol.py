from xml.etree.ElementTree import ParseError

import pytest

from agent.video_parse_v2.timelineEvent_canvas import ParseContextCanvas
from evals.parse_output_protocol import LanguageDefaults, decode_result


DEFAULTS = LanguageDefaults("zh-Hans", "TRANSLATION", "ja")
XML = '''<timeline>
<event start="60000" end="64200" participants="千早爱音,高松灯" visible="千早爱音">
<dialogue speaker="千早爱音" lang="zh-Hans">
你其实很想组乐队吧
<original lang="ja">本当はバンドやりたいんじゃないの？</original>
<description>千早爱音抬头发问。</description>
</dialogue>
</event>
<event start="64200" end="66000" participants="高松灯" visible="高松灯">
<action>
高松灯低下头。
</action>
</event>
</timeline>'''


def decode(raw, output_format="xml"):
    return decode_result(raw, output_format=output_format, start_ms=60000,
                         end_ms=120000, defaults=DEFAULTS)


def test_layout_newlines_and_indentation_do_not_change_domain_result():
    expected = decode(XML)
    assert decode(XML.replace("\n", "")) == expected
    assert decode(XML.replace("\n", "\n  ")) == expected
    assert decode(XML.replace("\n", "\r\n")) == expected
    assert decode(expected.model_dump_json(), "json") == expected


def test_internal_text_newlines_spaces_and_escaped_symbols_are_preserved():
    raw = XML.replace("你其实很想组乐队吧", "A &amp; B\n第二行  &lt;三&gt;")
    result = decode(raw)
    assert result.timeline[0].text_variants[0].text == "A & B\n第二行  <三>"


def test_unknown_speaker_language_override_and_missing_description():
    result = decode('''<timeline><event start="60000" end="61000">
<dialogue lang="ja" role="original">はい</dialogue></event></timeline>''')
    assert result.persons == []
    assert result.timeline[0].speaker is None
    assert result.timeline[0].text_variants[0].role == "ORIGINAL"


@pytest.mark.parametrize("raw", [
    XML.replace('start="60000"', 'start=60000'),
    XML.replace('start="60000"', 'start="60.0"'),
    XML.replace('start="60000"', 'start="59999"'),
    XML.replace('end="66000"', 'end="120001"'),
    XML.replace('end="64200"', 'end="60000"'),
    XML.replace('participants="千早爱音,高松灯"', 'participants="高松灯"'),
    XML.replace('speaker="千早爱音"', 'speaker=""'),
    XML.replace('</dialogue>', '<action>点头</action></dialogue>'),
    XML.replace('</event>', '<action>点头</action></event>', 1),
    XML.replace('</dialogue>', '<translation lang="zh-Hans">重复</translation></dialogue>'),
    XML.replace('</dialogue>', '<translation>missing language</translation></dialogue>'),
    XML.replace('</dialogue>', '<description>重复</description></dialogue>'),
    XML.replace('lang="zh-Hans"', 'tag="zh-Hans"'),
    XML.replace('你其实很想组乐队吧', ''),
    XML.replace('<action>', '<unknown>').replace('</action>', '</unknown>'),
    XML.removesuffix('</timeline>'),
    XML.replace('<timeline>', '<timeline>unexpected'),
    XML.replace('</original>', '</original>unclassified tail'),
    '<!DOCTYPE timeline [<!ENTITY x "test">]>' + XML,
])
def test_invalid_or_incomplete_output_is_not_repaired(raw):
    with pytest.raises((ValueError, ParseError)):
        decode(raw)


def test_json_duplicate_keys_are_rejected_and_sorting_is_shared():
    result = decode(XML)
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        decode('{"persons":[],"persons":[],"timeline":[]}', "json")
    value = result.model_dump()
    value["timeline"].reverse()
    import json
    assert decode(json.dumps(value), "json") == result


def test_decoded_result_can_be_written_atomically_and_reused_by_refine():
    result = decode(XML)
    member = {"sequence_no": 1, "version": 0, "bounds": (60000, 120000),
              "content": {"persons": [], "timeline": []}}
    canvas = ParseContextCanvas(scope="xml-experiment", stage="understanding", members={"chunk": member})
    canvas.write(role="understanding", batch_id="xml-output", expected_revision=canvas.revision,
                 operations=[{"op": "persons", "member_ref": "chunk", "action": "set", "values": result.persons},
                             *[{"op": "add_event", "member_ref": "chunk", "key": str(i), "event": event}
                               for i, event in enumerate(result.timeline)]])
    assert canvas.result("chunk") == result
    refine = ParseContextCanvas(scope="refine", stage="refine", members={
        "chunk": {**member, "version": 1, "content": canvas.result("chunk")}})
    assert refine.result("chunk") == result

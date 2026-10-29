import json
from xml.etree.ElementTree import ParseError

import pytest

from evals.parse_compact_protocol import decode_compact_result


def decode(raw, fmt="xml"):
    return decode_compact_result(raw, output_format=fmt, start_ms=60000, end_ms=120000)


XML = '''<timeline>
<event start="60000" end="64000"><shot>甲的眼部特写。</shot></event>
<event start="60000" end="64000"><dialogue speaker="乙">A &amp; B
第二行  &lt;三&gt;<original>こんにちは</original><translation lang="en">Hello</translation></dialogue></event>
<event start="64000" end="66000"><action>甲点头。</action></event>
</timeline>'''


def test_same_fields_have_identical_json_xml_meaning_and_preserve_shot():
    expected = {"timeline": [
        {"start":60000,"end":64000,"type":"shot","text":"甲的眼部特写。"},
        {"start":60000,"end":64000,"type":"dialogue","speaker":"乙",
         "text":"A & B\n第二行  <三>","original":{"text":"こんにちは"},
         "translations":[{"lang":"en","text":"Hello"}]},
        {"start":64000,"end":66000,"type":"action","text":"甲点头。"},
    ]}
    assert decode(XML) == decode(json.dumps(expected), "json")
    assert decode(XML.replace("\n", "\r\n")) == decode(XML)
    assert decode(XML).timeline[0].type == "shot"
    assert "participants" not in decode(XML).model_dump_json()


def test_unknown_identity_language_override_and_stable_order():
    value = {"timeline": [
        {"type":"action","start":62000,"end":63000,"text":"点头"},
        {"type":"dialogue","start":60000,"end":62000,"text":"はい",
         "lang":"ja","role":"original","speaker":None},
    ]}
    result = decode(json.dumps(value), "json")
    assert result.timeline[0].speaker is None
    assert result.timeline[0].original is None
    assert result.timeline[0].start == 60000


@pytest.mark.parametrize("kind", ["scene","shot","action","emotion","relation-change","other","screen-text"])
def test_all_non_dialogue_types_round_trip(kind):
    result = decode(f'<timeline><event start="60000" end="61000"><{kind}>内容</{kind}></event></timeline>')
    assert decode(result.model_dump_json(), "json") == result


@pytest.mark.parametrize("raw", [
    XML.replace('start="60000"', 'start="59999"'),
    XML.replace('end="66000"', 'end="120001"'),
    XML.replace('end="64000"', 'end="60000"'),
    XML.replace('start="60000"', 'start="60.5"'),
    XML.replace('start="60000"', 'start=60000'),
    XML.replace('<event start=', '<event participants="甲" start='),
    XML.replace('<shot>', '<shot visible="甲">'),
    XML.replace('speaker="乙"', 'speaker=" "'),
    XML.replace('</dialogue>', '<translation lang="zh-Hans">重复</translation></dialogue>'),
    XML.replace('</dialogue>', '<original>重复</original></dialogue>'),
    XML.replace('lang="en"', ''),
    XML.replace('</shot>', '</shot><action>动作</action>'),
    XML.replace('</original>', '</original>意外文字'),
    XML.removesuffix('</timeline>'),
    '<!DOCTYPE timeline [<!ENTITY x "test">]>'+XML,
])
def test_invalid_xml_is_not_repaired(raw):
    with pytest.raises((ValueError, ParseError)):
        decode(raw)


@pytest.mark.parametrize("patch", [
    {"participants":[]}, {"visible_persons":[]}, {"speaker":"甲"},
    {"start":True}, {"start":60000.0}, {"start":59999}, {"end":120001},
    {"text":" "}, {"type":"unknown"},
])
def test_json_contract_has_same_strict_bounds_and_no_old_fields(patch):
    event = {"type":"shot","start":60000,"end":61000,"text":"特写",**patch}
    with pytest.raises(ValueError):
        decode(json.dumps({"timeline":[event]}), "json")


def test_duplicate_json_keys_and_language_variants_are_rejected():
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        decode('{"timeline":[],"timeline":[]}', "json")
    with pytest.raises(ValueError, match="Duplicate language and role"):
        decode(json.dumps({"timeline":[{"type":"dialogue","start":60000,"end":61000,
               "text":"你好","translations":[{"lang":"ZH-hans","text":"重复"}]}]}), "json")

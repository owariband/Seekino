"""Experimental shared JSON/XML contract with SHOT and no person lists.

This is deliberately not VideoParseResult: SHOT has no production counterpart.
"""
from __future__ import annotations

import json
import re
from typing import Annotated, Literal
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from evals.parse_output_protocol import _attributes, _json_object, _text


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Translation(StrictModel):
    lang: Text
    text: Text


class Original(Translation):
    lang: Text = "ja"


class EventBase(StrictModel):
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, gt=0)
    text: Text

    @model_validator(mode="after")
    def increasing_bounds(self):
        if self.end <= self.start:
            raise ValueError("Event end must be greater than start")
        return self


class Observation(EventBase):
    type: Literal["scene", "shot", "action", "emotion", "relation-change", "other"]


class ScreenText(EventBase):
    type: Literal["screen-text"]
    lang: Text | None = None


class Dialogue(EventBase):
    type: Literal["dialogue"]
    speaker: Text | None = None
    lang: Text = "zh-Hans"
    role: Literal["original", "translation"] = "translation"
    original: Original | None = None
    translations: list[Translation] = Field(default_factory=list)
    description: Text | None = None

    @model_validator(mode="after")
    def unique_variants(self):
        keys = [(self.lang.casefold(), self.role)]
        if self.original is not None:
            keys.append((self.original.lang.casefold(), "original"))
        keys.extend((v.lang.casefold(), "translation") for v in self.translations)
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate language and role")
        return self


class CompactTimeline(StrictModel):
    timeline: list[Annotated[Observation | ScreenText | Dialogue, Field(discriminator="type")]]


def decode_compact_result(raw, *, output_format, start_ms, end_ms):
    if output_format == "json":
        value = json.loads(raw, object_pairs_hook=_json_object)
    elif output_format == "xml":
        value = _xml_value(raw)
    else:
        raise ValueError("Unsupported output format")
    result = CompactTimeline.model_validate(value)
    for event in result.timeline:
        if event.start < start_ms or event.end > end_ms:
            raise ValueError("Event outside allowed chunk bounds")
    result.timeline.sort(key=lambda e: e.start)
    return result


def _xml_value(raw):
    if re.search(r"<!DOCTYPE|<!ENTITY", raw, re.IGNORECASE):
        raise ValueError("DTD and entity declarations are forbidden")
    root = ET.fromstring(raw)
    if root.tag != "timeline" or root.attrib or (root.text or "").strip():
        raise ValueError("Expected timeline containing only events")
    events = []
    for wrapper in root:
        if wrapper.tag != "event" or len(wrapper) != 1:
            raise ValueError("Each event must contain exactly one primary type")
        _attributes(wrapper, {"start", "end"})
        if (wrapper.text or "").strip() or (wrapper.tail or "").strip():
            raise ValueError("Unexpected event text")
        times = [wrapper.get(k, "") for k in ("start", "end")]
        if any(not re.fullmatch(r"0|[1-9][0-9]*", v) for v in times):
            raise ValueError("Times must be nonnegative integer milliseconds")
        node = wrapper[0]
        if (node.tail or "").strip():
            raise ValueError("Unexpected trailing text")
        allowed = {"speaker", "lang", "role"} if node.tag == "dialogue" else (
            {"lang"} if node.tag == "screen-text" else set())
        _attributes(node, allowed)
        event = {"start": int(times[0]), "end": int(times[1]), "type": node.tag,
                 "text": _text(node), **node.attrib}
        if node.tag != "dialogue" and len(node):
            raise ValueError("Non-dialogue types require plain text")
        for child in node:
            if len(child) or (child.tail or "").strip():
                raise ValueError("Supplemental fields require plain text")
            if child.tag == "description":
                _attributes(child, set())
                if "description" in event:
                    raise ValueError("Duplicate description")
                event["description"] = _text(child)
            elif child.tag in {"original", "translation"}:
                _attributes(child, {"lang"})
                item = {"text": _text(child), **child.attrib}
                if child.tag == "original":
                    if "original" in event:
                        raise ValueError("Duplicate original")
                    event["original"] = item
                else:
                    event.setdefault("translations", []).append(item)
            else:
                raise ValueError("Unknown dialogue field")
        events.append(event)
    return {"timeline": events}


COMMON = '''理解这段 [0,60000) 毫秒的视频，生成可供剧本及镜头检索使用的事件。图片是有序的 3 FPS 采样；每张前面有绝对毫秒标签。
ASR 是不完美的观察证据，可以与图片交叉核对；不是要求照抄的事件。只记录可确认的事实，不用背景编造内容。
本轮 OCR=null，未运行独立 OCR；请直接从图片读取字幕及画面文字，不得声称已有 OCR 结果。
保留完整对白及有证据的原文/译文，不把不同说话人的发言合并；有证据时填写 speaker，未知允许省略或 JSON null。画面人物不自动等于说话人。
取消逐事件 participants、visible/visible_persons 和根级 persons，任何事件都不输出这些名单。动作、关系与镜头描述中写清已确认的人物主体，避免脱离上下文的“她”“对方”。
类型：scene 写环境/场景变化；shot 专门写画面主体、景别、构图、角度、镜头运动；action 写人物动作；dialogue 写对白；emotion 写可观察情绪；relation-change 写关系变化；screen-text 写非对白画面文字；other 写其余必要事实。
切镜、特写、推拉等归 shot，不当作新场景或人物动作；有意义的镜头变化才记录，不逐帧或随每句台词重复。人物独立动作仍写 action。shot 与对白/动作允许时间重叠。
不要求八种类型全出现，也不能为满足类型而造事件。普通对白字幕归对白，不重复写成 screen-text。保持清楚可见的非对白文字。
时间都是原视频绝对整数毫秒，0<=start<end<=60000；跨边界发言只写可确认部分，程序可稳定排序。
描述用简体中文；台词保持证据中的完整原语言与有证据的译文。对白描述只补必要观察，不重复台词或枚举每帧嘴部开合；没有额外信息可省略。不猜测动机。
主台词默认 lang=zh-Hans、role=translation，original 默认 lang=ja；没有译文证据则主台词显式 lang=ja、role=original。补充译文必须注明 lang。同语言同角色不得重复。
只输出完整结果，不要 Markdown 围栏、解释、工具调用或省略号占位。'''

JSON_CONTRACT = '''只输出 {"timeline":[...]}。每项是一个事件，必填 start、end、type、text；type 使用上述八种小写类型名称。
text 为该类型正文：dialogue 只写台词，不重复说话人；其余类型写描述或实际文字。
只有 dialogue 可额外包含 speaker、lang、role、original、translations、description；可选字段无内容时省略。
original 是 {"text":"原文","lang":"ja"}，lang 可省略使用默认值；translations 是 [{"lang":"语言标签","text":"译文"}]；description 是额外观察字符串。
screen-text 可另有 lang，其他非对白类型没有额外字段。不输出旧领域字段或派生字段。
语法示例（虚构，非片段事实）：
{"timeline":[{"start":1000,"end":2000,"type":"shot","text":"甲的近景，固定镜头。"},{"start":1000,"end":2000,"type":"dialogue","speaker":"甲","text":"你好","original":{"text":"こんにちは"},"description":"甲挥手招呼。"},{"start":2000,"end":3000,"type":"action","text":"乙点头。"}]}'''

XML_CONTRACT = '''只输出一个 timeline 根节点，下方为 event 列表。每 event 必填带引号的 start/end，没有其他属性；每 event 只有一个主类型子节点，标签使用上述八种小写类型名称。
主类型正文对应 JSON 的 text：dialogue 只写台词，不重复说话人；其余类型写描述或实际文字。正文不能为空。
只有 dialogue 可有 speaker、lang、role 属性，以及可选 original、translation、description 子节点；无内容时省略。
original 可有 lang 属性，省略时用默认值；每个 translation 必须有 lang，可有多个；description 纯文本、至多一个。补充子节点之后只允许排版空白。
screen-text 可有 lang 属性，其他非对白类型无属性且只有纯文本。标准 XML 转义；无缩进、标签和正文分行。不输出旧领域字段或派生字段。
语法示例（虚构，非片段事实）：
<timeline>
<event start="1000" end="2000"><shot>甲的近景，固定镜头。</shot></event>
<event start="1000" end="2000"><dialogue speaker="甲">你好<original>こんにちは</original><description>甲挥手招呼。</description></dialogue></event>
<event start="2000" end="3000"><action>乙点头。</action></event>
</timeline>'''

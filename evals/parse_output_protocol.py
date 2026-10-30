"""Experimental event-wrapper XML decoder; production parsing is unchanged."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from xml.etree import ElementTree as ET

from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.validation import validate_result_time_range


EVENT_TYPES = {
    "scene": "SCENE", "dialogue": "DIALOGUE", "action": "ACTION",
    "emotion": "EMOTION", "relation-change": "RELATION_CHANGE",
    "screen-text": "ON_SCREEN_TEXT", "other": "OTHER",
}


@dataclass(frozen=True)
class LanguageDefaults:
    main_language: str
    main_role: str
    original_language: str

    def __post_init__(self):
        if not self.main_language.strip() or not self.original_language.strip():
            raise ValueError("Language defaults must be explicit")
        if self.main_role not in {"ORIGINAL", "TRANSLATION"}:
            raise ValueError("Invalid default text role")


def _attributes(node, allowed):
    if set(node.attrib) - set(allowed):
        raise ValueError(f"Unexpected attributes on {node.tag}")


def _text(node):
    # Only boundary whitespace is layout; internal whitespace remains evidence.
    value = (node.text or "").strip()
    if not value:
        raise ValueError(f"Missing text in {node.tag}")
    return value


def _names(value):
    if value is None:
        return []
    names = [name.strip() for name in value.split(",")]
    if any(not name for name in names):
        raise ValueError("Person lists cannot contain empty names")
    return list(dict.fromkeys(names))


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_result(raw, *, output_format, start_ms, end_ms, defaults):
    """Decode a complete response, without repairing syntax or semantic errors."""
    if output_format == "json":
        value = json.loads(raw, object_pairs_hook=_json_object)
        if not isinstance(value, dict) or not isinstance(value.get("timeline"), list):
            raise ValueError("Expected a VideoParseResult JSON object")
    elif output_format == "xml":
        value = _decode_xml(raw, defaults)
    else:
        raise ValueError("Unsupported output format")
    # The same host ordering rule applies to both experimental protocols.
    value["timeline"].sort(key=lambda event: event["start_seconds"])
    result = VideoParseResult.model_validate(value)
    return validate_result_time_range(result, start_seconds=start_ms, end_seconds=end_ms)


def _decode_xml(raw, defaults):
    if re.search(r"<!DOCTYPE|<!ENTITY", raw, re.IGNORECASE):
        raise ValueError("DTD and entity declarations are not part of this protocol")
    root = ET.fromstring(raw)
    if root.tag != "timeline" or root.attrib or (root.text or "").strip():
        raise ValueError("Expected a timeline root containing only events")
    persons, timeline = [], []
    for wrapper in root:
        if wrapper.tag != "event" or len(wrapper) != 1:
            raise ValueError("Each event must contain exactly one primary type")
        _attributes(wrapper, {"start", "end", "participants", "visible"})
        if (wrapper.text or "").strip() or (wrapper.tail or "").strip():
            raise ValueError("Unexpected text outside the primary event type")
        times = [wrapper.get(name, "") for name in ("start", "end")]
        if any(not re.fullmatch(r"0|[1-9][0-9]*", item) for item in times):
            raise ValueError("Times must be absolute nonnegative integer milliseconds")
        node = wrapper[0]
        if node.tag not in EVENT_TYPES or (node.tail or "").strip():
            raise ValueError("Unknown event type or unexpected trailing text")
        allowed = {"speaker", "lang", "role"} if node.tag == "dialogue" else (
            {"lang"} if node.tag == "screen-text" else set())
        _attributes(node, allowed)
        body = _text(node)
        event = {
            "event_type": EVENT_TYPES[node.tag], "start_seconds": int(times[0]),
            "end_seconds": int(times[1]), "description": body,
            "participants": _names(wrapper.get("participants")),
            "visible_persons": _names(wrapper.get("visible")),
        }
        if node.tag == "dialogue":
            speaker = node.get("speaker")
            if speaker is not None and not speaker.strip():
                raise ValueError("Omit speaker for unknown identity; do not supply a blank name")
            event.update(speaker=speaker, text_variants=[{
                "language": node.get("lang", defaults.main_language),
                "role": node.get("role", defaults.main_role.lower()).upper(), "text": body,
            }], description=f"{speaker or '未知说话人'}：{body}")
            description_seen = False
            for child in node:
                if len(child) or (child.tail or "").strip():
                    raise ValueError("Supplemental fields must be plain text")
                if child.tag == "description":
                    _attributes(child, set())
                    if description_seen:
                        raise ValueError("Duplicate description")
                    event["description"], description_seen = _text(child), True
                elif child.tag in {"original", "translation"}:
                    _attributes(child, {"lang"})
                    language = child.get("lang", defaults.original_language if child.tag == "original" else "")
                    if not language:
                        raise ValueError("Additional translations require lang")
                    event["text_variants"].append({
                        "language": language, "role": child.tag.upper(), "text": _text(child),
                    })
                else:
                    raise ValueError("Unknown dialogue field")
        elif len(node):
            raise ValueError("Non-dialogue event bodies must be plain text")
        if node.tag == "screen-text":
            event.update(language=node.get("lang"), text=body)
        persons.extend([*event["participants"], *event["visible_persons"]])
        if event.get("speaker") is not None:
            persons.append(event["speaker"])
        timeline.append(event)
    return {"persons": list(dict.fromkeys(persons)), "timeline": timeline}

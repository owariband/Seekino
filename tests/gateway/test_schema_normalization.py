from __future__ import annotations

from collections.abc import Mapping, Sequence

from engine.parsev2.schemas import VideoParseResult
from gateway.schema_normalization import inline_schema_refs


def _strings(value):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def test_real_parse_v2_union_has_no_dangling_definition_pointer():
    schema = inline_schema_refs(VideoParseResult.model_json_schema())

    assert "$defs" not in schema
    assert not any("#/$defs/" in value for value in _strings(schema))
    timeline = schema["properties"]["timeline"]["items"]
    branches = timeline["oneOf"]
    mapping = timeline["discriminator"]["mapping"]
    assert len(branches) == len(mapping) == 7
    for pointer in mapping.values():
        assert pointer.startswith("#/properties/timeline/items/oneOf/")
        assert int(pointer.rsplit("/", 1)[1]) < len(branches)

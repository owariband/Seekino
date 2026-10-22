from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import pytest

from server.model.identifiers import (
    derive_context_snapshot_id,
    derive_parse_job_id,
    derive_series_id,
    derive_video_chunk_id,
    derive_video_id,
)
from server.utils.validation import canonical_uuid


def test_video_chunk_id_keeps_the_published_uuid_seed() -> None:
    expected = uuid5(
        NAMESPACE_URL,
        "anivag:video-segment:00000000-0000-0000-0000-000000000001:0",
    )

    assert derive_video_chunk_id("00000000-0000-0000-0000-000000000001", 0) == str(expected)


@pytest.mark.parametrize(("derive_identifier", "arguments", "expected"), [
    (
        derive_series_id,
        (" workspace ", " Straße "),
        "72826211-4e85-5fed-b026-94e252ca6c54",
    ),
    (
        derive_video_id,
        ("00000000-0000-0000-0000-000000000001", "a" * 64),
        "aef9baea-7840-5131-a624-f4c359ab184e",
    ),
    (
        derive_video_chunk_id,
        ("00000000-0000-0000-0000-000000000001", 0),
        "25e3d328-e108-51ec-9620-f99a7ecdcfb8",
    ),
    (
        derive_context_snapshot_id,
        ("00000000-0000-0000-0000-000000000001", 0),
        "d3cbb50e-6cc4-5e1b-8647-1560d172f628",
    ),
    (
        derive_parse_job_id,
        ("00000000-0000-0000-0000-000000000001", " v1 "),
        "0dc48a79-cc04-588f-bd81-a03a10ad3fc4",
    ),
])
def test_entity_identifiers_keep_published_values(derive_identifier, arguments, expected) -> None:
    assert derive_identifier(*arguments) == expected


@pytest.mark.parametrize("value", [
    "ABCDEF01-2345-6789-ABCD-EF0123456789",
    "ABCDEF0123456789ABCDEF0123456789",
    "{ABCDEF01-2345-6789-ABCD-EF0123456789}",
])
def test_canonical_uuid_preserves_accepted_forms(value: str) -> None:
    assert canonical_uuid(value, "video_id") == "abcdef01-2345-6789-abcd-ef0123456789"


@pytest.mark.parametrize("value", ["not-a-uuid", None, 1])
def test_canonical_uuid_preserves_field_error(value) -> None:
    with pytest.raises(ValueError, match="^video_id must be a UUID$"):
        canonical_uuid(value, "video_id")

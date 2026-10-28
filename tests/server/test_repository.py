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


def test_video_chunk_id_uses_seekino_uuid_seed() -> None:
    expected = uuid5(
        NAMESPACE_URL,
        "seekino:video-segment:00000000-0000-0000-0000-000000000001:0",
    )

    assert derive_video_chunk_id("00000000-0000-0000-0000-000000000001", 0) == str(expected)


@pytest.mark.parametrize(("derive_identifier", "arguments", "expected"), [
    (
        derive_series_id,
        (" workspace ", " Straße "),
        "d6511b6d-6919-50f7-a93a-49569ab38f3a",
    ),
    (
        derive_video_id,
        ("00000000-0000-0000-0000-000000000001", "a" * 64),
        "d98b1b7b-af21-5cd6-b5af-a53baf282a64",
    ),
    (
        derive_video_chunk_id,
        ("00000000-0000-0000-0000-000000000001", 0),
        "7e4ca0f8-aa7e-5753-aa1b-bb7be1595d69",
    ),
    (
        derive_context_snapshot_id,
        ("00000000-0000-0000-0000-000000000001", 0),
        "8ea6d176-a79d-5909-96e2-32b09acd3c11",
    ),
    (
        derive_parse_job_id,
        ("00000000-0000-0000-0000-000000000001", " v1 "),
        "ab92f4ec-7fff-511e-b9c5-d242e196a0e8",
    ),
])
def test_entity_identifiers_match_seekino_namespace(derive_identifier, arguments, expected) -> None:
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

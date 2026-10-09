from __future__ import annotations

import json
import re

_BACKGROUND_IMAGE_REFERENCE = re.compile(r"asset://([^\s\\\"'`()<>\[\]{},;，。；：！？、]*)")
_BACKGROUND_IMAGE_ID = re.compile(r"[A-Za-z0-9_-]+")


def extract_background_image_ids(text: str) -> list[str]:
    image_ids: list[str] = []
    for reference in _BACKGROUND_IMAGE_REFERENCE.finditer(text):
        image_id = reference.group(1)
        if _BACKGROUND_IMAGE_ID.fullmatch(image_id) is None:
            raise ValueError(
                f"invalid background image reference at character {reference.start()}"
            )
        if image_id not in image_ids:
            image_ids.append(image_id)
    return image_ids


def prepare_background_text(
    character_bible: list[dict],
    important_extra_data: dict | None,
) -> str | None:
    sections: list[str] = []
    if important_extra_data is not None:
        document_format = important_extra_data.get("format")
        content = important_extra_data.get("content")
        if document_format in ("markdown", "json") and isinstance(content, str):
            if document_format == "json":
                content = json.dumps(
                    json.loads(content), ensure_ascii=False, indent=2, allow_nan=False,
                )
            if content.strip():
                sections.append(content)
            extra_fields = {
                key: value
                for key, value in important_extra_data.items()
                if key not in {"format", "content"}
            }
        else:
            legacy_text = important_extra_data.get("seriesBible")
            if isinstance(legacy_text, str):
                if legacy_text.strip():
                    sections.append(legacy_text)
                extra_fields = {
                    key: value
                    for key, value in important_extra_data.items()
                    if key != "seriesBible"
                }
            else:
                extra_fields = important_extra_data
        if extra_fields:
            sections.append(
                json.dumps(extra_fields, ensure_ascii=False, indent=2, allow_nan=False)
            )
    if character_bible:
        sections.append(
            json.dumps(
                {"characters": character_bible},
                ensure_ascii=False, indent=2, allow_nan=False,
            )
        )
    return "\n\n".join(sections) or None

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


def character_name_groups(character_bible: list[dict], document: dict | None) -> list[set[str]]:
    """Read explicit character names and aliases from optional series metadata."""
    characters = list(character_bible)
    content = (document or {}).get("content")
    if isinstance(content, str):
        if document.get("format") == "json":
            try:
                data = json.loads(content)
            except ValueError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("characters"), list):
                characters.extend(data["characters"])
        elif document.get("format") == "markdown":
            columns = None
            for line in content.splitlines():
                line = line.strip()
                if not line.startswith("|"):
                    columns = None
                    continue
                cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
                headers = [cell.casefold() for cell in cells]
                name_col = next((i for i, h in enumerate(headers) if h in ("name", "标准名", "角色名")), None)
                alias_col = next((i for i, h in enumerate(headers) if h in ("aliases", "别名", "常见查询别名")), None)
                if name_col is not None and alias_col is not None:
                    columns = name_col, alias_col
                elif columns and max(columns) < len(cells) and not re.fullmatch(r":?-+:?", cells[columns[0]]):
                    characters.append({"name": cells[columns[0]],
                                       "aliases": re.split(r"[,，、;；]", cells[columns[1]])})

    groups: dict[str, set[str]] = {}
    for person in characters:
        if not isinstance(person, dict) or not isinstance(person.get("name"), str):
            continue
        name = person["name"].strip()
        if not name or len(name) > 255:
            continue
        aliases = person.get("aliases")
        group = groups.setdefault(name, {name})
        if isinstance(aliases, list):
            group.update(a.strip() for a in aliases if isinstance(a, str) and 0 < len(a.strip()) <= 255)
    return list(groups.values())

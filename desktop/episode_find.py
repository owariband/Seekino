"""Search one published episode, using the same character dictionary as the Web player."""
import ctypes
from functools import cache
from pathlib import Path
import re
import sys
import unicodedata

_SCENES = {"SCENE", "ACTION", "EMOTION"}


@cache
def _dictionary():
    rows = (Path(__file__).parent / "data/opencc/TSCharacters.txt").read_text(encoding="utf-8").splitlines()
    return {ord(key): value for key, value in (line.split(" ", 1) for line in rows)}


def key(text):
    return "".join(text.casefold().split()).translate(_dictionary())


def _phrase(text):
    # Keep only the spaces that separate Latin words, as the Web visual index does.
    return key(re.sub(r"(?<=[a-zA-Z0-9])\s+(?=[a-zA-Z0-9])", "\x01", text)).replace("\x01", " ")


@cache
def _word_breaker():
    # Intl.Segmenter in the Web player uses an ICU word break iterator. Both
    # desktop platforms ship the C ABI. Apple and Windows use unversioned names.
    try:
        if sys.platform == "darwin":
            lib = ctypes.CDLL("/usr/lib/libicucore.A.dylib")
        elif sys.platform == "win32":
            lib = ctypes.CDLL("icu.dll", winmode=0x800)  # LOAD_LIBRARY_SEARCH_SYSTEM32
        else:
            return None
        pointer, integer = ctypes.c_void_p, ctypes.c_int32
        lib.ubrk_open.argtypes = [integer, ctypes.c_char_p, ctypes.c_char_p, integer, ctypes.POINTER(integer)]
        lib.ubrk_open.restype = pointer
        lib.ubrk_next.argtypes = lib.ubrk_getRuleStatus.argtypes = lib.ubrk_close.argtypes = [pointer]
        lib.ubrk_close.restype = None
        return lib
    except (OSError, AttributeError):
        return None


def _words(text):
    breaker = _word_breaker()
    if breaker:
        # ICU offsets are UTF-16 units, including two units for a supplementary character.
        data = text.encode("utf-16-le")
        error = ctypes.c_int32(0)
        iterator = breaker.ubrk_open(1, b"zh", data, len(data) // 2, ctypes.byref(error))
        if iterator:
            try:
                if error.value <= 0:
                    start = 0
                    while (end := breaker.ubrk_next(iterator)) >= 0:
                        if breaker.ubrk_getRuleStatus(iterator) >= 100:  # Number, letter, kana or ideograph.
                            yield data[2 * start:2 * end].decode("utf-16-le")
                        start = end
                    return
            finally:
                breaker.ubrk_close(iterator)
    yield from re.findall(r"\w+", re.sub(r"[的了着在和与跟]", " ", text))


def _match(cue, source, text):
    start, end = cue["start_ms"], cue["end_ms"]
    position = (start + end) // 2 if source in {"visual", "related"} else start
    return {"source": source, "text": text, "startMs": start, "endMs": end, "positionMs": position}


def _visual_terms(query, names):
    names_by_alias = {}
    for name in map(_phrase, names):
        if not name:
            continue
        aliases = {name}
        if 3 <= len(name) <= 5 and all(unicodedata.east_asian_width(c) == "W" for c in name):
            aliases.update((name[:2], name[-2:]))
        for alias in aliases:
            names_by_alias.setdefault(alias, set()).add(name)
    rest = _phrase(query)
    people = []
    for alias in sorted(names_by_alias, key=len, reverse=True):
        pattern = re.escape(alias)
        if re.fullmatch(r"[a-z0-9 ]+", alias):
            pattern = r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])"
        rest, count = re.subn(pattern, " ", rest)
        if count:
            people.append(names_by_alias[alias])
    terms = [term for term in _words(rest) if term not in {"的", "了", "在", "着", "和", "与", "跟", "and", "the", "a", "an", "with"}]
    return people, terms


def episode_matches(timeline, query, include_visual, related=(), expansions=()):
    needles = tuple(dict.fromkeys(filter(None, map(key, (query, *expansions)))))
    rows = []
    if not needles:
        return rows, False
    for cue in timeline["text"]["cues"]:
        variant = next((v for needle in needles for v in cue["texts"] if needle in key(v)), None)
        if variant is not None:
            rows.append(_match(cue, cue["source"], variant))
    if not include_visual:
        return rows, False
    visual = timeline["visual"]
    names = set(visual.get("known_persons", []))
    for cue in visual["cues"]:
        names.update(cue["visible_persons"])
    people, terms = _visual_terms(query, names)
    alternatives = [(people, terms)]
    if terms or not people:
        for term in expansions:
            expanded_people, expanded_terms = _visual_terms(term, names)
            if expanded_people or expanded_terms:
                alternatives.append((expanded_people, expanded_terms))

    def has_people(cue, required):
        present = set(map(_phrase, cue["visible_persons"]))
        return all(group & present for group in required)

    exact = []
    allow_related = False
    for cue in visual["cues"]:
        if not has_people(cue, people):
            continue
        is_scene = cue["kind"] in _SCENES
        description = _phrase(cue["description"]) if is_scene else ""
        allow_related |= bool(description and terms and not expansions)
        if not any(has_people(cue, required) and (
            (is_scene and words and all(word in description for word in words)) or (required and not words)
        ) for required, words in alternatives):
            continue
        exact.append(_match(cue, "visual", cue["description"] if description else "、".join(cue["visible_persons"])))
    confirmed = []
    for hit in sorted(exact, key=lambda row: (row["startMs"], -row["endMs"])):
        if not confirmed or hit["endMs"] > confirmed[-1]["endMs"]:
            confirmed.append(hit)
    suggestions = []
    for cue in related:
        if cue["kind"] not in _SCENES or not has_people(cue, people):
            continue
        if any(other["startMs"] < cue["end_ms"] and cue["start_ms"] < other["endMs"] for other in confirmed + suggestions):
            continue
        suggestions.append(_match(cue, "related", cue["description"]))
        if len(suggestions) == 5:
            break
    return sorted(rows + confirmed + suggestions, key=lambda row: row["positionMs"]), allow_related

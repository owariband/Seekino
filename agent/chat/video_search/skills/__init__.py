from pathlib import Path


_MEDIA_TIME_INSTRUCTIONS = (
    "媒体时间合同：Evidence、Catalog、Draft 中历史命名的 *_seconds / *Seconds "
    "媒体位置与时长字段均为整数毫秒。工具入参和播放范围必须保留原始毫秒值；"
    "claim 和最终正文显示秒时必须除以 1000，或转换为时间码，例如 12095 毫秒为 12.095 秒。"
    "精读工具 read 的 start/end 是字符偏移，与媒体时间无关。\n\n"
)


def _load_skill(name: str) -> str:
    path = Path(__file__).with_name(name) / "SKILL.md"
    return path.read_text(encoding="utf-8").strip()


LEXICAL_RETRIEVAL_SKILL_PROMPT = _load_skill("retrieval")
RETRIEVAL_SKILL_PROMPT = _load_skill("retrieval_transcript")
TRANSCRIPT_RETRIEVAL_SKILL_PROMPT = RETRIEVAL_SKILL_PROMPT
QUERY_SKILL_PROMPT = _MEDIA_TIME_INSTRUCTIONS + _load_skill("query")


__all__ = [
    "LEXICAL_RETRIEVAL_SKILL_PROMPT",
    "QUERY_SKILL_PROMPT",
    "RETRIEVAL_SKILL_PROMPT",
    "TRANSCRIPT_RETRIEVAL_SKILL_PROMPT",
]

from pathlib import Path


def _load_skill(name: str) -> str:
    path = Path(__file__).with_name(name) / "SKILL.md"
    return path.read_text(encoding="utf-8").strip()


RETRIEVAL_SKILL_PROMPT = _load_skill("retrieval")
QUERY_SKILL_PROMPT = _load_skill("query")
EVALUATOR_SKILL_PROMPT = _load_skill("evaluator")
CHAT_SKILL_PROMPT = _load_skill("chat")


__all__ = [
    "CHAT_SKILL_PROMPT",
    "EVALUATOR_SKILL_PROMPT",
    "QUERY_SKILL_PROMPT",
    "RETRIEVAL_SKILL_PROMPT",
]

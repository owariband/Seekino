from pathlib import Path


_RETRIEVAL_SKILL_PATH = Path(__file__).with_name("retrieval") / "SKILL.md"

# Query prompts end with one JSON block separated by a blank line. Removing
# blank Markdown lines keeps that boundary stable for every model invocation.
RETRIEVAL_SKILL_PROMPT = "\n".join(
    line
    for line in _RETRIEVAL_SKILL_PATH.read_text(encoding="utf-8").splitlines()
    if line.strip()
)


__all__ = ["RETRIEVAL_SKILL_PROMPT"]

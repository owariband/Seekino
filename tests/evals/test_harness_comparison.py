from collections import Counter
from types import SimpleNamespace

import pytest

from evals.benchmark.compare import schedule
from evals.benchmark.harnesses import validate_kimi


def test_schedule_covers_both_suites_without_breaking_followup_history():
    rows = schedule(2)
    assert len(rows) == 99
    assert Counter((r["suite"], r["arm"]) for r in rows) == {
        (suite, arm): size for suite, size in [("chat_behavior", 20), ("query_capabilities", 13)]
        for arm in ("deepagents", "pi", "codex")}
    assert len({(r["suite"], r["arm"], r["repeat"], r["case_id"]) for r in rows}) == len(rows)
    for arm in ("deepagents", "pi", "codex"):
        cases = [r["case_id"] for r in rows if r["suite"] == "query_capabilities" and r["arm"] == arm]
        assert cases.index("chat-T01") < cases.index("chat-T02")
    for offset in range(0, len(rows), 3):
        triple = rows[offset:offset+3]
        assert len({(r["suite"], r["case_id"], r["repeat"]) for r in triple}) == 1
        assert {r["arm"] for r in triple} == {"pi", "codex", "deepagents"}


@pytest.mark.parametrize("model,fallback", [("gpt-other", None), ("k3.1-pro", object())])
def test_comparison_rejects_non_kimi_generation_and_fallback(model, fallback):
    binding = SimpleNamespace(model=model, source="test", fallback=fallback)
    config = SimpleNamespace(normalized_models={"supervisor": binding}, supervisor_harness="deepagents",
                             source=lambda name: SimpleNamespace(provider="kimi"))
    with pytest.raises(ValueError, match="Kimi"):
        validate_kimi(config)

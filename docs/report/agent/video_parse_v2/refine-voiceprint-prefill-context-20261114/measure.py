"""Measure synthetic reference growth through the actual Refine prompt builders."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

from agent.video_parse_v2.prompts import build_refine_prompt
from agent.video_parse_v2.timelineEvent_canvas import ParseContextCanvas
from tests.agent.video_parse_v2.test_context_canvas import _voice_snapshot


baseline = "16d32ff4"
source = subprocess.run(
    ["git", "show", f"{baseline}:agent/video_parse_v2/prompts.py"],
    check=True, capture_output=True, text=True,
).stdout
namespace = {"__name__": "agent.video_parse_v2.prompts_baseline", "__package__": "agent.video_parse_v2"}
exec(compile(source, "prompts_baseline.py", "exec"), namespace)

rows = []
for mode in ("flag", "prefill", "off"):
    snapshot = _voice_snapshot(mode)
    expanded = deepcopy(snapshot)
    prepared = expanded["member_evidence"]["member"]["voiceprint"]
    prepared["samples"] = {"source-1": {"trace": "reference metadata " * 10_000}}
    prepared["lines"][0]["candidates"][0]["reference_ids"] = ["reference-id-" + "x" * 10_000]
    prepared["lines"][0]["candidates"][0]["src"] = [{"trace": "source details " * 10_000}]
    original = deepcopy(expanded)
    row = {"mode": mode}
    for label, builder in (("before", namespace["build_refine_prompt"]), ("after", build_refine_prompt)):
        small = builder(snapshot=snapshot, canvas=ParseContextCanvas.from_refine(snapshot))
        large = builder(snapshot=expanded, canvas=ParseContextCanvas.from_refine(expanded))
        row[f"{label}_small_chars"] = len(small)
        row[f"{label}_expanded_chars"] = len(large)
        row[f"{label}_reference_growth_chars"] = len(large) - len(small)
        if label == "after":
            data = json.loads(large.split("Input: ", 1)[1])
            member = data["members"][0]
            row["prefilled_speaker"] = member["content"]["timeline"][0]["speaker"]
            row["voice_summary"] = member["evidence"].get("voiceprint")
            assert large == small
    row["reduction_percent"] = round(100 * (1 - row["after_expanded_chars"] / row["before_expanded_chars"]), 4)
    row["original_host_evidence_unchanged"] = expanded == original
    assert row["original_host_evidence_unchanged"]
    rows.append(row)

result = {
    "baseline": baseline,
    "implementation_sha256": hashlib.sha256(Path("agent/video_parse_v2/prompts.py").read_bytes()).hexdigest(),
    "scope": "Synthetic one-member fixture, fixed scores; expanded sample/source metadata and reference IDs. No EP04 data or model requests.",
    "unit": "Python Unicode characters of complete build_refine_prompt output, not provider tokens or wrapped request size",
    "rows": rows,
}
Path(__file__).with_name("measurement.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
for row in rows:
    print({key: value for key, value in row.items() if key != "voice_summary"})

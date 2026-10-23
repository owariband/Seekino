"""Prepare a small text-only NPC experiment from a committed timeline export."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def _dialogue_segments(record: dict, aliases: set[str], skipped: Counter):
    """Complete each turn before selecting targets, so its prefix cannot leak into the prompt."""
    turns = []
    events = sorted(enumerate(record["timeline"]), key=lambda pair: (pair[1]["start_seconds"], pair[0]))
    for index, event in events:
        if event["event_type"] == "SCENE":
            if turns:
                yield turns
                turns = []
        if event["event_type"] != "DIALOGUE":
            continue
        speaker = event.get("speaker")
        variants = [v["text"].strip() for v in event["text_variants"]
                    if v["language"] == "zh-Hans" and v["text"].strip()]
        if not speaker or len(set(variants)) != 1:
            if turns:
                yield turns
                turns = []
            skipped["unknown_speaker_or_ambiguous_chinese"] += 1
            continue
        start, end = event["start_seconds"], event["end_seconds"]
        if turns and not 0 <= start - turns[-1]["end_ms"] <= 15000:
            yield turns
            turns = []
        original_index = record["source"]["timeline_indices"][index]
        if turns and (turns[-1]["speaker"] == speaker or {turns[-1]["speaker"], speaker} <= aliases):
            turns[-1]["text"] += "\n" + variants[0]
            turns[-1]["end_ms"] = end
            turns[-1]["timeline_indices"].append(original_index)
        else:
            turns.append({"speaker": speaker, "text": variants[0], "start_ms": start, "end_ms": end,
                          "timeline_index": original_index, "timeline_indices": [original_index]})
    if turns:
        yield turns


def prepare(source: Path, destination: Path, validation_episodes: set[int]) -> dict:
    summary = json.loads((source.parent / "summary.json").read_text())
    episodes = {row["video_id"]: row["episode_order"] for row in summary["episodes"]}
    aliases = {"千早愛音", "Anon"}
    samples = {"train": [], "validation": []}
    skipped = Counter()
    merged_fragments = 0
    for line in source.read_text().splitlines():
        record = json.loads(line)
        episode = episodes[record["source"]["video_id"]]
        for turns in _dialogue_segments(record, aliases, skipped):
            history = []
            for turn in turns:
                merged_fragments += len(turn["timeline_indices"]) - 1
                if turn["speaker"] not in aliases:
                    history.append(turn)
                    continue
                if history and any(turn["speaker"] not in aliases for turn in history):
                    context = history[-4:]
                    sample = {
                        "messages": [
                            {"role": "system", "content": "你扮演千早爱音，用中文接续对话，只输出爱音的下一句。"},
                            {"role": "user", "content": "\n".join(f'{t["speaker"]}：{t["text"]}' for t in context)},
                            {"role": "assistant", "content": turn["text"]},
                        ],
                        "source": {k: v for k, v in record["source"].items() if k != "timeline_indices"},
                        "episode": episode, "timeline_index": turn["timeline_index"],
                        "timeline_indices": turn["timeline_indices"],
                        "context": context, "start_ms": turn["start_ms"], "end_ms": turn["end_ms"],
                    }
                    split = "validation" if episode in validation_episodes else "train"
                    samples[split].append(sample)
                else:
                    skipped["target_without_local_context"] += len(turn["timeline_indices"])
                history.append(turn)
    if not all(samples.values()):
        raise ValueError("Both episode-separated splits must contain samples")
    destination.mkdir(parents=True, exist_ok=False)
    for split, rows in samples.items():
        (destination / f"{split}.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    report = {"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "counts": {split: len(rows) for split, rows in samples.items()},
              "merged_dialogue_fragments": merged_fragments,
              "skipped": dict(skipped), "validation_episodes": sorted(validation_episodes),
              "source_kind": "automatic_parse_results", "compact": False, "time_unit": "ms"}
    (destination / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--validation-episodes", nargs="+", type=int, default=[1, 2])
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.destination, set(args.validation_episodes)), ensure_ascii=False, indent=2))

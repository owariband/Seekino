"""Collect the completed Chat answer and its frame-tool evidence, without another model call."""
import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[3]
ARTIFACTS = PROJECT / "outputs/server/local-20261010/artifacts"


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def main():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    run = json.loads((ROOT / "run-01/run.json").read_text())
    assert run["status"] != "RUNNING", "Wait for the existing Chat; do not send a duplicate request."
    assistant = json.loads((ROOT / "run-01/assistant.json").read_text())
    cid, mid = run["conversation_id"], run["assistant_message_id"]
    trace = [line for line in (PROJECT / "outputs/server/logs/development/api.log").read_text().splitlines()
             if cid in line]
    (ROOT / "run-01/trace.log").write_text("\n".join(trace)+"\n")
    tool_calls = []
    for line in trace:
        if "event=agent_tool_finished " not in line:
            continue
        fields = {key: re.search(rf"\b{key}=([^ ]+)", line).group(1) for key in ["role", "tool", "status", "duration_ms"]}
        summary = json.loads(line.split(" result_summary=", 1)[1]) if " result_summary=" in line else {}
        tool_calls.append({**fields, "duration_ms": int(fields["duration_ms"]), "summary": summary})
    evidence_dir = ROOT / "evidence"
    (evidence_dir / "frames").mkdir(parents=True, exist_ok=True)
    (evidence_dir / "inspections").mkdir(exist_ok=True)
    inspections, frames = [], []
    series_root = ARTIFACTS / f"development/media/series/{manifest['series_id']}/videos"
    for path in sorted(series_root.glob("*/keyframes/inspection-*.json")):
        entry = json.loads(path.read_text())
        if entry.get("conversation_id") != cid or entry.get("message_id") != mid:
            continue
        shutil.copy2(path, evidence_dir / "inspections" / path.name)
        inspections.append({"path": str((evidence_dir / "inspections" / path.name).relative_to(ROOT)),
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "inspection_id": entry["inspection_id"],
                            "video_chunk_id": entry["video_chunk_id"], "role": entry["role"]})
        for frame in entry["frames"]:
            uri = frame["artifact_uri"]
            assert uri.startswith("artifact://")
            source = (ARTIFACTS / unquote(uri.removeprefix("artifact://"))).resolve()
            assert source.is_relative_to(ARTIFACTS.resolve())
            assert hashlib.sha256(source.read_bytes()).hexdigest() == frame["sha256"]
            target = evidence_dir / "frames" / source.name
            shutil.copy2(source, target)
            frames.append({"path": str(target.relative_to(ROOT)), "timestamp_ms": frame["requested_timestamp_ms"],
                "origin": "chat_inspected", "sha256": frame["sha256"], "inspection_id": entry["inspection_id"],
                "role": entry["role"], "video_id": entry["source_identity"]["video_id"],
                "video_chunk_id": entry["video_chunk_id"], "source_manifest": str(path.relative_to(PROJECT))})
    save(ROOT / "evidence/index.json", {"conversation_id": cid, "message_id": mid, "inspections": inspections,
        "frames": frames, "note": "Frames prepared by this Chat run's frame tool; timestamps are requested seek positions, not measured frame PTS. Provider attention to every frame cannot be inferred."})
    hits = {h["candidate"]["videoChunkId"]: h for h in (assistant.get("searchResult") or {}).get("hits", [])}
    candidates = []
    for block in assistant.get("renderBlocks") or []:
        if block.get("type") != "video":
            continue
        chunk_id = block["videoChunkId"]
        assert chunk_id in hits
        hit = hits[chunk_id]
        evidence = hit["evidence"]
        # The public legacy Seconds fields carry integer milliseconds; web/src/shared/format.ts documents this.
        start, end = block["startSeconds"], block["endSeconds"]
        assert isinstance(start, int) and isinstance(end, int) and end > start
        matching = [f for f in frames if f["video_id"] == evidence["videoId"] and start <= f["timestamp_ms"] < end]
        unique = {(f["timestamp_ms"], f["sha256"]): f for f in matching}
        ordered = [unique[key] for key in sorted(unique)]
        selected = [ordered[i] for i in sorted({0, len(ordered)//2, len(ordered)-1})] if ordered else []
        candidates.append({"id": chunk_id, "episode": evidence.get("episodeOrder"),
            "video_id": evidence["videoId"], "start_ms": start, "end_ms": end,
            "reason": hit.get("selectionReason") or "", "frames": selected,
            "all_matching_frame_count": len(ordered), "original_filename": evidence.get("originalFilename")})
    summary = {"tool_calls": tool_calls, "inspection_count": len(inspections), "frame_records": len(frames),
               "unique_frame_hashes": len({f["sha256"] for f in frames}), "delivered_video_cards": len(candidates)}
    save(ROOT / "run-01/observations.json", summary)
    comparison = {"title": "俯视房间：真实 Chat 与构图向量检索对照", "question": manifest["question"],
        "reference_path": "reference.jpg", "chat": {"status": assistant.get("status"),
            "model": assistant.get("modelName"), "elapsed_seconds": run["duration_ms"]/1000,
            "conversation_id": cid, "conversation_title": manifest["title"],
            "web_url": "http://127.0.0.1:5173/search", "answer": assistant.get("content") or "",
            "candidates": candidates}, "baselines": json.loads((ROOT / "baseline.json").read_text()),
        "observations": ["Chat 检索 MyGO 13 集已建库视频；小模型对照只检索 17 张选图中的 13 张人物候选，不能直接比较准确率。",
                         "画面来自本次 Chat 取帧工具；展示返回区间内按时间排列的首、中、末帧，最多 3 张。未添加新的检索候选。",
                         "目视复核：第一候选保留了斜俯拍、单个可见人物靠右下、右侧斜向沙发和左下大片地板的关系。人物比参考图更大，俯视较缓，墙面占比更高。",
                         "目视复核：第二候选复用同一休息室机位，三人集中在右侧。整体空间布局相近，但人物数量、整体占比和排列与参考图差异更大。",
                         "实际链路为 VLM 看参考图后生成文字查询，再检索文本/视觉索引并取原片帧核验；visual 通道的查询端仍是文字 embedding，没有直接编码这张参考图做近邻检索。",
                         "这一个例子说明现有 Chat 能召回具有明确构图关系的候选；不能据此证明其召回完整性或整体优于框图方法。下一步公平对照需让两种方法检索同一批候选帧。",
                         "保留 Chat 原始回答；页面中的说明与后续目视评议不是 Chat 的额外输出。"],
        "sources": [{"label": "原始 Chat 回答", "path": "run-01/answer.md"},
                    {"label": "完整助手消息", "path": "run-01/assistant.json"},
                    {"label": "原始 SSE", "path": "run-01/run.sse"},
                    {"label": "请求及运行记录", "path": "run-01/run.json"},
                    {"label": "取帧证据", "path": "evidence/index.json"},
                    {"label": "实验说明", "path": "README.md"}]}
    save(ROOT / "comparison.json", comparison)
    print(json.dumps({"chat_status": assistant.get("status"), **{k:v for k,v in summary.items() if k != "tool_calls"},
                      "candidate_frames": [len(c["frames"]) for c in candidates]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

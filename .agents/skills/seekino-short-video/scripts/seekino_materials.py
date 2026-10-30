#!/usr/bin/env python3
"""Prepare Seekino Agent-approved source clips for a HyperFrames project."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:9621")
    parser.add_argument("--workspace-id", default="local-default")
    parser.add_argument("--timeout", type=float, default=900)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list-series", help="List up to 100 READY series")
    list_parser.add_argument("--query")

    prepare = subparsers.add_parser("prepare", help="Retrieve and trim approved material")
    prepare.add_argument("--series-id", action="append", default=[])
    prepare.add_argument("--query", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--top-k", type=int)
    prepare.add_argument("--max-clips", type=int, default=8)
    prepare.add_argument(
        "--expected-agent-model",
        required=True,
        help="Fail unless assistantMessage.modelName exactly matches this value",
    )
    prepare.add_argument("--ffmpeg", default="ffmpeg")
    return parser.parse_args()

def request_json(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()

def list_series(client: httpx.Client, args: argparse.Namespace) -> None:
    params: dict[str, Any] = {"workspaceId": args.workspace_id, "status": "READY", "limit": 100}
    if args.query:
        params["query"] = args.query
    result = request_json(client, "GET", "/api/series/list", params=params)
    print(json.dumps(result, ensure_ascii=False, indent=2))

def caption_cues(evidence: dict[str, Any], start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    cues = []
    for cue in evidence.get("transcript") or []:
        cue_start = max(int(cue["startMs"]), start_ms)
        cue_end = min(int(cue["endMs"]), end_ms)
        if cue_start < cue_end:
            normalized = {key: value for key, value in cue.items() if key not in {"startMs", "endMs"}}
            normalized.update(startMs=cue_start - start_ms, endMs=cue_end - start_ms)
            cues.append(normalized)
    return cues

def trim_clip(
    ffmpeg: str, source_url: str, target: Path, start_ms: int, end_ms: int
) -> None:
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{start_ms / 1000:.3f}", "-i", source_url,
        "-t", f"{(end_ms - start_ms) / 1000:.3f}",
        "-map", "0:v:0", "-map", "0:a?", "-sn", "-dn",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(target),
    ]
    subprocess.run(command, check=True)


def prepare(client: httpx.Client, args: argparse.Namespace) -> None:
    ffmpeg = shutil.which(args.ffmpeg)
    if not ffmpeg:
        raise RuntimeError(f"FFmpeg executable not found: {args.ffmpeg}")
    if args.max_clips < 1:
        raise ValueError("--max-clips must be positive")
    option = request_json(client, "GET", "/api/agent/messages/options")["topK"]
    top_k = args.top_k if args.top_k is not None else int(option["defaultValue"])
    if not int(option["minimum"]) <= top_k <= int(option["maximum"]):
        raise ValueError(f"--top-k must be between {option['minimum']} and {option['maximum']}")

    output = args.output.resolve()
    clips_dir = output / "assets" / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    conversation_key = f"seekino-short-conv-{uuid.uuid4()}"
    conversation = request_json(
        client,
        "POST",
        "/api/agent/conversations/create",
        headers={"Idempotency-Key": conversation_key},
        json={"workspaceId": args.workspace_id, "title": args.query[:255]},
    )
    conversation_id = conversation["conversationId"]
    message_key = f"seekino-short-msg-{uuid.uuid4()}"
    prompt = (
        "请只使用所选 Seekino 本地系列的视频证据，不要搜索网页。请调用视频检索，"
        "围绕目标选择适合短片编排的有序片段，并给出简洁串联文案。"
        f"最多选择 {args.max_clips} 个片段。短片目标：{args.query}"
    )
    payload = {"workspaceId": args.workspace_id, "content": prompt,
               "seriesIds": args.series_id, "topK": top_k}
    result = request_json(
        client,
        "POST",
        "/api/agent/messages/send",
        params={"conversationId": conversation_id},
        headers={"Idempotency-Key": message_key, "Accept": "application/json"},
        json=payload,
    )
    answer = result["assistantMessage"]
    if answer.get("status") != "COMPLETED":
        raise RuntimeError(f"Seekino Agent failed: {answer.get('lastError') or answer.get('status')}")
    agent_model = answer.get("modelName")
    if agent_model != args.expected_agent_model:
        raise RuntimeError(
            "Seekino Agent model mismatch: "
            f"expected {args.expected_agent_model!r}, got {agent_model!r}"
        )
    search = answer.get("searchResult")
    blocks = answer.get("renderBlocks") or []
    if not search:
        raise RuntimeError("Seekino Agent completed without an approved search result")
    video_blocks = [block for block in blocks if block.get("type") == "video"]
    if not video_blocks:
        raise RuntimeError("Seekino Agent found no approved video material; refine the query")
    if len(video_blocks) > args.max_clips:
        raise RuntimeError("Seekino Agent returned more video blocks than --max-clips")

    hits = {
        hit["candidate"]["videoChunkId"]: hit
        for hit in search.get("hits") or []
        if hit.get("candidate", {}).get("videoChunkId")
    }
    prepared = []
    for index, block in enumerate(video_blocks, start=1):
        chunk_id = block["videoChunkId"]
        if chunk_id not in hits:
            raise RuntimeError(f"Approved block has no evidence hit: {chunk_id}")
        hit = hits[chunk_id]
        evidence = hit["evidence"]
        start_ms, end_ms = int(block["startMs"]), int(block["endMs"])
        clip_id = f"clip-{index:03d}"
        relative_path = Path("assets") / "clips" / f"{clip_id}.mp4"
        query = urlencode(
            {"workspaceId": args.workspace_id, "videoId": evidence["videoId"], "variant": "original"}
        )
        source_url = f"{str(client.base_url).rstrip('/')}/api/videos/content?{query}"
        trim_clip(ffmpeg, source_url, output / relative_path, start_ms, end_ms)
        prepared.append(
            {
                "id": clip_id,
                "path": relative_path.as_posix(),
                "durationMs": end_ms - start_ms,
                "sourceStartMs": start_ms,
                "sourceEndMs": end_ms,
                "videoChunkId": chunk_id,
                "videoId": evidence["videoId"],
                "seriesId": evidence.get("seriesId"),
                "seriesName": evidence.get("seriesName"),
                "episodeOrder": evidence.get("episodeOrder"),
                "originalFilename": evidence.get("originalFilename"),
                "selectionReason": hit.get("selectionReason"),
                "captions": caption_cues(evidence, start_ms, end_ms),
            }
        )

    bundle = {
        "schemaVersion": 1,
        "source": {
            "workspaceId": args.workspace_id,
            "seriesIds": search.get("seriesIds") or args.series_id,
            "query": args.query,
            "conversationId": conversation_id,
            "messageId": answer.get("messageId"),
            "agentModel": agent_model,
            "partial": bool(search.get("partial")),
            "warnings": search.get("warnings") or [],
        },
        "renderBlocks": blocks,
        "clips": prepared,
    }
    bundle_path = output / "material-bundle.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {"bundle": str(bundle_path), "clips": len(prepared), "agentModel": agent_model},
            ensure_ascii=False,
        )
    )


def main() -> int:
    args = parse_args()
    try:
        with httpx.Client(base_url=args.base_url, timeout=args.timeout, follow_redirects=True) as client:
            list_series(client, args) if args.command == "list-series" else prepare(client, args)
    except (httpx.HTTPError, KeyError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

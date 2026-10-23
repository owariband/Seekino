"""One offline VoiceprintAgent run; no Server, queue, database or production writes."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import asdict, fields
from datetime import UTC, datetime, timedelta
from hashlib import file_digest, sha256
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import RLock
from time import monotonic
import wave

PROJECT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT))

from agent.utils.errors import AgentToolFeedbackError
from agent.utils.tool import as_langchain_tools, build_agent_tool
from agent.utils.tool_result import JsonToolResult
from agent.utils.video_frames import build_inspect_video_frames_tool
from agent.video_parse_v2.input import GenerativeModelConfig, ParseInput
from agent.video_parse_v2.state import LeaseToken
from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion
from agent.video_parse_v2.voiceprint import VoiceprintAgent, build_tools
from engine.parsev2.schemas import VideoParseResult
from gateway.config import load_config
from gateway.model_factory import GatewayClient
from gateway.multimodal import TextInput
from gateway.usage import capture_model_usage
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parent
TARGET = 6
PUBLIC_FIELDS = ("source_ref", "sample_id", "group_id", "start_ms", "end_ms", "chunk_ref",
                 "sequence_no", "text", "weak_speaker", "weak_speakers", "selection", "model")
AUDIT = PROJECT / "engine/experiment/voiceprint-agent/20261017-mygo-ep04-visual-overlap-submit/voiceprint-source-audit-20261017T135330Z.json"


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=str) + "\n")
    temporary.replace(path)


def usage_record(usage):
    result = {f.name: getattr(usage, f.name) for f in fields(usage)
              if f.name not in {"context", "usage", "sdk_model_usage"}}
    return {**result, "context": dict(usage.context), "usage": asdict(usage.usage) if usage.usage else None,
            "sdk_model_usage": [asdict(item) for item in usage.sdk_model_usage]}


class PreparationIncomplete(RuntimeError):
    pass


class InspectArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_ref: str


class LocalRuntime:
    def __init__(self, data, output, *, gateway_factory=GatewayClient):
        self.data, self.output, self.gateway_factory = data, output, gateway_factory
        protocol_path = Path(data["protocol"]["path"])
        if sha256(protocol_path.read_bytes()).hexdigest() != data["protocol"]["sha256"]:
            raise ValueError("Frozen experiment protocol changed")
        budgets = json.loads(protocol_path.read_text())["agent_budget"]
        self.tool_budget, self.model_budget, self.timeout = (budgets["maximum_tool_calls"],
            budgets["maximum_model_calls"], budgets["timeout_seconds"])
        self.sources = {c["source_ref"]: c for c in data["candidates"]}
        if len(self.sources) != len(data["candidates"]):
            raise ValueError("duplicate issued source_ref")
        self.speakers = {s["speaker"]: s for s in data["speakers"]}
        if len(self.speakers) != len(data["speakers"]):
            raise ValueError("duplicate speaker names")
        self.started, self.calls, self.lock = monotonic(), 0, RLock()
        self.read_refs, self.inspected_refs, self.usage = set(), set(), []
        self.task_id = "offline-ep04-wavlm-k8"
        self.registry = {"ready": False, "experiment_task_id": self.task_id, "video_id": data["video"]["id"],
                         "model": data["model"], "dimension": 256, "references": [],
                         "limitations": ["Experimental candidate identities are not human-certified.",
                                         "Images contain no audio; the Agent cannot listen to these clips.",
                                         "No calibrated WavLM consistency threshold is applied."]}
        self.output.mkdir(parents=True, exist_ok=True)
        if any((output / name).exists() for name in ("registry.json", "trace.jsonl", "status.json", "result.json", "usage.json")):
            raise FileExistsError("Existing run outputs: use a new experiment directory; no automatic rerun/overwrite")
        for relative, expected in data["inputs"].items():
            with (PROJECT / relative).open("rb") as stream:
                if file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError(f"Frozen input changed: {relative}")
        with Path(data["source_video_path"]).open("rb") as stream:
            if file_digest(stream, "sha256").hexdigest() != data["video"]["sha256"]:
                raise ValueError("Actual source video differs from cached embedding provenance")
        self.registry["provenance"] = {"inputs": data["inputs"], "video_sha256": data["video"]["sha256"],
            "candidate_payload_sha256": sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
        for c in self.sources.values():
            vector = c["embedding"]
            if (len(vector) != 256 or not all(math.isfinite(x) for x in vector)
                    or abs(math.hypot(*vector) - 1) > 1e-5 or c["model"] != data["model"]
                    or not 0 <= c["start_ms"] < c["end_ms"]):
                raise ValueError("Invalid cached embedding, model or bounds")
            with wave.open(c["audio_path"], "rb") as audio:
                pcm = audio.readframes(audio.getnframes())
                if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
                    raise ValueError("Expected cached PCM16 mono 16kHz")
                if len(pcm) != (c["end_ms"] - c["start_ms"]) * 32 or sha256(pcm).hexdigest() != c["pcm_sha256"]:
                    raise ValueError("Cached PCM provenance mismatch")
        self.config = load_config()
        self.binding = self.config.resolve_model_binding("vision")
        original = json.loads(AUDIT.read_text())["task"]["parse_config"]
        values = self.binding.model_dump(exclude_none=True)
        values.pop("kind", None)
        original["models"]["voiceprint"] = GenerativeModelConfig.model_validate(values).model_dump(mode="json")
        original["models"]["speaker_embedding"] = {"source": "offline-vector-cache", "model": data["model"]["name"],
                                                   "request_timeout_seconds": float(self.timeout)}
        original["voiceprint"] = {"max_tool_calls": self.tool_budget, "execution_timeout_seconds": float(self.timeout)}
        original["execution"]["max_model_calls"] = self.model_budget
        self.inputs = ParseInput(series_id="offline-experiment", video_id=data["video"]["id"], parse_config=original)
        save(output / "registry.json", self.registry)

    def trace(self, event, **fields):
        with self.lock, (self.output / "trace.jsonl").open("a") as stream:
            stream.write(json.dumps({"time": datetime.now(UTC).isoformat(), "event": event, **fields},
                                    ensure_ascii=False, allow_nan=False, default=str) + "\n")

    def check(self):
        if monotonic() - self.started >= self.timeout:
            raise TimeoutError("Offline VoiceprintAgent total deadline exceeded")

    def count(self, _name):
        self.check()
        self.calls += 1
        if self.calls > self.tool_budget:
            raise RuntimeError("Offline VoiceprintAgent Tool budget exhausted")

    def public(self, candidate):
        return {key: candidate[key] for key in PUBLIC_FIELDS if key in candidate}

    def progress(self):
        refs = self.registry["references"]
        return {"speaker_list": [{**s, "refs_count": sum(r["speaker"] == name for r in refs),
                                  "target_refs_count": TARGET,
                                  "references": [{k: r[k] for k in ("source_ref", "start_ms", "end_ms")}
                                                 for r in refs if r["speaker"] == name]}
                                 for name, s in self.speakers.items()],
                "speaker_list_size": len(self.speakers), "target_refs_count": TARGET,
                "registered_speaker_count": len({r["speaker"] for r in refs}),
                "minimum_speaker_count": len(self.speakers) // 2 + 1 if self.speakers else 0}

    def reject(self, code, message):
        raise AgentToolFeedbackError(code, message, "Use an issued independent candidate; do not invent identities or audio.")

    def register(self, *, source_ref, speaker, start_ms, end_ms, selection_basis, confidence=None, replace_source_ref=None):
        if self.registry["ready"]:
            self.reject("ALREADY_READY", "Completed references are immutable")
        c = self.sources.get(source_ref)
        if c is None or speaker not in self.speakers or (start_ms, end_ms) != (c["start_ms"], c["end_ms"]):
            self.reject("INVALID_SOURCE", "Use an issued source, listed speaker and exact cached start_ms/end_ms")
        if source_ref not in self.read_refs or source_ref not in self.inspected_refs:
            self.reject("INSPECT_FIRST", "Read this source value and inspect_candidate_frames before registration")
        refs = self.registry["references"]
        old = next((r for r in refs if r["source_ref"] == replace_source_ref), None)
        if replace_source_ref is not None and (old is None or old["speaker"] != speaker):
            self.reject("INVALID_REPLACEMENT", "Replacement must identify an existing reference for this speaker")
        existing = next((r for r in refs if r["source_ref"] == source_ref), None)
        if existing is not None and existing is not old:
            if existing["speaker"] != speaker:
                self.reject("IDENTITY_CONFLICT", "This source is already registered under another speaker")
            return {"added": False, "reference": self.public(existing), "progress": self.progress()}
        for r in refs:
            if r is not old and (r["pcm_sha256"] == c["pcm_sha256"] or
                                max(start_ms, r["start_ms"]) < min(end_ms, r["end_ms"])):
                self.reject("OVERLAPPING_REFERENCE", "Audio is duplicate or overlaps a registered reference; choose another")
        record = {**deepcopy(c), "speaker": speaker, "selection_basis": selection_basis, "confidence": confidence}
        updated = {**self.registry, "references": [r for r in refs if r is not old] + [record]}
        save(self.output / "registry.json", updated)
        self.registry = updated
        return {"added": True, "reference": self.public(record), "dimension": 256, "progress": self.progress()}

    def unregister(self, *, source_ref):
        if self.registry["ready"] or source_ref not in self.sources:
            self.reject("INVALID_REMOVAL", "Use an issued reference before preparation finishes")
        refs = [r for r in self.registry["references"] if r["source_ref"] != source_ref]
        updated = {**self.registry, "references": refs}
        save(self.output / "registry.json", updated)
        removed = len(refs) < len(self.registry["references"])
        self.registry = updated
        return {"removed": removed, "progress": self.progress()}

    def finish(self, *, complete, skip_reasons):
        if self.registry["ready"]:
            return self.registry["completion"]
        if set(skip_reasons) - self.speakers.keys():
            self.reject("UNKNOWN_SPEAKER", "Skip reasons must use the fixed speaker list")
        for ref in self.registry["references"]:
            c = self.sources.get(ref["source_ref"])
            if (c is None or ref["speaker"] not in self.speakers or ref["embedding"] != c["embedding"]
                    or (ref["start_ms"], ref["end_ms"]) != (c["start_ms"], c["end_ms"])
                    or ref["source_ref"] not in self.read_refs or ref["source_ref"] not in self.inspected_refs):
                self.reject("INVALID_REGISTERED_SOURCE", "Registered source provenance/inspection is inconsistent")
        progress = self.progress()
        missing = [s["speaker"] for s in progress["speaker_list"]
                   if s["refs_count"] < TARGET and not skip_reasons.get(s["speaker"], "").strip()]
        ready = progress["registered_speaker_count"] >= progress["minimum_speaker_count"] and not missing
        result = {"ready": ready, **progress, "missing_reason_speakers": missing, "skip_reasons": skip_reasons,
                  "message": "Experimental coverage only; no calibrated acoustic consistency gate. "
                             "If evidence is exhausted, stop without inventing identities; not_ready is permitted."}
        updated = {**self.registry, "ready": ready, "completion": result}
        save(self.output / "registry.json", updated)
        self.registry = updated
        return result

    async def load_voiceprint_input(self, task_id, lease):
        members, sequences, versions = {}, {}, {}
        for c in self.sources.values():
            key = c["chunk_ref"]
            sequences[key], versions[key] = c["sequence_no"], c["provenance"]["content_version"]
            member = members.setdefault(key, {"persons": [], "timeline": []})
            if c["weak_speaker"] and c["weak_speaker"] not in member["persons"]:
                member["persons"].append(c["weak_speaker"])
            member["timeline"].append({
                "event_type": "DIALOGUE", "speaker": c["weak_speaker"], "start_seconds": c["start_ms"],
                "end_seconds": c["end_ms"], "description": "Selected ASR window; text/identity labels are weak evidence",
                "participants": [c["weak_speaker"]] if c["weak_speaker"] else [], "visible_persons": [],
                "text_variants": [{"language": "ja", "role": "ORIGINAL", "text": c["text"]}] if c["text"] else []})
        for member in members.values():
            member["timeline"].sort(key=lambda event: event["start_seconds"])
        return {"video_parse_task_id": task_id, "inputs": self.inputs, "member_sequence_nos": sequences,
                "member_versions": versions, "member_contents": {k: VideoParseResult.model_validate(v) for k, v in members.items()},
                "sources": {key: self.public(c) for key, c in self.sources.items()},
                "background": {"candidate_groups": self.data["groups"], "notes": self.data["background"]}}

    def inspect(self, *, source_ref):
        c = self.sources.get(source_ref)
        if c is None:
            self.reject("INVALID_SOURCE", "Unknown source_ref")
        tool = build_inspect_video_frames_tool(source_path=Path(self.data["source_video_path"]),
            read_bounds=(c["start_ms"], c["end_ms"]), max_height=576, timeout_seconds=min(30., self.timeout-(monotonic()-self.started)),
            check_control=self.check, trace_context={"experiment_task_id": self.task_id, "source_ref": source_ref})
        result = tool.execute(start_ms=c["start_ms"], end_ms=c["end_ms"], frame_count=3)
        if result.success and result.artifact and result.artifact.images:
            self.inspected_refs.add(source_ref)
        return result

    async def generate_voiceprint_preparation(self, snapshot, *, prompt, canvas):
        tools = build_tools(canvas, get_speaker_list=self.progress, register_voiceprint=self.register,
                            unregister_voiceprint=self.unregister, finish_voiceprint_preparation=self.finish,
                            check_control=self.check, on_call=self.count)
        tools.append(build_agent_tool(name="inspect_candidate_frames", args_schema=InspectArguments,
            description="View 3 actual frames only within this issued candidate's fixed range; images contain no audio.",
            func=lambda **kw: (self.count("inspect_candidate_frames"), self.inspect(**kw))[1], maximum_result_bytes=16000))
        def logged(tool):
            def invoke(**values):
                with self.lock:
                    self.trace("tool_started", tool=tool.name, arguments=values)
                    try:
                        result = tool.execute(**values)
                        if result.success and tool.name == "get_context_canvas":
                            for item in result.payload.get("items", [result.payload]):
                                if (item.get("collection") == "sources" and item.get("section") == "value"
                                        and not item.get("truncated", False) and item.get("start") == 0
                                        and item.get("end") == item.get("total_chars")
                                        and item.get("reference") in self.sources):
                                    self.read_refs.add(item["reference"])
                        self.trace("tool_finished", tool=tool.name, success=result.success, result=result.model_content())
                        return result
                    except BaseException as error:
                        self.trace("tool_failed", tool=tool.name, error_type=type(error).__name__, message=str(error))
                        raise
            return build_agent_tool(name=tool.name, args_schema=tool.args_schema, description=tool.description,
                                    func=invoke, maximum_result_bytes=32000)
        prompt += ("\nOFFLINE EXPERIMENT OVERRIDES: sources are anonymous algorithm-selected candidates. Their weak_speaker is "
            "not authoritative; assign only a name returned by get_speaker_list when supported. Never label a whole cluster "
            "from one example. This model cannot hear audio; images only help contextual identity evidence. Read each source "
            "value using get_context_canvas, then inspect_candidate_frames before registering that exact cached interval. "
            "No subrange embeddings are computed in this experiment. No WavLM consistency threshold is calibrated; the old "
            "prompt's consistency gate does not run here. Use concrete selection_basis and abstain on weak evidence. "
            "Batch source reads/tool calls when useful. Reserve calls for get_speaker_list and finish. Once candidates/evidence "
            "are exhausted, call finish with actual skip reasons and return complete=true even if coverage remains incomplete; "
            "the host records not_ready. Never invent identities to achieve strict majority. Do not use other file/shell tools.")
        gateway = self.gateway_factory(self.config)
        try:
            model = gateway.create_generative_model(self.binding, mcp=False)
            try:
                with capture_model_usage(lambda usage: self.usage.append(usage_record(usage))):
                    result = await asyncio.to_thread(model.generate, [TextInput(prompt)], schema_name="CanvasCompletion",
                        result_type=CanvasCompletion, tools=as_langchain_tools([logged(t) for t in tools]),
                        max_model_calls=self.model_budget, request_timeout_seconds=self.timeout,
                        check_control=self.check, context_checkpoint=canvas.checkpoint, tool_harness="deepagents")
                self.model_response = result.model_dump(mode="json")
                self.trace("model_completed", response=self.model_response)
                return result
            finally:
                model.close()
        finally:
            gateway.close()

    async def ensure_voiceprint_prepared(self, snapshot):
        self.check()
        if not json.loads((self.output / "registry.json").read_text())["ready"]:
            raise PreparationIncomplete("Agent returned; local references did not meet strict majority/reason requirements")

    async def notify_voiceprint_prepared(self, snapshot):
        self.trace("local_ready_observed", task_id=snapshot["video_parse_task_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate cached inputs and registry in a temporary directory; no model call")
    args = parser.parse_args()
    data = json.loads((ROOT / "candidates.json").read_text())
    if args.check:
        # A synthetic one-name list tests completion; it is never persisted as the real protocol or result.
        fixture = deepcopy(data)
        fixture["speakers"] = data["speakers"][:1]
        class FakeGateway:
            def __init__(self, config):
                pass
            def create_generative_model(self, binding, *, mcp):
                assert mcp is False
                return self
            def close(self):
                pass
            def generate(self, inputs, **options):
                tools = {t.name: t for t in options["tools"]}
                c, name = data["candidates"][0], fixture["speakers"][0]["speaker"]
                register = {k: c[k] for k in ("source_ref", "start_ms", "end_ms")}
                register.update(speaker=name, selection_basis="Synthetic self-check, not an identity claim")
                call = lambda tool, **kw: json.loads(tools[tool].invoke(kw))
                assert "error" in call("register_voiceprint", **register)
                call("get_context_canvas", collection="sources", references=[c["source_ref"]], section="value")
                assert "error" in call("register_voiceprint", **register)
                call("inspect_candidate_frames", source_ref=c["source_ref"])
                assert call("register_voiceprint", **register)["added"]
                assert not call("register_voiceprint", **register)["added"]
                invalid = {**register, "end_ms": c["end_ms"]-1}
                assert "error" in call("register_voiceprint", **invalid)
                assert call("finish_voiceprint_preparation", complete=True, skip_reasons={name: "Synthetic fixture"})["ready"]
                return CanvasCompletion(complete=True)
        with TemporaryDirectory(prefix="anivag-voiceprint-check-") as temporary:
            host = LocalRuntime(fixture, Path(temporary), gateway_factory=FakeGateway)
            assert all("embedding" not in host.public(c) and "audio_path" not in host.public(c) for c in data["candidates"])
            assert not host.finish(complete=True, skip_reasons={})["ready"]
            lease = LeaseToken(owner="self-check", version=1, expires_at=datetime.now(UTC)+timedelta(seconds=host.timeout))
            asyncio.run(VoiceprintAgent(runtime=host).InvokeVoiceprint(host.task_id, lease))
            print(json.dumps({"check": "passed", "candidate_count": len(host.sources), "speaker_count": len(host.speakers),
                              "model_called": False, "synthetic_fixture": True, "actual_frames_extracted": True}, ensure_ascii=False))
        return 0
    host = None
    try:
        host = LocalRuntime(data, ROOT)
        save(ROOT / "status.json", {"status": "running", "model": host.binding.model_dump(mode="json"), "mcp": False})
        print("Starting isolated VoiceprintAgent", flush=True)
        lease = LeaseToken(owner="offline-experiment", version=1, expires_at=datetime.now(UTC)+timedelta(seconds=host.timeout))
        asyncio.run(VoiceprintAgent(runtime=host).InvokeVoiceprint(host.task_id, lease))
        state, error, code = "complete", None, 0
    except PreparationIncomplete as exc:
        state, error, code = "coverage_incomplete", str(exc), 2
    except BaseException as exc:
        if host is None:
            print(f"Run not started: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        state, error, code = ("cancelled" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else
                              "timed_out" if isinstance(exc, TimeoutError) else "failed"), f"{type(exc).__name__}: {exc}", 1
    save(ROOT / "usage.json", host.usage)
    save(ROOT / "result.json", {"model_response": getattr(host, "model_response", None), "progress": host.progress(),
         "ready": host.registry["ready"], "tool_calls": host.calls, "error": error, "partial_references_are_diagnostic_only": not host.registry["ready"]})
    save(ROOT / "status.json", {"status": state, "elapsed_seconds": monotonic()-host.started, "error": error,
         "ready": host.registry["ready"], "model": host.binding.model_dump(mode="json"), "mcp": False})
    print(json.dumps({"status": state, "ready": host.registry["ready"], "tool_calls": host.calls, "error": error}, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the clip Understanding prompt on four original, deterministic 3-fps controls.

From the repository root: python -m evals.clip_camera_reference --output /tmp/camera-pilot
Uses the configured official DeepSeek source and its environment credential. No library media or
tenant context are loaded. At most four provider requests, no fallback, no database writes.
"""

import argparse
import json
import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

from agent.video_parse_v2.input import ParseInput
from agent.video_parse_v2.prompts import build_understanding_prompt
from agent.video_parse_v2.state import ChunkExecution, LeaseToken
from agent.video_parse_v2.understanding.response import ClipUnderstandingModelResponse
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.camera_evidence import camera_motion_hints, ground_camera_pans
from engine.parsev2.validation import validate_result_time_range
from gateway.agent_model.errors import ModelFailureError
from gateway.config import ModelBinding, load_config
from gateway.model_factory import GatewayClient
from gateway.multimodal import ImageInput, TextInput
from server.config.parse_profile import default_video_parse_v2_config


def panorama():
    image = Image.new("RGB", (792, 280), "#b4d5d9")
    d = ImageDraw.Draw(image)
    d.ellipse((381, 36, 423, 78), fill="#f7be7b")
    d.polygon(
        [
            (0, 133),
            (60, 107),
            (98, 119),
            (171, 90),
            (244, 127),
            (321, 93),
            (373, 110),
            (438, 87),
            (496, 121),
            (554, 103),
            (625, 121),
            (703, 89),
            (756, 110),
            (792, 98),
            (792, 182),
            (0, 182),
        ],
        fill="#739797",
    )
    d.rectangle((0, 156, 792, 280), fill="#447c8a")
    for y in range(165, 279, 13):
        for x in range((7 * y) % 53 - 20, 795, 66):
            d.line((x, y, x + 19, y - 1), fill="#84adb0", width=2)
    d.polygon(
        [(0, 155), (75, 144), (128, 150), (173, 164), (188, 184), (135, 180), (88, 194), (0, 199)],
        fill="#586362",
    )
    d.polygon([(73, 156), (82, 64), (103, 64), (116, 156)], fill="#fff0d1", outline="#243e44", width=2)
    d.polygon([(78, 112), (106, 106), (110, 125), (77, 133)], fill="#c74b39")
    d.rectangle((80, 52, 105, 66), fill="#edbc6b", outline="#2a4449", width=2)
    d.polygon([(75, 52), (92, 37), (110, 52)], fill="#344954")
    for x in (84, 91, 99):
        d.line((x, 53, x, 65), fill="#2a4449", width=2)
    for i, x in enumerate((210, 314, 448, 548, 645, 731)):
        y = 145 - (i % 3) * 4
        d.rectangle((x, y - 19, x + 29, y + 3), fill="#dfc79d", outline="#625d50")
        d.polygon([(x - 3, y - 18), (x + 10, y - 32), (x + 31, y - 20)], fill="#aa6047")
        d.rectangle((x + 8, y - 12, x + 13, y - 4), fill="#3e5d65")
    d.rectangle((525, 155, 790, 160), fill="#6a6758")
    for x in range(533, 792, 23):
        d.line((x, 159, x, 175), fill="#515b54", width=3)
    return image


def sample(background, case, i):
    u = i / 11
    progress = (1 - math.cos(math.pi * u)) / 2
    left = {
        "right": round(396 * progress),
        "left": round(396 * (1 - progress)),
        "subject": 0,
        "cut": 396 if i >= 6 else 0,
    }[case]
    image = background.copy()
    x = round(75 + 245 * u) if case == "subject" else 629
    d = ImageDraw.Draw(image)
    d.polygon([(x - 37, 216), (x + 35, 216), (x + 20, 230), (x - 23, 230)], fill="#b94f3f", outline="#313e44")
    d.rectangle((x - 11, 201, x + 19, 215), fill="#f7e2b7", outline="#405860")
    d.rectangle((x - 7, 204, x + 3, 210), fill="#496c73")
    d.line((x - 15, 215, x - 15, 177), fill="#384d52", width=2)
    d.polygon([(x - 14, 177), (x + 3, 180), (x - 14, 184)], fill="#c24d39")
    return image.crop((left, 0, left + 396, 280))


def run(config_path, output, cases=("right", "left", "subject", "cut")):
    output.mkdir(parents=True, exist_ok=True)
    config = load_config(config_path)
    if config.source("deepseek_api").base_url.rstrip("/") != "https://api.deepseek.com":
        raise ValueError("Expected the configured official DeepSeek endpoint")
    profile = default_video_parse_v2_config(config).model_copy(update={"content_kind": "clip"})
    inputs = ParseInput(series_id="synthetic", video_id="sample", parse_config=profile)
    lease = LeaseToken("pilot", 1, datetime.now(UTC) + timedelta(minutes=5))
    execution = ChunkExecution("sample", "sample", 0, 0, 4000, 0, 4000, inputs, lease)
    binding = ModelBinding(
        source="deepseek_api",
        model="deepseek-flash",
        kind="generative",
        supports_vision_input=True,
        reasoning_effort="none",
        request_timeout_seconds=90,
        max_output_tokens=1800,
    )
    client = GatewayClient(config, allow_local_endpoints=False)
    background = panorama()
    failures = []
    try:
        for case in cases:
            media = []
            frames = []
            for i in range(12):
                path = output / f"{case}-{i:02}.jpg"
                sample(background, case, i).save(path, quality=90)
                at = round(i * 1000 / 3)
                frames.append((at, path))
                media.extend((TextInput(f"timestamp_ms={at}"), ImageInput(path)))
            hints = camera_motion_hints(frames)
            prompt = build_understanding_prompt(
                execution, asr_result={"transcript": []}, ocr_result=None, camera_hints=hints
            )
            media.append(TextInput(prompt))
            started = time.monotonic()
            model = client.create_generative_model(binding)
            try:
                response = model.generate(
                    media,
                    schema_name="video_parse_v2_chunk",
                    result_type=ClipUnderstandingModelResponse,
                    max_model_calls=1,
                    request_timeout_seconds=90,
                )
            except ModelFailureError as error:
                failures.append(case)
                (output / f"{case}-error.json").write_text(
                    json.dumps({"case": case, "error": type(error).__name__}) + "\n"
                )
                print(case, type(error).__name__, flush=True)
                continue
            finally:
                model.close()
            result = validate_result_time_range(
                VideoParseResult.model_validate(response.root), start_seconds=0, end_seconds=4000
            )
            row = {
                "case": case,
                "model": binding.model,
                "seconds": round(time.monotonic() - started, 2),
                "camera_hints": hints,
                "raw_result": result.model_dump(mode="json"),
                "result": ground_camera_pans(result, hints).model_dump(mode="json"),
            }
            (output / f"{case}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n")
            print(
                case,
                row["seconds"],
                [event.get("camera_ref", {}).get("movement") for event in row["result"]["timeline"]],
                flush=True,
            )
    finally:
        client.close()
    if failures:
        raise RuntimeError(f"No model result for {failures}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", dest="cases", action="append", choices=("right", "left", "subject", "cut"))
    args = parser.parse_args()
    run(args.config, args.output, args.cases or ("right", "left", "subject", "cut"))

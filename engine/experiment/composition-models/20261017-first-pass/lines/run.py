"""OpenCV LSD geometry baseline; no learned weights or composition semantics."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import time

import cv2
import numpy as np


PARAMETERS = {
    "refine": "LSD_REFINE_STD",
    "scale": 0.8,
    "sigma_scale": 0.6,
    "quant": 2.0,
    "ang_th": 22.5,
    "log_eps": 0.0,
    "density_th": 0.7,
    "n_bins": 1024,
    "min_length_diagonal_ratio": 0.15,
    "max_selected_segments": 12,
    "angle_histogram_bins": 12,
}


def main():
    output = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, default=output.parent)
    args = parser.parse_args()
    root = args.experiment.resolve()
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    samples = manifest["samples"]
    assert samples and len({s["id"] for s in samples}) == len(samples)
    assert all(Path(s["id"]).name == s["id"] for s in samples)
    cv2.setNumThreads(1)
    detector = cv2.createLineSegmentDetector(
        cv2.LSD_REFINE_STD, PARAMETERS["scale"], PARAMETERS["sigma_scale"],
        PARAMETERS["quant"], PARAMETERS["ang_th"], PARAMETERS["log_eps"],
        PARAMETERS["density_th"], PARAMETERS["n_bins"],
    )
    records = []
    for sample in samples:
        image_path = (root / sample["image_path"]).resolve()
        assert image_path.is_relative_to(root), "Sample must be within experiment"
        image_bytes = image_path.read_bytes()
        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"Cannot decode image: {image_path}")
        height, width = image.shape[:2]
        diagonal = math.hypot(width, height)
        started = time.perf_counter()
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        detected, widths, precisions, _ = detector.detect(gray)
        elapsed_ms = (time.perf_counter() - started) * 1000
        segments = []
        if detected is not None:
            for index, points in enumerate(detected.reshape(-1, 4)):
                x1, y1, x2, y2 = map(float, points)
                length = math.hypot(x2 - x1, y2 - y1)
                angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180
                segments.append({
                    "detector_index": index,
                    "endpoints_xy_pixel": [[x1, y1], [x2, y2]],
                    "endpoints_xy_normalized": [[x1 / width, y1 / height],
                                                [x2 / width, y2 / height]],
                    "angle_undirected_degrees": angle,
                    "length_pixels": length,
                    "length_diagonal_ratio": length / diagonal,
                    "lsd_width_pixels": float(widths[index, 0]),
                    "lsd_precision": float(precisions[index, 0]),
                })
        eligible = [s for s in segments if s["length_diagonal_ratio"] >=
                    PARAMETERS["min_length_diagonal_ratio"]]
        selected = sorted(eligible, key=lambda s: (-s["length_pixels"], s["detector_index"]))[
            :PARAMETERS["max_selected_segments"]]
        histogram, _ = np.histogram(
            [s["angle_undirected_degrees"] for s in selected],
            bins=PARAMETERS["angle_histogram_bins"], range=(0, 180),
            weights=[s["length_pixels"] for s in selected],
        )
        histogram = histogram.astype(np.float64)
        norm = float(np.linalg.norm(histogram))
        vector = histogram / norm if norm else histogram
        overlay = image.copy()
        for rank, segment in enumerate(selected, 1):
            first, second = segment["endpoints_xy_pixel"]
            a, b = tuple(round(v) for v in first), tuple(round(v) for v in second)
            color = tuple(int(v) for v in cv2.cvtColor(
                np.uint8([[[round((rank - 1) * 179 / 12), 220, 255]]]), cv2.COLOR_HSV2BGR)[0, 0])
            cv2.line(overlay, a, b, color, 3, cv2.LINE_AA)
            cv2.putText(overlay, str(rank), a, cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(overlay, str(rank), a, cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, color, 1, cv2.LINE_AA)
        overlay_path = output / f"{sample['id']}.png"
        if not cv2.imwrite(str(overlay_path), overlay):
            raise OSError(f"Cannot write overlay: {overlay_path}")
        raw_path = output / f"{sample['id']}.raw.json"
        raw_path.write_text(json.dumps({"segments": segments}, indent=2) + "\n")
        records.append({
            "id": sample["id"], "image_path": sample["image_path"],
            "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
            "input_size_wh": [width, height], "detect_including_grayscale_ms": elapsed_ms,
            "detected_count": len(segments), "eligible_count": len(eligible),
            "selected_count": len(selected), "segments": selected,
            "histogram_length_weighted_pixels": histogram.tolist(),
            "embedding": vector.tolist(), "embedding_status": "ok" if norm else "zero_no_lines",
            "overlay_path": str(overlay_path.relative_to(root)),
            "raw_output_path": str(raw_path.relative_to(root)),
        })
        print(json.dumps({"id": sample["id"], "selected": len(selected),
                          "detect_ms": round(elapsed_ms, 3)}), flush=True)
    results = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "method": "opencv_lsd_geometry_baseline", "learned_model": False,
        "download_bytes": 0, "parameters": PARAMETERS,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "versions": {"opencv": cv2.__version__, "numpy": np.__version__},
        "platform": platform.platform(), "device": "cpu", "threads": 1,
        "preprocessing": "Read shared manifest image unchanged; BGR to uint8 grayscale. No resize/crop.",
        "coordinates": "Top-left origin; x right, y down. Normalize x/width and y/height.",
        "embedding_definition": "12 bins [0,15),...,[165,180); undirected pixel-space angle; selected-segment length weights; L2 normalized. Zero vector when no selected lines.",
        "limitations": "Geometric line detector only. No learned composition categories, semantic main-line selection, subject boxes or layout position in vector. Parallel edges can count separately; bin boundary sensitivity remains.",
        "records": records,
    }
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()

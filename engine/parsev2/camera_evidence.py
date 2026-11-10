"""Conservative horizontal motion hints from ordered clip frames, using only Pillow.

Several separated image regions must agree on a translation. The result describes apparent
image motion; the Understanding model still checks for cuts, wipes and moving objects.
"""

from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from .camera import SampledPan, covering_pan, pan_timing
from .schemas import SceneEvent, TimelineEvent, VideoParseResult


def _horizontal_edges(path: Path) -> Image.Image:
    # Work at a fixed scale, suppress lighting changes and ignore wrapped edge pixels in matching.
    with Image.open(path) as source:
        image = source.convert("L").resize((160, 96), Image.Resampling.BOX)
    return ImageChops.difference(image, ImageChops.offset(image, 2, 0))


def _frame_shift(old_image: Image.Image, new_image: Image.Image) -> int | None:
    # Score equal-area crops. A quarter viewport between samples is the search limit.
    reference = new_image.crop((42, 2, 118, 94))
    losses = []
    for candidate in range(-40, 41):
        crop = old_image.crop((42 + candidate, 2, 118 + candidate, 94))
        loss = ImageStat.Stat(ImageChops.difference(reference, crop)).mean[0]
        losses.append((loss, candidate))
    minimum, shift = min(losses)
    competitor = min(loss for loss, s in losses if abs(s - shift) > 2)
    if competitor - minimum < max(0.12, 0.08 * minimum) or abs(shift) == 40:
        return None
    if shift == 0:
        return 0
    # Verify the shift in the image. Require improvements across separated
    # rows and columns; a moving boat in one corner should not move the viewport.
    left, right = max(2, 2 - shift), min(158, 158 - shift)
    supporting, contradictory = [], 0
    for row in range(4):
        for col in range(3):
            x0 = left + (right - left) * col // 3
            x1 = left + (right - left) * (col + 1) // 3
            box = (x0, row * 24, x1, (row + 1) * 24)
            observed = new_image.crop(box)
            zero = ImageStat.Stat(ImageChops.difference(observed, old_image.crop(box))).mean[0]
            shifted = old_image.crop((x0 + shift, box[1], x1 + shift, box[3]))
            residual = ImageStat.Stat(ImageChops.difference(observed, shifted)).mean[0]
            if zero > 0.65 and residual < min(8.0, 0.8 * zero):
                supporting.append((col, row))
            elif residual > 1.5 and residual > 1.6 * zero:
                contradictory += 1
    if (
        len(supporting) <= contradictory
        or len({p[0] for p in supporting}) < 2
        or len({p[1] for p in supporting}) < 2
    ):
        return None
    return shift


def _run_hint(run):
    moving = [i for i, (_, _, dx) in enumerate(run) if dx]
    if not moving:
        return None
    run = run[moving[0] : moving[-1] + 1]
    active = [dx for _, _, dx in run if dx]
    if len(active) < 3 or abs(sum(active)) < 8 or len(active) * 2 < len(run):
        return None
    samples = [(run[0][0], 0.0)]
    offset = 0
    for _, end, dx in run:
        offset += dx
        samples.append((end, offset / 160))
    return {
        "first_sample_ms": run[0][0],
        "last_sample_ms": run[-1][1],
        "if_background_is_fixed": "PAN_RIGHT" if sum(active) > 0 else "PAN_LEFT",
        "sampled_timing": pan_timing(samples),
        "travel_in_frame_widths": round(abs(offset) / 160, 3),
        "samples": samples,
    }


def camera_motion_hints(frames: list[tuple[int, Path]] | tuple[tuple[int, Path], ...]) -> list[SampledPan]:
    """Find unbroken runs and abstain on ambiguous matches, invalid images and isolated cuts."""
    hints, run = [], []
    previous = None
    previous_path = None
    direction = 0
    for at, path in frames:
        if path == previous_path:
            continue  # The sampler can map two requested times to one physical frame.
        previous_path = path
        try:
            current = _horizontal_edges(path)
        except (OSError, ValueError):
            current = None
        shift = None
        if previous is not None and current is not None:
            previous_at, previous_rows = previous
            if previous_at < at:
                shift = _frame_shift(previous_rows, current)
        paused = shift == 0 and len(run) >= 2 and run[-1][2] == run[-2][2] == 0
        if shift is None or direction * shift < 0 or paused:
            if hint := _run_hint(run):
                hints.append(hint)
            run, direction = [], 0
        if shift is not None:
            if shift:
                direction = shift
            run.append((previous[0], at, shift))
        previous = (at, current) if current is not None else None
    if hint := _run_hint(run):
        hints.append(hint)
    return hints


def _pan_evidence(run: SampledPan) -> str:
    drift = "向左" if run["if_background_is_fixed"] == "PAN_RIGHT" else "向右"
    return (
        f"在 {run['first_sample_ms']}–{run['last_sample_ms']} 毫秒，多处画面纹理共同{drift}移动，"
        f"累计约 {run['travel_in_frame_widths']:.2f} 个画幅。"
    )


def _join_pan_fragments(timeline: list[TimelineEvent], run: SampledPan) -> None:
    """Retain story beats while giving a continuous, corroborated pan one playback interval."""
    first, last = run["first_sample_ms"], run["last_sample_ms"]
    fragments = []
    for index, event in enumerate(timeline):
        if not isinstance(event, SceneEvent) or event.camera_ref is None:
            continue
        if event.start_seconds >= last or event.end_seconds <= first:
            continue
        if (event.camera_ref.movement != run["if_background_is_fixed"]
                or event.camera_ref.timing in {"STEPPED", "VARIABLE"}
                or event.start_seconds < first or event.end_seconds > last):
            return  # The model observed a different motion or shot across this run.
        fragments.append((index, event))
    if len(fragments) < 2:
        return
    fragments.sort(key=lambda item: item[1].start_seconds)
    opening, closing = fragments[0][1], fragments[-1][1]
    if (closing.end_seconds - opening.start_seconds < (last - first) / 2
            or any(a.end_seconds != b.start_seconds for (_, a), (_, b) in zip(fragments, fragments[1:]))):
        return
    camera = opening.camera_ref.model_copy(update={
        "end_framing": closing.camera_ref.end_framing,
        "timing": run["sampled_timing"],
        "visual_evidence": _pan_evidence(run),
    })
    whole = SceneEvent(
        start_seconds=first, end_seconds=last, event_type="SCENE",
        description=camera.describe(), camera_ref=camera,
        participants=list(dict.fromkeys(name for _, event in fragments for name in event.participants)),
        visible_persons=list(dict.fromkeys(name for _, event in fragments for name in event.visible_persons)),
    )
    for index, event in fragments:
        timeline[index] = event.model_copy(update={"camera_ref": None})
    # Original story events may overlap the motion. Preserve their relative order.
    insert_at = next((i for i, event in enumerate(timeline) if event.start_seconds >= first), len(timeline))
    timeline.insert(insert_at, whole)


def ground_camera_pans[T: VideoParseResult](result: T, hints: list[SampledPan]) -> T:
    """The model identifies a within-shot pan. Registered scenery determines its signed motion.

    Only reconcile a disagreement for one well-covered, corroborated pan; a cut or an actor
    crossing a locked shot cannot become a pan just because some pixels moved.
    """
    if not hints:
        return result
    timeline = list(result.timeline)
    for run in hints:
        _join_pan_fragments(timeline, run)
    for i, event in enumerate(timeline):
        if not isinstance(event, SceneEvent) or event.camera_ref is None:
            continue
        old = event.camera_ref
        if old.movement not in {"PAN_LEFT", "PAN_RIGHT"}:
            continue
        match = covering_pan(hints, event.start_seconds, event.end_seconds)
        if match is None:
            continue
        run = match.observation
        direction = run["if_background_is_fixed"]
        timing = run["sampled_timing"]
        if timing == "UNKNOWN" or old.timing in {"STEPPED", "VARIABLE"}:
            timing = old.timing
        if (direction, timing) == (old.movement, old.timing):
            continue
        camera = old.model_copy(update={"movement": direction, "timing": timing, "visual_evidence": _pan_evidence(run)})
        timeline[i] = event.model_copy(update={"camera_ref": camera, "description": camera.describe()})
    return result.model_copy(update={"timeline": timeline})

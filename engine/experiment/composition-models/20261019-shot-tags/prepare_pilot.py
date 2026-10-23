"""Select a reproducible composition-tagging pilot without relevance judgments."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
EXPERIMENTS = OUT.parent
DEFAULT_SEED = "mygo-shot-tags-pilot-v1-20261019"
BUCKETS = ("lt_1s", "1_to_4s", "gt_4_to_8s", "gt_8s")
REQUIRED_FRAME_IDS = ("mygo-ep01-s00152-f2", "mygo-ep07-s00056-f1")
EP10_INTERVAL = (930300, 935600)


def duration_bucket(row):
    duration = row["end_ms"] - row["start_ms"]
    return BUCKETS[0] if duration < 1000 else BUCKETS[1] if duration <= 4000 else BUCKETS[2] if duration <= 8000 else BUCKETS[3]


def stratum(row):
    return row["episode"], duration_bucket(row), row["internal_motion"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gallery", type=Path)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--size", type=int, default=200)
    args = parser.parse_args()
    if args.gallery:
        gallery = args.gallery.resolve()
    else:
        candidates = sorted(EXPERIMENTS.glob("*-shot-gallery-v1.1"), reverse=True)
        gallery = next((path for path in candidates
                        if (path / "validation.json").is_file()
                        and json.loads((path / "validation.json").read_text()).get("status") == "passed"),
                       EXPERIMENTS / "20261018-shot-gallery")
    validation = json.loads((gallery / "validation.json").read_text())
    gallery_bytes = (gallery / "gallery.jsonl").read_bytes()
    gallery_sha = hashlib.sha256(gallery_bytes).hexdigest()
    assert validation["status"] == "passed", "Gallery validation must have passed"
    assert validation["gallery_sha256"] == gallery_sha, "Gallery bytes changed after validation"
    rows = [json.loads(line) for line in gallery_bytes.splitlines()]
    by_id = {row["frame_id"]: row for row in rows}
    assert len(by_id) == len(rows), "Duplicate frame IDs in gallery"
    representatives = [row for row in rows if row["is_representative"]]
    assert len({row["shot_id"] for row in representatives}) == len(representatives)
    assert {row["shot_id"] for row in representatives} == {row["shot_id"] for row in rows}
    eligible = [row for row in representatives if not row["is_op_ed"] and not row["low_information"]]
    eligible_ids = {row["frame_id"] for row in eligible}
    mandatory = [by_id[frame_id] for frame_id in REQUIRED_FRAME_IDS]
    ep10 = [row for row in representatives if row["episode"] == 10
            and row["start_ms"] < EP10_INTERVAL[1] and EP10_INTERVAL[0] < row["end_ms"]]
    assert ep10, "No representative overlaps the required Episode 10 interval"
    ep10.sort(key=lambda row: row["start_ms"])
    assert ep10[0]["start_ms"] <= EP10_INTERVAL[0] and ep10[-1]["end_ms"] >= EP10_INTERVAL[1]
    assert all(left["end_ms"] == right["start_ms"] for left, right in zip(ep10, ep10[1:]))
    mandatory.extend(ep10)
    mandatory_ids = {row["frame_id"] for row in mandatory}
    overrides = []
    for row in mandatory:
        assert not row["is_op_ed"] and not row["low_information"], "Anchor fails explicit quality exclusions"
        if row["frame_id"] not in eligible_ids:
            assert row["frame_id"] == REQUIRED_FRAME_IDS[0], "Only the explicit Episode 1 anchor may override representative selection"
            eligible = [item for item in eligible if item["shot_id"] != row["shot_id"]]
            eligible.append(row)
            overrides.append(dict(frame_id=row["frame_id"], reason="Explicit Episode 1 anchor overrides the gallery representative"))

    strata = defaultdict(list)
    for row in eligible:
        strata[stratum(row)].append(row)
    keys = sorted(strata, key=lambda key: (key[0], BUCKETS.index(key[1]), key[2]))

    def order_token(value):
        return hashlib.sha256((args.seed + "\n" + value).encode()).hexdigest()

    selected_by_stratum = {}
    for key in keys:
        selected_by_stratum[key] = [row for row in strata[key] if row["frame_id"] in mandatory_ids]
        if not selected_by_stratum[key]:
            selected_by_stratum[key] = [min(strata[key], key=lambda row: order_token(row["frame_id"]))]
    base_count = sum(map(len, selected_by_stratum.values()))
    remaining = args.size - base_count
    assert 0 <= remaining <= len(eligible) - base_count, "Pilot size must cover every populated stratum and fit eligible capacity"
    capacity = {key: len(strata[key]) - len(selected_by_stratum[key]) for key in keys}
    capacity_total = sum(capacity.values())
    # Hamilton allocation preserves the exact requested total without random rounding.
    extra = {key: remaining * capacity[key] // capacity_total if capacity_total else 0 for key in keys}
    remainder_order = sorted(keys, key=lambda key: (
        -(remaining * capacity[key] % capacity_total) if capacity_total else 0,
        order_token(json.dumps(key)),
    ))
    for key in remainder_order[:remaining - sum(extra.values())]:
        extra[key] += 1
    for key in keys:
        selected_ids = {row["frame_id"] for row in selected_by_stratum[key]}
        available = sorted((row for row in strata[key] if row["frame_id"] not in selected_ids),
                           key=lambda row: order_token(row["frame_id"]))
        selected_by_stratum[key].extend(available[:extra[key]])
    selected = sorted((row for group in selected_by_stratum.values() for row in group),
                      key=lambda row: (row["episode"], row["start_ms"], row["frame_id"]))
    assert len(selected) == args.size and len({row["frame_id"] for row in selected}) == args.size
    assert len({row["shot_id"] for row in selected}) == args.size
    assert mandatory_ids <= {row["frame_id"] for row in selected}
    output_rows = []
    for index, row in enumerate(selected):
        image_path = (gallery / row["frame_path"]).resolve()
        assert image_path.is_file(), image_path
        assert hashlib.sha256(image_path.read_bytes()).hexdigest() == row["sha256"], image_path
        output_rows.append(dict(row, image_path=str(image_path), pilot_index=index,
                                duration_bucket=duration_bucket(row), mandatory_anchor=row["frame_id"] in mandatory_ids))
    frame_bytes = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in output_rows).encode()
    sampling = dict(
        sampling_version="shot-tags-pilot-v1", seed=args.seed, requested_size=args.size,
        gallery_directory=str(gallery), gallery_version="v1.1" if gallery.name.endswith("-v1.1") else "v1",
        gallery_sha256=gallery_sha, gallery_validation_status=validation["status"],
        source_validation_file=str(gallery / "validation.json"),
        frames_sha256=hashlib.sha256(frame_bytes).hexdigest(),
        eligibility="is_representative and not is_op_ed and not low_information; explicit Episode 1 frame may replace its shot representative if needed",
        low_information_definition="Existing gallery flag: grayscale standard deviation <8 OR entropy <1.5 bits on a 160x90 thumbnail",
        duration_buckets={BUCKETS[0]: "duration_ms < 1000", BUCKETS[1]: "1000 <= duration_ms <= 4000",
                          BUCKETS[2]: "4000 < duration_ms <= 8000", BUCKETS[3]: "duration_ms > 8000"},
        motion_field="Existing internal_motion pixel-change proxy; not a dedicated camera-motion label",
        algorithm="Reserve all mandatory anchors. Guarantee at least one frame per nonempty episode x duration x internal_motion stratum. Allocate remaining places proportional to each stratum's remaining eligible capacity using Hamilton largest remainders. Order candidates and break allocation ties by SHA-256(seed + newline + frame_id or JSON stratum). Output sorted by episode, shot start, frame ID.",
        counts=dict(gallery_keyframes=len(rows), gallery_representatives=len(representatives),
                    op_ed_representatives=sum(row["is_op_ed"] for row in representatives),
                    low_information_representatives=sum(row["low_information"] for row in representatives),
                    excluded_by_both=sum(row["is_op_ed"] and row["low_information"] for row in representatives),
                    eligible=len(eligible), populated_strata=len(strata), selected=len(selected),
                    selected_unique_shots=len({row["shot_id"] for row in selected}),
                    selected_unique_sha256=len({row["sha256"] for row in selected})),
        representative_overrides=overrides,
        mandatory_anchors=[dict(frame_id=row["frame_id"], shot_id=row["shot_id"], episode=row["episode"],
                                start_ms=row["start_ms"], end_ms=row["end_ms"], frame_timestamp_ms=row["frame_timestamp_ms"],
                                is_representative=row["is_representative"], duration_bucket=duration_bucket(row),
                                internal_motion=row["internal_motion"]) for row in mandatory],
        anchor_notes=["Episode 1 mygo-ep01-s00152-f2 is already a representative in the selected v1 gallery; no override was required." if not overrides else "See representative_overrides.",
                      f"Episode 10 required interval [930300, 935600) overlaps {len(ep10)} detected shots; every overlapping shot's representative is mandatory. The gallery boundaries are preserved."],
        strata=[dict(episode=key[0], duration_bucket=key[1], internal_motion=key[2], eligible=len(strata[key]),
                     selected=len(selected_by_stratum[key]), mandatory=sum(row["frame_id"] in mandatory_ids for row in selected_by_stratum[key])) for key in keys],
        selected_per_episode=dict(sorted(Counter(row["episode"] for row in selected).items())),
        selected_per_duration=dict(Counter(duration_bucket(row) for row in selected)),
        selected_per_internal_motion={str(value).lower(): sum(row["internal_motion"] == value for row in selected) for value in (False, True)},
        validation=dict(exact_requested_count=True, unique_frame_ids=True, unique_shot_ids=True,
                        all_nonempty_strata_covered=True, all_mandatory_anchors_present=True,
                        no_op_ed_or_low_information=True, selected_image_sha256_verified=True),
        limitations=["Sampling uses existing gallery extraction/quality metadata, not composition or relevance labels.",
                     "Rare strata are deliberately overrepresented by the one-per-stratum floor; report pilot consistency as pilot statistics.",
                     "v1 requires the brief's adjacent near-duplicate collapse in later judging pools."],
    )
    destination = OUT / "pilot"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "frames.jsonl").write_bytes(frame_bytes)
    (destination / "sampling.json").write_text(json.dumps(sampling, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(dict(selected=len(selected), eligible=len(eligible), strata=len(strata),
                          mandatory=len(mandatory_ids), representative_overrides=len(overrides), frames_sha256=sampling["frames_sha256"])))


if __name__ == "__main__":
    main()

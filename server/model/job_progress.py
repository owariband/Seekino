from __future__ import annotations


def calculate_video_parse_progress(
    *, completed_parse_units: int, total_parse_units: int,
    indexed_chunks: int, total_chunks: int,
) -> float | None:
    if total_parse_units <= 0:
        return None
    if total_chunks == 0:
        return completed_parse_units * 100 / total_parse_units
    # Understanding and Refine retain equal unit weights within the first 95%.
    return (completed_parse_units * 95 / total_parse_units
            + indexed_chunks * 5 / total_chunks)

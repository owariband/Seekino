"""Stable host contracts shared by Parse acceptance and execution."""

PARSE_RETRIEVAL_CONTRACT = "parse-v2-retrieval-v1"
MAX_INDEX_GENERATION = (1 << 31) - 1
VIDEO_PARSE_CAPACITY_LOCK_NAME = "video-parse-capacity"
RUNTIME_DETAIL_KEY = "_worker_runtime_v2"
CAPACITY_ACQUIRED_KEY = "capacity_acquired"
PREPARE_FAILURES_KEY = "prepare_failures"
VOICEPRINT_REFS_PER_SPEAKER = 6
VOICEPRINT_DECISION_REASON_MAX_CHARS = 300
REFINE_PROVIDER_RETRY_DELAYS_SECONDS = (30.0, 90.0)
# Experimental preparation checks; these are cosine criteria, not identity probabilities.
VOICEPRINT_CONSISTENCY_MIN_REFS = 5
VOICEPRINT_CONSISTENCY_MAX_CANDIDATE_MEAN = 0.50

# Uncalibrated B0 decision policy from speaker-algo-20261018; cosine is not probability.
VOICEPRINT_B0_LEGACY_METHOD_VERSION = "B0-trimmed-centroid-v1"
VOICEPRINT_B0_COHERENCE_METHOD_VERSION = "B0-trimmed-centroid-v2-label-doubtful"
VOICEPRINT_B0_INCLUSIVE_TAIL_METHOD_VERSION = "B0-trimmed-centroid-v3-label-tail"
VOICEPRINT_B0_METHOD_VERSION = "B0-trimmed-centroid-v4-loo-tail"
VOICEPRINT_B0_DIMENSIONS = 192
VOICEPRINT_B0_MIN_AUDIO_MS = 400
VOICEPRINT_B0_TRIM_FRACTION = 0.20
# Null thresholds make gap descriptive; frozen numeric parameters retain legacy replay.
VOICEPRINT_B0_ACCEPT_GAP = None
# Integer millisecond durations: the inclusive minimum implements strictly >500 ms.
VOICEPRINT_B0_ACCEPT_DURATION_MS = 501
VOICEPRINT_B0_ACCEPT_MIN_CUES = 3
VOICEPRINT_B0_CONFLICT_GAP = None
VOICEPRINT_B0_NO_VOICE_DURATION_MS = VOICEPRINT_B0_ACCEPT_DURATION_MS
VOICEPRINT_B0_LABEL_MIN_RANK = 3
VOICEPRINT_B0_LABEL_MIN_MARGIN = 0.15
VOICEPRINT_B0_LABEL_MIN_DURATION_MS = 1000
VOICEPRINT_B0_LABEL_MIN_REF_COUNT = 3
# Frozen v2 policy; v3 uses the character's own kept-reference score distribution.
VOICEPRINT_B0_LABEL_MIN_CONSISTENCY = 0.70
VOICEPRINT_B0_LABEL_MIN_REF_HIGHER_FRACTION = 0.95
VOICEPRINT_B0_LABEL_TAIL_MIN_REFS = 8
VOICEPRINT_B0_WINDOW_MS = 1500
VOICEPRINT_B0_DIRECT_MAX_MS = 30_000

# EP04 review heuristic, reused unchanged from the 12-case offline experiment.
# Scores are contrasts, not calibrated speaker probabilities. No speech mask.
SPEAKER_CONSISTENCY_METHOD = "camplus-median-contrast-v1"
SPEAKER_CONSISTENCY_WINDOW_MS = 1000
SPEAKER_CONSISTENCY_HOP_MS = 400
SPEAKER_CONSISTENCY_PARAMETERS = {
    "side_windows": 6, "min_pairs": 3,
    "contrast_threshold": 0.10, "edge_threshold": 0.15,
}

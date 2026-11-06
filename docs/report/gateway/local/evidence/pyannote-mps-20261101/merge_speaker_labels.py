"""Produce a separate measured ASR enrichment; never mutate the committed input."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time
import warnings

BASE = Path(__file__).resolve().parent
SOURCE = BASE.parent / 'committed-asr.json'
DIAR = BASE / 'diarization-mps-full' / 'result.json'
TARGET = BASE / 'asr-with-speakers.json'
assert not TARGET.exists(), 'Refusing to replace an existing enrichment'
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['PYANNOTE_METRICS_ENABLED'] = '0'
os.environ['MPLCONFIGDIR'] = str(BASE / 'matplotlib')
warnings.filterwarnings('ignore', message='(?s).*torchcodec is not installed.*')

import pandas as pd
from whisperx.diarize import assign_word_speakers, IntervalTree

start = time.monotonic()
source_bytes = SOURCE.read_bytes()
original = json.loads(source_bytes)
diarization = json.loads(DIAR.read_text())
assert diarization['speaker_embeddings_finite'] is True
transcript = original['transcript']
# Normalized Seekino cue fields named *_seconds contain integer milliseconds.
temporary = {'segments': [
    {'start': cue['start_seconds'] / 1000,
     'end': cue['end_seconds'] / 1000, 'text': cue['text']}
    for cue in transcript
]}
segments = diarization['exclusive_segments']
assigned = assign_word_speakers(pd.DataFrame(segments), temporary, fill_nearest=False)
enriched = deepcopy(original)
tree = IntervalTree([(s['start'], s['end'], s['speaker']) for s in segments])
multiple_speakers = []
unassigned = []
for index, (source, cue, match) in enumerate(zip(transcript, enriched['transcript'], assigned['segments'], strict=True)):
    cue['speaker_id'] = match.get('speaker')
    if cue['speaker_id'] is None:
        unassigned.append(index)
    candidates = {label for label, _ in tree.query(match['start'], match['end'])}
    if len(candidates) > 1:
        multiple_speakers.append(index)
    assert {k: v for k, v in source.items() if k != 'speaker_id'} == {
        k: v for k, v in cue.items() if k != 'speaker_id'}
assert SOURCE.read_bytes() == source_bytes
TARGET.write_text(json.dumps(enriched, ensure_ascii=False, indent=2) + '\n')
summary = {
    'source_file_sha256': hashlib.sha256(source_bytes).hexdigest(),
    'diarization_file_sha256': hashlib.sha256(DIAR.read_bytes()).hexdigest(),
    'result_file_sha256': hashlib.sha256(TARGET.read_bytes()).hexdigest(),
    'source_task_id': '613f40e2-7bcb-4e38-9ae4-9ad1021afb51',
    'source_kind': 'separate export of committed whisper.cpp large-v3-turbo Q5_0 ASR',
    'speaker_model': diarization['model'], 'speaker_model_revision': diarization['revision'],
    'speaker_model_device': diarization['device'],
    'assignment': 'whisperx.assign_word_speakers; exclusive diarization; maximum temporal overlap',
    'fill_nearest': False, 'speaker_count_forced': False,
    'cue_count': len(transcript),
    'speaker_counts': dict(Counter(c['speaker_id'] for c in enriched['transcript'])),
    'unassigned_cue_count': len(unassigned), 'unassigned_cue_indexes': unassigned,
    'multiple_speaker_cue_count': len(multiple_speakers),
    'multiple_speaker_cue_indexes': multiple_speakers,
    'text_and_all_timestamps_unchanged': True,
    'original_file_unchanged': True, 'token_time_source': 'whisper.cpp; no acoustic forced alignment',
    'speaker_id_semantics': 'audio voice cluster, not a named person or face identification',
    'written_to_production_db': False, 'production_pipeline_integrated': False,
    'merge_seconds': time.monotonic() - start,
    'first_cue': {k: v for k, v in enriched['transcript'][0].items() if k != 'words'},
}
(BASE / 'speaker-merge-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(summary, ensure_ascii=False, indent=2))

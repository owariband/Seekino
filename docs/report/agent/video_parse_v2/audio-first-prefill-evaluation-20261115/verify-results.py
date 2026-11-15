"""Verify restored B0 wiring with cached EP04 answers, without any model request."""

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

from agent.video_parse_v2.prompts import build_refine_prompt, build_understanding_prompt
from agent.video_parse_v2.timelineEvent_canvas import ParseContextCanvas
from engine.parsev2.schemas import VideoParseResult
from server.service.video_parse import VideoParseRuntimeService


def read(path):
    return json.loads(path.read_text())


async def main(args):
    root = args.data_root.resolve()
    out = root / 'outputs/verification' / args.run_name
    audio = read(out / 'private/audio-first-artifact.json')
    assert audio['identity']['method_version'] == 'asr-id-b0-v1'
    original_path = root / 'outputs/verification/voiceprint-early-preparation-20261115/private/asr.json'
    original = read(original_path)
    assert hashlib.sha256(original_path.read_bytes()).hexdigest() == audio['identity']['asr_sha256']
    assert 'groups' not in audio and 'diarization' not in audio
    raw_labels = {cue['speaker_id'] for cue in original['transcript'] if cue.get('speaker_id')}
    assert set(audio['prefilled_asr']['speaker_ids']) == raw_labels
    queries = audio['queries']
    references = audio['references']
    assert len(queries) == len(original['transcript']) == 166
    assert references == [query for query in queries if query['label'] and 'vector' in query]
    for cue, updated, query, scored in zip(original['transcript'], audio['prefilled_asr']['transcript'], queries, audio['lines']):
        assert query['label'] == cue.get('speaker_id')
        assert not cue.get('speaker_id') or updated.get('speaker_id')
        assert {k:v for k,v in cue.items() if k!='speaker_id'} == {k:v for k,v in updated.items() if k!='speaker_id'}
        assert scored == VideoParseRuntimeService._score_voiceprint_sample(query, references)
        for candidate in scored['candidates']:
            assert query['id'] not in candidate['reference_ids']
    host = object.__new__(VideoParseRuntimeService)
    baseline = read(root / 'outputs/verification/ep04-offscreen-flash-low-20261114/private/voiceprint-artifact.json')
    samples = {sample['id']: sample for sample in baseline['samples']}
    for line in baseline['lines']:
        query = samples.get(line['source_id'], line)
        actual = host._score_voiceprint_sample(query, baseline['samples'])
        assert all(actual[key] == line[key] for key in ['candidates','gap','band','conflict','reason'])

    script = Path(__file__).with_name('live-ab.py')
    spec = importlib.util.spec_from_file_location('live_ab', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    previous = root / 'outputs/verification/audio-first-prefill-prompt-synced-20261115/private'
    directory = previous / 'B_two_thirds'
    manifest = read(previous/'manifest.json')
    rows = [read(path) for path in sorted((directory/'understanding').glob('*.json'))]
    call = read(directory/'calls/093a7b86-5043-480e-b7f2-c028ddb38597.input.json')
    background = json.loads(call['inputs'][0]['text'].split('Input: ',1)[1])['background']
    inputs = module.ParseInput(series_id=str(uuid4()),video_id=str(uuid4()),parse_config=manifest['parse_config'])
    options = dict(name='B_two_thirds',shared=root,inputs=inputs,background=background,
                   media=[],gateway=None,output=out/'private/replay',semaphores=(asyncio.Semaphore(1),asyncio.Semaphore(1)))
    obsolete = read(root/'outputs/verification/audio-first-prefill-evaluation-20261115/private/audio-first-artifact.json')
    try:
        module.Experiment(audio=obsolete,**options)
    except AssertionError as error:
        assert 'Reject superseded clustering input' in str(error)
    else:
        raise AssertionError('Old clustering artifact entered current host')
    case = module.Experiment(audio=audio,**options)
    case.refs = [row['chunk_task_id'] for row in rows]
    case.sequences = {ref:seq for seq,ref in enumerate(case.refs)}
    case.query_lock = asyncio.Lock()
    for row in rows:
        ref = row['chunk_task_id']
        case.initial[ref] = VideoParseResult.model_validate(row['content'])
        case.contents[ref] = case.initial[ref].model_copy(deep=True)
        case.versions[ref],case.scopes[ref] = 1,[row['sequence_no']]
        execution = await case.load_chunk_execution(ref)
        asr = await case.transcribe_chunk(execution)
        case.evidence[ref] = {**deepcopy(row['evidence']),'asr':asr}
        prompt = build_understanding_prompt(execution,asr_result=asr,ocr_result=None)
        assert 'VOICE_' not in prompt
        for cue in asr['transcript']:
            if cue['start_seconds']<49500 and cue['end_seconds']>47500:
                assert cue['speaker_id']
    retained = read(directory/'named-lines.json')
    case.frozen_anchors = [sample for sample in read(directory/'frozen-anchors.json')
                          if sample.get('label') and 'vector' in sample]
    case.named_lines = {ref:case.named_evidence(lines) for ref,lines in retained.items()}
    case.named_ready = True
    before = deepcopy(case.frozen_anchors)
    for ref, lines in retained.items():
        for query, actual in zip(lines,case.named_lines[ref]):
            assert actual == host._score_voiceprint_sample(query,case.frozen_anchors)
    assert case.frozen_anchors == before
    targets = {}
    for start, name, bounds in [(0,'opening_soyo',(48000,49800)),(4,'anon_introduction',(292500,295499))]:
        snapshot = await case.load_refine_input(f'{case.task_id}-{start}')
        canvas = ParseContextCanvas.from_refine(snapshot)
        prompt = build_refine_prompt(snapshot=snapshot,canvas=canvas)
        assert 'VOICE_' not in prompt
        matched = []
        for ref in snapshot['member_contents']:
            for line in case.named_lines[ref]:
                if line['start_ms']<bounds[1] and line['end_ms']>bounds[0]:
                    event = canvas.result(ref).timeline[line['position']]
                    matched.append({'start_ms':line['start_ms'],'end_ms':line['end_ms'],
                        'understanding_speaker':line['understanding_speaker'],
                        'band':line['band'],'gap':line['gap'],
                        'top1':line['candidates'][0]['speaker'] if line['candidates'] else None,
                        'canvas_speaker':event.speaker,'speaker_source':event.speaker_source})
        targets[name] = {'prompt_chars':len(prompt),'expressions':matched}
    proof = {
        'method_version':'asr-id-b0-v1','asr_cues_scored':len(queries),
        'script_sha256':{path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in
                         [Path(__file__),script,script.with_name('prepare-audio.py')]},
        'previous_invalid_prefill':{
            'before_non_null':sum(bool(cue.get('speaker_id')) for cue in original['transcript']),
            'after_non_null':sum(bool(cue.get('speaker_id')) for cue in obsolete['prefilled_asr']['transcript']),
            'non_null_to_null':sum(bool(before.get('speaker_id')) and not after.get('speaker_id')
                                  for before,after in zip(original['transcript'],obsolete['prefilled_asr']['transcript'])),
        },
        'reference_samples':len(references),'reference_ids':dict(Counter(row['label'] for row in references)),
        'null_id_reference_samples':0,'null_id_queries':sum(not row['label'] for row in queries),
        'non_null_to_null':0,'baseline_exact_reproduction_lines':len(baseline['lines']),
        'constructed_understanding_requests':len(rows),'replayed_expression_scores':sum(map(len,case.named_lines.values())),
        'named_reference_samples':len(case.frozen_anchors),'obsolete_clustering_artifact_rejected':True,
        'targets':targets,'generative_model_calls':0,'new_asr_calls':0,'gpu_access':False,
        'cpu_prod_access':False,'database_access':False,'source_asr_unchanged':True,
        'limits':['Cached Understanding came from the superseded input; this is wiring and B0 replay only.',
                  'No fresh Understanding or Refine result, identity-quality or end-to-end speed claim.'],
    }
    (out/'evidence/verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(proof,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-root',type=Path,required=True)
    parser.add_argument('--run-name',default='audio-first-prefill-asr-id-b0-20261115')
    asyncio.run(main(parser.parse_args()))

"""Run fresh EP04 Understanding and Refine with/without the 2/3 named pass.

Experimental host only: real Agent workflows and configured Gateway models,
local file commits, no Server/database/ASR/indexing calls.
"""

import argparse
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from uuid import uuid4
import wave

from agent.video_parse_v2.input import ParseConfig, ParseInput
from agent.video_parse_v2 import prompts as parse_prompts
from agent.video_parse_v2.refine.agent import IntervalRefine
from agent.video_parse_v2.refine.response import RefineModelResponse
from agent.video_parse_v2.state import ChunkExecution, LeaseToken
from agent.video_parse_v2.understanding.workflow import ChunkUnderstandingWorkflow
from engine.parsev2.schemas import VideoParseResult
from gateway.config import load_config
from gateway.local.campplus import CAMPPLUS_MODEL, CampPlusSpeakerEmbedder
from gateway.model_factory import GatewayClient
from gateway.multimodal import TextInput
from server.service.video_parse import VideoParseRuntimeService
from server.service.video_parse_media import MediaLeaseIdentity, PreparedVideoParseMedia, VideoParseMediaService
from server.service.video_parse_voiceprint import _voiceprint_event_fingerprint


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix('.tmp')
    pending.write_text(json.dumps(value, ensure_ascii=False, default=str, indent=2) + '\n')
    pending.chmod(0o600)
    pending.replace(path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Experiment:
    # Reuse the production visual request construction and repair transport.
    understand_chunk = VideoParseRuntimeService.understand_chunk
    repair_chunk_understanding = VideoParseRuntimeService.repair_chunk_understanding
    _generate_text_structured = VideoParseRuntimeService._generate_text_structured

    def __init__(self, *, name, shared, inputs, background, media, gateway, audio, output, semaphores):
        assert audio['identity'].get('method_version') == 'asr-id-b0-v1', 'Reject superseded clustering input'
        self.name, self.shared, self.inputs = name, shared, inputs
        self.background, self.media, self.gateway = background, media, gateway
        self.audio, self.output = audio, output
        self.under_sem, self.refine_sem = semaphores
        self.task_id = str(uuid4())
        self.refs = [str(uuid4()) for _ in range(24)]
        self.sequences = {ref: index for index, ref in enumerate(self.refs)}
        self.lease = LeaseToken('local-live-ab', 1, datetime.now(timezone.utc) + timedelta(hours=6))
        self.contents, self.initial, self.evidence, self.versions, self.scopes = {}, {}, {}, {}, {}
        self.commit_order, self.refine_jobs, self.refined, self.errors = [], {}, [], []
        self.refine_condition, self.active_members = asyncio.Condition(), set()
        self.named_ready, self.named_started = False, False
        self.named_lines, self.frozen_anchors = {}, []
        self.encoder = None
        self.calls = []
        self.started = time.monotonic()
        self.host = object.__new__(VideoParseRuntimeService)
        self.host._gateway = gateway
        self.prompt_source_sha256 = sha(Path(parse_prompts.__file__))
        self.output.mkdir(parents=True, exist_ok=True)

    def progress(self):
        save(self.output / 'progress.json', {
            'case': self.name, 'task_id': self.task_id,
            'recorded_utc': datetime.now(timezone.utc).isoformat(),
            'understanding_committed': len(self.initial), 'refine_complete': len(self.refined),
            'named_ready': self.named_ready, 'calls_started': len(self.calls),
            'calls_returned': sum(call.get('status') == 'returned' for call in self.calls),
            'errors': self.errors, 'elapsed_seconds': time.monotonic() - self.started,
        })

    async def load_chunk_execution(self, ref):
        seq = self.sequences[ref]
        start, end = seq * 60000, min((seq + 1) * 60000, 1420127)
        return ChunkExecution(ref, self.task_id, seq, start, end, start, end,
                              self.inputs, self.lease, self.background,
                              source_media_ref='local-ep04-existing-media', dialogue_bounds=(0, 1420127))

    async def transcribe_chunk(self, execution):
        result = deepcopy(self.audio['prefilled_asr'])
        result['transcript'] = [cue for cue in result['transcript']
            if cue['start_seconds'] < execution.allowed_end_seconds
            and execution.allowed_start_seconds < cue['end_seconds']]
        return result

    async def _ensure_chunk_media(self, execution):
        return self.media[execution.sequence_no]

    async def prepare_chunk_visual(self, execution):
        return None

    async def cleanup_chunk_execution(self, execution):
        pass

    async def _generate_multimodal(self, binding, model_inputs, *, schema_name, result_type,
                                   fallback_inputs=None, tools=(), **unused):
        assert not tools
        assert binding.model == 'deepseek-flash' and binding.reasoning_effort == 'low'
        assert binding.temperature == 0
        prompt = next(item.text for item in reversed(model_inputs) if isinstance(item, TextInput))
        required = (('Keep separate ASR cues and distinct utterances as separate DIALOGUE events',
                     'Cross-segment consolidation belongs to Refine')
                    if schema_name.startswith('video_parse_v2_chunk') else
                    ('Understanding preserves independent speech segments',
                     'Allow off-screen speech and voices heard before the speaker appears',
                     'Both ACCEPT and CONFLICT may prefill the top candidate'))
        assert all(rule in prompt for rule in required), 'Approved prompt contract is incomplete'
        assert sha(Path(parse_prompts.__file__)) == self.prompt_source_sha256
        call_id = str(uuid4())
        record = {'call_id': call_id, 'schema_name': schema_name,
                  'model': binding.model, 'reasoning_effort': binding.reasoning_effort,
                  'started_utc': datetime.now(timezone.utc).isoformat(), 'status': 'running',
                  'prompt_source_sha256': self.prompt_source_sha256,
                  'prompt_contract_verified': True,
                  'instruction_prefix_sha256': hashlib.sha256(prompt.split('Input: ', 1)[0].encode()).hexdigest()}
        self.calls.append(record)
        save(self.output / 'calls' / (call_id + '.input.json'), {
            **record, 'binding': binding.model_dump(mode='json'),
            'inputs': [{'type': type(item).__name__,
                        **({'text': item.text} if isinstance(item, TextInput) else
                           {'path': str(item.path), 'sha256': sha(item.path)})}
                       for item in model_inputs],
        })
        self.progress()
        started = time.monotonic()
        try:
            result = await asyncio.to_thread(self.host._invoke_multimodal,
                binding, model_inputs, schema_name=schema_name, result_type=result_type,
                fallback_inputs=fallback_inputs)
        except BaseException as error:
            record.update(status='error', error_type=type(error).__name__,
                          elapsed_seconds=time.monotonic() - started)
            save(self.output / 'calls' / (call_id + '.outcome.json'), record)
            raise
        record.update(status='returned', elapsed_seconds=time.monotonic() - started)
        save(self.output / 'calls' / (call_id + '.outcome.json'), {
            **record, 'result': result.model_dump(mode='json')})
        self.progress()
        return result

    async def commit_chunk_result(self, execution, result, *, evidence):
        ref = execution.chunk_parse_task_id
        assert ref not in self.initial
        if self.inputs.parse_config.voiceprint_mode != 'off':
            content = result.model_dump(mode='json')
            for event in content['timeline']:
                if event['event_type'] == 'DIALOGUE':
                    event['speaker_source'] = 'understanding'
            result = VideoParseResult.model_validate(content)
        self.initial[ref] = result.model_copy(deep=True)
        self.contents[ref] = result.model_copy(deep=True)
        self.evidence[ref], self.versions[ref], self.scopes[ref] = deepcopy(evidence), 1, [execution.sequence_no]
        self.commit_order.append(ref)
        save(self.output / 'understanding' / (f'{execution.sequence_no:02d}.json'), {
            'chunk_task_id': ref, 'sequence_no': execution.sequence_no,
            'commit_order': len(self.commit_order), 'committed_utc': datetime.now(timezone.utc).isoformat(),
            'content': result.model_dump(mode='json'), 'evidence': evidence,
        })
        # Freeze synchronously on the actual 16th commit, before any await.
        if self.name == 'B_two_thirds' and len(self.commit_order) == 16:
            self.named_started = True
            frozen = tuple(self.commit_order)
            save(self.output / 'frozen-sixteen.json', {
                'frozen_utc': datetime.now(timezone.utc).isoformat(), 'chunk_refs': frozen,
                'sequence_nos': [self.sequences[item] for item in frozen],
                'content_sha256': {item: hashlib.sha256(json.dumps(
                    self.initial[item].model_dump(mode='json'), sort_keys=True,
                    ensure_ascii=False).encode()).hexdigest() for item in frozen},
            })
            self.preparation_job = asyncio.create_task(self.prepare_named(frozen))
        self.progress()

    async def notify_chunk_committed(self, execution):
        self.schedule_refine()

    def collect_queries(self, refs):
        if self.encoder is None:
            self.encoder = CampPlusSpeakerEmbedder(CAMPPLUS_MODEL,
                checkpoint_path=self.shared / 'outputs/local-models/campplus/campplus_cn_common.bin',
                request_timeout_seconds=120, cpu_threads=8)
            with wave.open(self.audio['pcm_path'], 'rb') as stream:
                self.pcm = stream.readframes(stream.getnframes())
            outer = self

            class LocalPCM:
                @contextmanager
                def prepare_speaker_audio(self, identity, *, source_video, start_ms, end_ms, check_active):
                    path = outer.output / 'query.wav'
                    with wave.open(str(path), 'wb') as stream:
                        stream.setnchannels(1)
                        stream.setsampwidth(2)
                        stream.setframerate(16000)
                        stream.writeframes(outer.pcm[start_ms * 32:end_ms * 32])
                    try:
                        yield path
                    finally:
                        path.unlink(missing_ok=True)
                        path.with_name('b0-window.wav').unlink(missing_ok=True)

            self.host._media_service = LocalPCM()
            self.host._artifact_store = SimpleNamespace(resolve=lambda path: Path(path))
        sources = {}
        for ref in refs:
            for position, event in enumerate(self.initial[ref].timeline):
                value = event.model_dump(mode='json')
                if value['event_type'] != 'DIALOGUE':
                    continue
                fingerprint = _voiceprint_event_fingerprint(value)
                source_id = f'{ref}:{position}:{fingerprint}'
                sources[source_id] = {
                    'source_id': source_id, 'chunk_task_id': ref, 'chunk_id': ref,
                    'event_index': position, 'position': position, 'source_version': 1,
                    'event_fingerprint': fingerprint, 'speaker': event.speaker,
                    'start_ms': event.start_seconds, 'end_ms': event.end_seconds,
                }
        snapshot = {'sources': sources, 'member_evidence': {ref: self.evidence[ref] for ref in refs},
                    'member_contents': {ref: self.initial[ref] for ref in refs},
                    'failed_chunks': [], 'timing': {}}
        source = SimpleNamespace(duration_seconds=1420127, original_source_uri='local-source-pcm')
        lease = SimpleNamespace(video_parse_task_id=self.task_id, lease_version=1)
        preparation = self.host._collect_voiceprint_scores(snapshot, self.encoder, source, lease,
                                                           check_active=lambda: None)
        # This production collector retains too-short lines separately from embeddings.
        embeddings = {sample['id']: sample for sample in preparation['samples']}
        queries = [{**line, **({'vector': embeddings[line['id']]['vector']}
                    if line['id'] in embeddings else {})} for line in preparation['lines']]
        save(self.output / 'queries' / (str(len(self.initial)) + '-' + str(uuid4()) + '.json'), preparation)
        return queries

    async def prepare_named(self, frozen):
        timing = {'started_utc': datetime.now(timezone.utc).isoformat(),
                  'understanding_committed_at_start': len(self.initial), 'reference_chunk_count': len(frozen)}
        queries = await asyncio.to_thread(self.collect_queries, frozen)
        self.frozen_anchors = [query for query in queries if query.get('label') and 'vector' in query]
        save(self.output / 'frozen-anchors.json', self.frozen_anchors)
        self.named_ready = True
        save(self.output / 'named-timing.json', {**timing,
            'ready_utc': datetime.now(timezone.utc).isoformat(),
            'understanding_committed_at_ready': len(self.initial)})
        self.progress()
        self.schedule_refine()

    def named_evidence(self, queries):
        return [self.host._score_voiceprint_sample(query, self.frozen_anchors) for query in queries]

    def schedule_refine(self):
        if self.name == 'B_two_thirds' and not self.named_ready:
            return
        for start in range(22):
            if start not in self.refine_jobs and all(ref in self.initial for ref in self.refs[start:start + 3]):
                self.refine_jobs[start] = asyncio.create_task(self.run_refine(start))

    async def load_refine_input(self, interval_id):
        start = int(interval_id.rsplit('-', 1)[1])
        refs = self.refs[start:start + 3]
        evidence = {ref: deepcopy(self.evidence[ref]) for ref in refs}
        if self.name == 'B_two_thirds':
            # Exact current query boundaries are encoded after their real Understanding commit.
            # The reference labels remain frozen at the 16th commit.
            async with self.query_lock:
                missing = [ref for ref in refs if ref not in self.named_lines]
                if missing:
                    queries = await asyncio.to_thread(self.collect_queries, missing)
                    lines = await asyncio.to_thread(self.named_evidence, queries)
                    for ref in missing:
                        self.named_lines[ref] = [line for line in lines if line['chunk_task_id'] == ref]
                    save(self.output / 'named-lines.json', self.named_lines)
            for ref in refs:
                evidence[ref]['voiceprint'] = {'mode': 'prefill', 'method_version': 'B0-two-thirds-live-v1',
                                              'lines': self.named_lines[ref]}
        return {
            'interval_parse_task_id': interval_id, 'video_parse_task_id': self.task_id,
            'inputs': self.inputs, 'lease': self.lease, 'start_sequence_no': start, 'chunk_count': 3,
            'member_sequence_nos': {ref: self.sequences[ref] for ref in refs},
            'member_bounds': {ref: (min(self.scopes[ref]) * 60000,
                                   min((max(self.scopes[ref]) + 1) * 60000, 1420127)) for ref in refs},
            'member_contents': {ref: self.contents[ref].model_copy(deep=True) for ref in refs},
            'member_versions': {ref: self.versions[ref] for ref in refs},
            'member_scopes': {ref: list(self.scopes[ref]) for ref in refs},
            'member_evidence': evidence, 'background': self.background, 'dialogue_bounds': (0, 1420127),
            'write_bounds': (start * 60000, min((start + 3) * 60000, 1420127)),
        }

    async def generate_interval_refinement(self, snapshot, *, prompt, remaining_seconds):
        binding = self.inputs.parse_config.models.understanding
        binding = binding.model_copy(update={'request_timeout_seconds': min(binding.request_timeout_seconds, remaining_seconds)})
        return await self._generate_multimodal(binding, [TextInput(prompt)],
                                               schema_name='IntervalRefinementResult', result_type=RefineModelResponse)

    async def commit_interval_refinement(self, snapshot, result):
        for update in result.updates:
            ref = self.refs[update.sequence_no]
            assert self.versions[ref] == snapshot['member_versions'][ref]
            changes = {field: getattr(update, field) for field in update.changed_fields}
            if changes:
                self.contents[ref] = VideoParseResult.model_validate({
                    **self.contents[ref].model_dump(mode='json'), **changes})
                self.versions[ref] += 1
        consulted = {seq for scope in snapshot['member_scopes'].values() for seq in scope}
        for ref in snapshot['member_sequence_nos']:
            self.scopes[ref] = sorted(set(self.scopes[ref]).union(consulted))
        save(self.output / 'final-contents.json', {self.sequences[ref]: {
            'version': self.versions[ref], 'scope': self.scopes[ref],
            'content': content.model_dump(mode='json')} for ref, content in self.contents.items()})
        return True

    async def notify_interval_committed(self, snapshot):
        self.refined.append(snapshot['start_sequence_no'])
        save(self.output / 'intervals' / (f'{snapshot["start_sequence_no"]:02d}.json'), {
            'start_sequence_no': snapshot['start_sequence_no'],
            'committed_utc': datetime.now(timezone.utc).isoformat(),
            'input_versions': snapshot['member_versions'],
            'output_versions': {ref: self.versions[ref] for ref in snapshot['member_versions']},
        })
        self.progress()

    async def run_refine(self, start):
        refs = self.refs[start:start + 3]
        try:
            # Reserve the complete member set atomically; never hold a partial set
            # while waiting for adjacent intervals in the file-backed host.
            async with self.refine_condition:
                await self.refine_condition.wait_for(lambda: not self.active_members.intersection(refs))
                self.active_members.update(refs)
            try:
                async with self.refine_sem:
                    await IntervalRefine(runtime=self).InvokeRefine(f'{self.task_id}-{start}')
            finally:
                async with self.refine_condition:
                    self.active_members.difference_update(refs)
                    self.refine_condition.notify_all()
        except Exception as error:
            self.errors.append({'stage': 'refine', 'start_sequence_no': start,
                                'error_type': type(error).__name__, 'message': str(error)[:1200]})
            logging.exception('case=%s refine=%s failed', self.name, start)
            self.progress()

    async def run_understanding(self, ref):
        try:
            async with self.under_sem:
                await ChunkUnderstandingWorkflow(runtime=self).InvokeUnderstanding(ref)
        except Exception as error:
            self.errors.append({'stage': 'understanding', 'sequence_no': self.sequences[ref],
                                'error_type': type(error).__name__, 'message': str(error)[:1200]})
            logging.exception('case=%s chunk=%s failed', self.name, self.sequences[ref])
            self.progress()

    async def run(self):
        self.query_lock = asyncio.Lock()
        self.progress()
        await asyncio.gather(*(self.run_understanding(ref) for ref in self.refs))
        if self.named_started:
            await self.preparation_job
        self.schedule_refine()
        await asyncio.gather(*self.refine_jobs.values())
        if self.encoder is not None:
            self.encoder.close()
        assert len(self.initial) == 24 and len(self.refined) == 22 and not self.errors, self.errors
        save(self.output / 'complete.json', {'case': self.name, 'understanding': 24, 'refine': 22,
            'elapsed_seconds': time.monotonic() - self.started, 'asr_calls': 0,
            'gpu_calls': 0, 'database_calls': 0, 'calls': self.calls})


async def main(args):
    root = args.data_root.resolve()
    baseline = root / 'outputs/verification/ep04-offscreen-flash-low-20261114/private'
    assert Path(args.run_name).name == args.run_name and args.run_name.startswith('audio-first-prefill-')
    output = root / 'outputs/verification' / args.run_name
    private = output / 'private'
    private.mkdir(parents=True, exist_ok=True)
    assert not (private / 'manifest.json').exists(), 'Use existing run evidence; do not silently rerun live calls'
    audio_path = root / 'outputs/verification' / args.audio_run_name / 'private/audio-first-artifact.json'
    audio = json.loads(audio_path.read_text())
    assert audio['identity'].get('method_version') == 'asr-id-b0-v1', 'Reject superseded clustering input'
    source = next((root / 'assets/library/verification/ep04-prefill-context-20261114').rglob('original/*.mp4'))
    assert sha(source) == audio['identity']['media_sha256']
    asr_path = root / 'outputs/verification/voiceprint-early-preparation-20261115/private/asr.json'
    assert sha(asr_path) == audio['identity']['asr_sha256']
    config = ParseConfig.model_validate(json.loads((baseline / 'frozen-parse-config.json').read_text()))
    assert config.visual_input.fps == 1 and config.models.understanding.reasoning_effort == 'low'
    config_path = baseline / 'model-config.yaml'
    repo = Path(__file__).resolve().parents[5]
    guarded = ['agent/video_parse_v2', str(Path(__file__).resolve().relative_to(repo))]
    assert not subprocess.check_output(['git', 'diff', '--name-only', 'HEAD', '--', *guarded],
                                       cwd=repo, text=True).strip(), 'Commit the complete prompt/runtime baseline before live calls'
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    gateway = GatewayClient(load_config(config_path))
    original_call = json.loads(next((baseline / 'refine-calls').glob('*.input.json')).read_text())
    body, _ = json.JSONDecoder().raw_decode(original_call['prompt'].split('Input: ', 1)[1])
    background = body['background']
    inputs = ParseInput(series_id='d622007b-858b-5f18-ac72-a134d86f689e',
                        video_id='5df2f69f-6644-5a2d-ab37-776ece409aa4', parse_config=config)
    media_service = VideoParseMediaService(private / 'media')
    media_id = str(uuid4())
    save(private / 'manifest.json', {
        'started_utc': datetime.now(timezone.utc).isoformat(), 'script_sha256': sha(Path(__file__)),
        'source_revision': revision, 'prompt_source_sha256': sha(Path(parse_prompts.__file__)),
        'source_sha256': sha(source), 'asr_sha256': sha(asr_path), 'audio_artifact_sha256': sha(audio_path),
        'parse_config': config.model_dump(mode='json'), 'model_config_sha256': sha(config_path),
        'cases': args.cases, 'gpu_access': False, 'cpu_prod_access': False,
        'persistence': 'isolated local files; no Server task, DB or indexing',
        'understanding_source': 'fresh configured model calls, never old Understanding content',
    })
    context = media_service.prepare_episode(MediaLeaseIdentity(media_id, media_id, 1, 'video'),
        source_video=source, duration_ms=1420127, chunk_duration=60,
        visual_mode='ordered_frames', frame_fps=1, max_height=720, ocr_enabled=False,
        overlap_ms=3000, chunk_video_required=False, check_active=lambda: None)
    print('Preparing whole EP04 1fps shared visual input', flush=True)
    manifest = await asyncio.to_thread(context.__enter__)
    try:
        rows = json.loads(manifest.read_text())['chunks']
        media = [PreparedVideoParseMedia(None, manifest.parent / row['audio_path'],
            tuple((stamp, manifest.parent / path) for stamp, path in row['frames']),
            visual_start_ms=row['visual_start_ms'], visual_end_ms=row['visual_end_ms']) for row in rows]
        assert len(media) == 24
        save(private / 'frame-manifest.json', {'chunks': rows,
            'unique_frames': len({str(path) for item in media for _, path in item.frames})})
        semaphores = asyncio.Semaphore(10), asyncio.Semaphore(10)
        cases = [Experiment(name=name, shared=root, inputs=inputs, background=background,
                    media=media, gateway=gateway, audio=audio,
                    output=private / name, semaphores=semaphores)
                 for name in args.cases]
        print('Starting fresh Understanding/Refine for both whole-episode cases', flush=True)
        results = await asyncio.gather(*(case.run() for case in cases), return_exceptions=True)
        for case, result in zip(cases, results):
            print(case.name, 'FAILED ' + repr(result) if isinstance(result, BaseException) else 'COMPLETE', flush=True)
        assert not any(isinstance(result, BaseException) for result in results), results
        assert sha(asr_path) == audio['identity']['asr_sha256']
        save(output / 'evidence/complete.json', {'cases': {
            case.name: json.loads((case.output / 'complete.json').read_text()) for case in cases},
            'original_asr_unchanged': True})
    finally:
        await asyncio.to_thread(context.__exit__, None, None, None)
        gateway.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--run-name', default='audio-first-prefill-b0-live-ab-20261115')
    parser.add_argument('--audio-run-name', default='audio-first-prefill-asr-id-b0-20261115')
    parser.add_argument('--cases', nargs='+', choices=('A_audio_only', 'B_two_thirds'),
                        default=['A_audio_only', 'B_two_thirds'])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    asyncio.run(main(args))

"""Frozen single-chunk JSON/XML experiment; never writes tasks or changes services.

Run as a module, with --phase media, observations, or benchmark in that order.
Credentials are read from --env-file; only non-secret provenance is persisted.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time

import httpx
from openai import OpenAI

from engine.common.transcription import normalize_transcription
from engine.parsev2.schemas import VideoParseResult
from evals.parse_output_protocol import LanguageDefaults, decode_result
from evals import parse_compact_protocol as compact
from gateway.constants import ARK_OPENAI_BASE_URL
from gateway.media import encode_media_file


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def media(args):
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / 'frames').mkdir()
    start = time.monotonic()
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(args.video), '-t', '60',
                    '-vf', 'fps=3,scale=-2:720', '-q:v', '3',
                    str(args.out / 'frames/%04d.jpg')], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(args.video), '-t', '60',
                    '-vf', 'fps=1,scale=640:-2', '-c:v', 'libx264', '-preset', 'veryfast',
                    '-crf', '23', '-c:a', 'aac', '-b:a', '64k',
                    str(args.out / 'asr-input.mp4')], check=True)
    frames = [{'file': str(p.relative_to(args.out)), 'timestamp_ms': round(i*1000/3),
               'sha256': digest(p)} for i, p in enumerate(sorted((args.out/'frames').glob('*.jpg')))]
    assert len(frames) == 180
    save(args.out/'frames.json', frames)
    source = Path('assets/mygo_background_referrence/characters.md')
    # Only appearance facts, avoiding legacy field names and seconds instructions.
    background = '\n'.join(line for line in source.read_text().splitlines()
                           if line.startswith('- ') and '：' in line and
                           any(line.startswith('- '+n) for n in
                               ['高松燈', '千早愛音', '要楽奈', '長崎そよ', '椎名立希', '若葉睦', '豊川祥子', '三角初華', '八幡海鈴']))
    background += '\n本任务统一使用简体人名：高松灯、千早爱音、要乐奈、长崎爽世、椎名立希、若叶睦、丰川祥子、三角初华、八幡海铃。'
    (args.out/'background.txt').write_text(background)
    save(args.out/'media.json', {'video': str(args.video.resolve()), 'sha256': digest(args.video),
         'bounds_ms': [0, 60000], 'media_seconds': time.monotonic()-start,
         'asr_video_sha256': digest(args.out/'asr-input.mp4'), 'background_source_sha256': digest(source),
         'sampling': 'ffmpeg fps=3, scale=-2:720, JPEG q=3; frame timestamp round(index*1000/3)',
         'created_at': datetime.now(timezone.utc).isoformat()})


def observations(args):
    if (args.out/'asr.json').exists():
        raise ValueError('Frozen observations already exist')
    # Localhost only; user chose local Whisper and no separate OCR service.
    began = time.monotonic()
    source = json.loads((args.out/'media.json').read_text())
    subprocess.run(['ffmpeg', '-v', 'error', '-i', source['video'], '-t', '60',
                    '-vn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le',
                    str(args.out/'asr-input.wav')], check=True)
    extraction = time.monotonic()-began
    with (args.out/'asr-input.wav').open('rb') as audio:
        response = httpx.post('http://127.0.0.1:18842/v1/audio/transcriptions',
                              files={'file': ('chunk.wav', audio, 'audio/wav')},
                              data={'response_format':'verbose_json', 'language':'ja',
                                    'temperature':'0.0', 'max_context':'0',
                                    'best_of':'5', 'beam_size':'5'},
                              timeout=600, trust_env=False)
    response.raise_for_status()
    raw = response.json()
    save(args.out/'asr-raw.json', raw)
    result = normalize_transcription(raw, duration_seconds=60000)
    save(args.out/'asr.json', result.model_dump(mode='json'))
    save(args.out/'ocr.json', None)
    weights = Path('outputs/local-models/whisper/ggml-large-v3-turbo-q5_0.bin')
    save(args.out/'asr-provenance.json', {'provider':'localhost whisper.cpp',
         'model':'large-v3-turbo-q5_0', 'language':'ja', 'max_context':0, 'temperature':0,
         'best_of':5, 'beam_size':5, 'threads':6, 'language_probabilities':False,
         'seconds':time.monotonic()-began, 'audio_extraction_s':extraction,
         'source_video_sha256':source['sha256'], 'audio_source':'original video first 60 seconds',
         'audio_sha256':digest(args.out/'asr-input.wav'), 'ocr':'disabled by user',
         'weights_sha256':digest(weights),
         'runtime_commit':subprocess.check_output(['git','-C','outputs/tools/whisper.cpp','rev-parse','HEAD'],text=True).strip()})
    print(f'Local ASR ready: {time.monotonic()-began:.2f}s, {len(result.transcript)} cues; OCR disabled', flush=True)


COMMON = '''理解这段 [0,60000) 毫秒的视频，生成可供剧本检索使用的事件。图片是有序的 3 FPS 采样；每张前面有绝对毫秒标签。
ASR/OCR 是不完美的观察证据，可交叉核对；它们不是要求照抄的事件。只记录可确认的事实，不用背景编造内容。
本轮 OCR=null，未运行独立 OCR；请直接从图片读取字幕及画面文字，不得声称已有 OCR 结果。
保留完整对白及有证据的原文/译文，不把多句不同发言合并成一句；未知说话人允许未知。
场景、独立动作、可观察情绪、关系变化、非对白画面文字和其他事实分别分类；普通切镜不是新场景，动作不要全部写成场景。
不要求七种类型都出现，也不能为满足类型而造事件。普通对白字幕归对白，不重复写成画面文字。
participants 表示完整语义参与者，包含已确认说话人/交谈对象；visible 表示画面可见人物，画外说话人不自动可见。
未确认参与/可见身份时使用空名单；人物正文明确参与时不要省略名单。已知 speaker 必须在 participants 中。
时间都是绝对整数毫秒（内部 *_seconds 字段也是毫秒），0<=start<end<=60000；跨边界发言只写可确认部分。
描述用简体中文，台词保持证据中的完整原语言与有证据的译文；不猜测动机。不要 Markdown 围栏、解释、工具调用或省略号占位。
'''

XML_CONTRACT = '''只输出一个 timeline 根节点，下面是 event 列表。每 event 只有一个主类型子节点：scene、dialogue、action、emotion、relation-change、screen-text、other。
event 必填 start/end（带引号），可填 participants/visible（英文逗号分隔完整人名），空名单可省略。主类型正文不能为空。
dialogue 的 speaker 属性为人名，未知则省略；正文只有台词，不重复说话人。默认主台词 lang=zh-Hans、role=translation，原文默认 ja。
没有译文证据时显式 lang="ja" role="original"。可选 original/translation 子节点保存不同语言版本，translation 必填 lang。同语言同角色不得重复。
可选 description 子节点只写额外观察，无额外信息可省略；宿主将生成默认描述。补充子节点后只允许排版空白。
非对白主类型只有纯文本，screen-text 可有 lang 属性。不要 note、persons、event_type、speaker_source 或其他字段。标准 XML 转义；无缩进、标签和正文分行。
语法示例（虚构，非片段事实）：
<timeline>
<event start="1000" end="2000" participants="甲,乙" visible="甲">
<dialogue speaker="甲" lang="zh-Hans">
你好
<original lang="ja">こんにちは</original>
<description>甲挥手招呼。</description>
</dialogue>
</event>
<event start="2000" end="3000" participants="乙" visible="乙"><action>乙点头。</action></event>
</timeline>'''


def benchmark(args):
    frames = json.loads((args.out/'frames.json').read_text())
    for row in frames:
        assert digest(args.out/row['file']) == row['sha256']
    evidence = {k: json.loads((args.out/f'{k}.json').read_text()) for k in ['asr', 'ocr']}
    evidence['background'] = (args.out/'background.txt').read_text()
    is_compact = args.contract == 'compact-shot-v2'
    common = (compact.COMMON if is_compact else COMMON) + '\n固定观察与人物背景：\n' + json.dumps(evidence, ensure_ascii=False)
    prompts = ({'json': common+'\n'+compact.JSON_CONTRACT, 'xml': common+'\n'+compact.XML_CONTRACT}
               if is_compact else {
                   'json': common + '\n只输出符合以下领域结构的 JSON。speaker_source 不输出；允许程序排序。\n'
                   + json.dumps(VideoParseResult.model_json_schema(), ensure_ascii=False),
                   'xml': common + '\n' + XML_CONTRACT})
    for fmt, prompt in prompts.items():
        if (args.out/f'prompt-{fmt}.txt').exists() and (args.out/f'prompt-{fmt}.txt').read_text() != prompt:
            raise ValueError('Cannot overwrite a different frozen prompt; use a new output directory')
        (args.out/f'prompt-{fmt}.txt').write_text(prompt)
    plan = {'kimi': ('k3', 'https://api.kimi.com/coding/v1', 'KIMI_API_KEY', {'reasoning': {'effort': 'low'}}),
            'doubao': ('doubao-seed-2-1-pro-260628', ARK_OPENAI_BASE_URL, 'ARK_API_KEY', {'extra_body': {'thinking': {'type': 'disabled'}}})}
    save(args.out/'protocol.json', {'plan': {k: {'model': v[0], 'base_url': v[1], 'options': v[3]} for k,v in plan.items()},
         'contract': args.contract, 'normalized_model': 'CompactTimeline (experimental)' if is_compact else 'VideoParseResult',
         'order': ['json', 'xml', 'xml', 'json'], 'frame_count': len(frames), 'detail': 'high',
         'max_output_tokens': 30000, 'tools': [], 'response_format': 'plain text; local validation',
         'source_sha256': {p: digest(Path(p)) for p in [__file__, 'evals/parse_output_protocol.py', 'evals/parse_compact_protocol.py', 'engine/parsev2/schemas.py']},
         'input_sha256': {p: digest(args.out/p) for p in ['frames.json','asr.json','ocr.json','background.txt','prompt-json.txt','prompt-xml.txt','quality-checkpoints.json']},
         'git_head': subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
         'dirty_diff_sha256': hashlib.sha256(subprocess.check_output(['git','diff'])).hexdigest()})
    for provider in ([args.model] if args.model else plan):
        model, base, key, options = plan[provider]
        for i, fmt in enumerate(['json', 'xml', 'xml', 'json']):
            folder = args.out/f'{provider}-{i+1}-{fmt}'
            if folder.exists():
                continue  # Existing attempts are immutable, including failed attempts.
            folder.mkdir()
            began, first, last, terminal_time = time.monotonic(), None, None, None
            pieces, terminal = [], None
            row = {'provider': provider, 'model': model, 'format': fmt, 'started_at': datetime.now(timezone.utc).isoformat(),
                   'valid': False, 'first_body_s': None, 'last_body_s': None, 'usage': None,
                   'first_event_s': None, 'decode_s': None}
            print(f'Start {folder.name}', flush=True)
            def expired(signum, frame):
                raise TimeoutError('600 second total attempt deadline')
            previous = signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, 600)
            try:
                content = []
                for frame in frames:
                    content.extend([{'type': 'input_text', 'text': f"Frame at {frame['timestamp_ms']} ms."},
                                    {'type': 'input_image', 'image_url': encode_media_file(args.out/frame['file'], expected_kind='image')[0], 'detail': 'high'}])
                content.append({'type': 'input_text', 'text': prompts[fmt]})
                row['request_start_s'] = time.monotonic()-began
                with OpenAI(base_url=base, api_key=os.environ[key], max_retries=0, timeout=600) as client:
                    with client.responses.create(model=model, input=[{'role':'user','content':content}],
                                                 max_output_tokens=30000, stream=True, **options) as stream:
                        with (folder/'events.jsonl').open('w') as trace:
                            for event in stream:
                                elapsed = time.monotonic()-began
                                if row['first_event_s'] is None:
                                    row['first_event_s'] = elapsed
                                trace.write(json.dumps({'t': elapsed, 'event': event.model_dump(mode='json')}, ensure_ascii=False)+'\n')
                                trace.flush()
                                if event.type == 'response.output_text.delta' and event.delta:
                                    first = elapsed if first is None else first
                                    last = elapsed
                                    pieces.append(event.delta)
                                if event.type in ('response.completed','response.failed','response.incomplete'):
                                    terminal, terminal_time = event.response, elapsed
                                    save(folder/'terminal.json', terminal.model_dump(mode='json'))
                                    break
                if terminal is not None:
                    row['returned_model'] = terminal.model
                    row['terminal_status'] = terminal.status
                    row['usage'] = terminal.usage.model_dump(mode='json') if terminal.usage else None
                if terminal is None or terminal.status != 'completed':
                    raise ValueError('No completed terminal response')
                raw = ''.join(pieces)
                terminal_text = ''.join(part.text for item in terminal.output if item.type == 'message'
                                        for part in item.content if part.type == 'output_text')
                if not raw or raw != terminal_text:
                    raise ValueError('Streamed text absent or differs from terminal text')
                decode_start = time.monotonic()
                try:
                    result = (compact.decode_compact_result(raw, output_format=fmt, start_ms=0, end_ms=60000)
                              if is_compact else decode_result(raw, output_format=fmt, start_ms=0, end_ms=60000,
                                                               defaults=LanguageDefaults('zh-Hans','TRANSLATION','ja')))
                finally:
                    row['decode_s'] = time.monotonic()-decode_start
                save(folder/'result.json', result.model_dump(mode='json'))
                row.update(valid=True, event_counts=dict(Counter(e.type if is_compact else e.event_type for e in result.timeline)))
            except Exception as error:
                row['error'] = str(error)[:2000]
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)
                (folder/'raw.txt').write_text(''.join(pieces))
                row.update(total_s=time.monotonic()-began, first_body_s=first, last_body_s=last,
                           terminal_s=terminal_time, body_s=last-first if first is not None else None)
                save(folder/'summary.json', row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if terminal is None:
                print('Transport/preflight failed: stopping this provider, no automatic repeated failures.', flush=True)
                break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=['media','observations','benchmark'], required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--video', type=Path)
    parser.add_argument('--env-file', type=Path, default=Path('.env.local'))
    parser.add_argument('--model', choices=['kimi','doubao'])
    parser.add_argument('--contract', choices=['full-v1','compact-shot-v2'], default='full-v1')
    args = parser.parse_args()
    for line in args.env_file.read_text().splitlines():
        if '=' not in line or line.lstrip().startswith('#'):
            continue
        key, value = line.split('=', 1)
        if key.strip().endswith('_API_KEY'):
            parts = shlex.split(value)
            os.environ[key.strip()] = parts[0] if parts else ''
    {'media': media, 'observations': observations, 'benchmark': benchmark}[args.phase](args)


if __name__ == '__main__':
    main()

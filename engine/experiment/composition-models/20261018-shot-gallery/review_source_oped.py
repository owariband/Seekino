"""Render source-video contact sheets for manual OP/ED annotation review."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
SOURCES = {s['episode']: s for s in json.loads((OUT / 'sources.json').read_text())['episodes']}
WINDOWS = {
    1: (None, (1326100, 1413628)),
    2: ((57187, 145302), (1315312, 1405000)),
    3: ((0, 90937), (1315000, 1404349)),
    4: ((85312, 174300), (1258600, 1348900)),
    5: ((77812, 167794), (1313437, 1405000)),
    6: ((141554, 230500), (1315312, 1404399)),
    7: ((118900, 208000), (1259039, 1349000)),
    8: ((34000, 123700), (1315312, 1403967)),
    9: (None, (1315312, 1404425)),
    10: (None, (1315312, 1405000)),
    11: ((109687, 197813), (1315312, 1404373)),
    12: ((55000, 144000), (1315312, 1404500)),
    13: ((66562, 155500), (1350800, 1397000)),
}


def sheet(name, requests, columns=4):
    target = OUT / f'{name}.jpg'
    if target.exists():
        return
    with tempfile.TemporaryDirectory(prefix='review-captures-', dir=OUT) as directory:
        directory = Path(directory)

        def capture(index_request):
            index, (episode, timestamp, label) = index_request
            timestamp = max(0, min(timestamp, SOURCES[episode]['duration_ms'] - 100))
            path = directory / f'{index:04d}.jpg'
            subprocess.run([
                'ffmpeg', '-v', 'error', '-threads', '1', '-ss', f'{timestamp/1000:.3f}',
                '-i', SOURCES[episode]['path'], '-frames:v', '1', '-an', '-sn',
                '-vf', 'scale=-2:320', '-q:v', '3', '-threads', '1', '-y', str(path),
            ], check=True, capture_output=True)
            return path

        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(capture, enumerate(requests)))
        width, height = 400, 252
        image = Image.new('RGB', (columns * width, ((len(paths)+columns-1)//columns) * height + 40), '#15181f')
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 17)
        draw.text((12, 8), name, font=font, fill='white')
        for index, (path, (episode, timestamp, label)) in enumerate(zip(paths, requests)):
            x, y = (index % columns) * width, (index // columns) * height + 40
            with Image.open(path) as frame:
                frame.thumbnail((width-8, 220))
                image.paste(frame, (x+4, y))
            draw.text((x+6, y+224), f'EP{episode:02d} {max(0,timestamp)/1000:.3f}s {label}', font=font, fill='white')
        image.save(target, quality=94)
    print(target.name, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--initial', action='store_true')
    parser.add_argument('--annotated', action='store_true', help='Render the final annotated boundary brackets')
    parser.add_argument('--samples', help='JSON list of [episode, timestamp_ms, label]')
    parser.add_argument('--name', default='oped-review-detail')
    args = parser.parse_args()
    if args.annotated:
        annotation = json.loads((OUT / 'op_ed_segments.json').read_text())
        for episode in annotation['reviewed_episodes']:
            requests = []
            for segment in annotation['segments']:
                if segment['episode'] != episode:
                    continue
                label = segment['label']
                requests += [(episode, segment['start_boundary_bracket_ms'][0], label+' before start'),
                             (episode, segment['start_boundary_bracket_ms'][1], label+' after start'),
                             (episode, segment['end_boundary_bracket_ms'][0], label+' before end'),
                             (episode, segment['end_boundary_bracket_ms'][1], label+' after end')]
            sheet(f'oped-reviewed-ep{episode:02d}', requests)
        return
    if args.samples:
        sheet(args.name, json.loads(args.samples))
        return
    if not args.initial:
        parser.error('Choose --initial, --annotated or --samples')
    for episode, windows in WINDOWS.items():
        requests = []
        for label, window in zip(('OP', 'ED'), windows):
            if not window:
                continue
            begin, end = window
            requests += [(episode, begin-2000, f'{label} pre'), (episode, begin+1000, f'{label} start'),
                         (episode, (begin+end)//2, f'{label} middle'), (episode, end-1000, f'{label} end'),
                         (episode, end+1000, f'{label} after1'), (episode, end+2500, f'{label} after2')]
        sheet(f'oped-boundaries-ep{episode:02d}', requests)
    for episode in (1, 9, 10):
        sheet(f'oped-opening-overview-ep{episode:02d}', [(episode, second*1000, 'opening scan') for second in range(0, 181, 10)])
    sheet('oped-ending-overview-ep13', [(13, second*1000, 'ending scan') for second in range(1300, 1421, 10)])


if __name__ == '__main__':
    main()

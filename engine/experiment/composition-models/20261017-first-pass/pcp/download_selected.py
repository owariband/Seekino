"""Retrieve only selected files from the official PCP ZIP, using HTTP Range."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import urllib.request
import zlib

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--cache", required=True, type=Path)
parser.add_argument("--weights", action="store_true")
args = parser.parse_args()
url = "https://mcl.korea.ac.kr/research/Submitted/jtlee_JVCIR2018/code.zip"
entries = json.loads((args.cache / "zip_entries.json").read_text())
selected = {"finetune_iter_10000.prototxt", "CompositionClassifier.cpp", "CompositionClassifier.h",
            "ConvNet.cpp", "ConvNet.h", "Config.h", "Main.cpp", "ProjectUtils.cpp"}
if args.weights:
    selected = {"caffenet_finetune_iter_10000.caffemodel"}
output = args.cache / ("weight_source.json" if args.weights else "code_source.json")
previous = {item["name"]: item for item in json.loads(output.read_text())} if output.exists() else {}
metadata = []
for entry in entries:
    name = Path(entry["name"]).name
    if name not in selected:
        continue
    target = args.cache / name
    if target.exists() and target.stat().st_size == entry["bytes"]:
        item = previous.get(entry["name"], dict(entry, url=url))
        item["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        metadata.append(item)
        print("existing", name, flush=True)
        continue
    offset = entry["local_offset"]
    req = urllib.request.Request(url, headers={"Range": f"bytes={offset}-{offset+29}"})
    with urllib.request.urlopen(req, timeout=30) as response:
        assert response.status == 206
        header = response.read()
    assert header[:4] == b"PK\x03\x04"
    name_length, extra_length = struct.unpack_from("<HH", header, 26)
    start = offset + 30 + name_length + extra_length
    end = start + entry["compressed_bytes"] - 1
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    inflater = zlib.decompressobj(-15)
    received = 0
    crc = 0
    args.cache.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".partial")
    with urllib.request.urlopen(req, timeout=60) as response, temp.open("wb") as out:
        assert response.status == 206
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            received += len(chunk)
            data = inflater.decompress(chunk) if entry["method"] == 8 else chunk
            out.write(data)
            crc = zlib.crc32(data, crc)
        tail = inflater.flush() if entry["method"] == 8 else b""
        out.write(tail)
        crc = zlib.crc32(tail, crc)
    assert received == entry["compressed_bytes"]
    assert temp.stat().st_size == entry["bytes"]
    assert crc == entry["crc32"]
    temp.rename(target)
    item = dict(entry, sha256=hashlib.sha256(target.read_bytes()).hexdigest(), url=url,
                compressed_byte_range=[start, end])
    metadata.append(item)
    print(json.dumps(item), flush=True)
output.write_text(json.dumps(metadata, indent=2)+"\n")

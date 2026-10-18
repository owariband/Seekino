#!/usr/bin/env bash
# Renders the Seekino promo from source: Kino's Theme (60 s), then the 60 s video.
# Run from anywhere; outputs land in assets/promo/out/.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="$here/out"
mkdir -p "$out"

echo "1/2  Kino's Theme (about 30 s to synthesize)"
python3 "$here/music/kino_theme.py" "$out/kino-theme-60s.wav"
ffmpeg -loglevel error -y -i "$out/kino-theme-60s.wav" -codec:a libmp3lame -b:a 192k "$out/kino-theme-60s.mp3"

echo "2/2  60 s promo video"
node "$here/video/promo-60s.mjs"

if [[ "${1:-}" == "--explorations" ]]; then
  echo "+    earlier directions"
  node "$here/explorations/snowdin-town-intro.mjs"
  node "$here/explorations/battle-box-style-frames.mjs"
fi

echo "done: $out"

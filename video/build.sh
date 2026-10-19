#!/usr/bin/env bash
# Renders the explainer animation and encodes it for the web page.
# Needs Manim Community Edition and ffmpeg on PATH (see video/README.md).
set -euo pipefail
cd "$(dirname "$0")/.."

python -m manim -r 1280,720 --fps 30 --media_dir video/media video/stopproof_flow.py StopProofFlow
SRC=video/media/videos/stopproof_flow/720p30/StopProofFlow.mp4
OUT=stopproof/ui/assets
mkdir -p "$OUT"

ffmpeg -y -loglevel error -i "$SRC" -c:v libx264 -preset slow -crf 24 -pix_fmt yuv420p -movflags +faststart -an "$OUT/stopproof-flow.mp4"
ffmpeg -y -loglevel error -ss 9 -i "$SRC" -frames:v 1 -q:v 3 "$OUT/stopproof-flow.jpg"
ls -la "$OUT"

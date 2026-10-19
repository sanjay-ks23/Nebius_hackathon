# Explainer animation

`stopproof_flow.py` is a Manim Community Edition scene that explains the whole StopProof flow in about
60 seconds. The page embeds the rendered file from `stopproof/ui/assets/`, so Manim is only needed to
re-render it and is not a runtime dependency.

## Re-render

Manim needs Cairo, Pango and FFmpeg. The simplest route without root access is micromamba:

```bash
micromamba create -p ~/mm/video -c conda-forge python=3.12 manim ffmpeg
export PATH=~/mm/video/bin:$PATH
bash video/build.sh
```

The scene uses the Arial and Consolas fonts; any font installed under those names works.

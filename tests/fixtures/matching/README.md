# Matching fixtures

Synthetic clips used by `tests/integration/test_matching_corpus.py`.

- 160x90, 25 fps, six seconds (trims and the middle segment are shorter).
- Generated from FFmpeg's `testsrc2`, `mandelbrot`, `smptehdbars`, and
  `cellauto` sources. No personal or copyrighted content.
- `manifest.json` lists the content family and the expected classification
  against `base.mp4`.

Regenerate from the repository root:

```powershell
.venv\Scripts\python tools\build_matching_fixtures.py
```

The clips are committed so the matching gate sees the same pixels across
machines and ffmpeg builds. Keep the folder under the 1.5 MB budget.

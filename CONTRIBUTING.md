# Contributing

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]" -c constraints.txt
```

FFmpeg and FFprobe must be on `PATH`:

```powershell
winget install --id Gyan.FFmpeg -e
```

## Checks

```powershell
python -m ruff check src tests tools
python -m pytest
```

## Matching quality gate

```powershell
python tools/build_corpus.py --output eval_corpus
python tools/evaluate.py --corpus eval_corpus --min-duration-ratio 0.01
```

The gate passes when `evaluate.py` exits zero. `eval_corpus/` is generated and
is not committed.

## Repository scope

This public repository is a source snapshot. The maintainers keep the full
development history and the detailed decision log privately, so some source
comments point at internal notes that are not in this repository. The source,
tests, and packaging are complete here; the installer is attached to the
GitHub release.

## Style

- Keep the existing module boundaries.
- Run the lint and test commands above before opening a pull request.

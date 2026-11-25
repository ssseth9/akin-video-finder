# Akin Video Finder

[![Keep the Why](https://keepthewhy.com/assets/badge.svg)](https://keepthewhy.com)

**Find duplicate and near-duplicate videos in large libraries - even after re-encoding, resizing, or trimming.**

Akin Video Finder compares content, not just filenames. It detects exact copies and videos that show the same scenes despite different codecs, bitrates, resolutions, or start/end cuts. You get clear match reasons, side-by-side previews, quality recommendations, and full control over every keep/remove decision. Nothing is deleted until you confirm.

## Highlights

- **Exact + perceptual matching** - Identical videos and re-encoded / resized / slightly trimmed versions
- **Side-by-side review** - Frame grids, resolution, bitrate, codec, duration, and size for every pair
- **Smart recommendations** - Marks the better-quality or longer copy as ★ Recommended and explains why
- **Fast keyboard-driven review** - Full-window compare mode with arrow keys, `1`/`2` to keep, `I` to ignore, `Esc` to close
- **Persistent ignores** - Ignored pairs stay ignored in later scans, including copies under other paths. Keep/remove choices stay saved until you run a new scan
- **Safe by design** - Scan and review never touch your videos. Moves/deletes only happen after explicit confirmation. Deletes go to Recycle Bin (or a same-drive quarantine folder when needed)
- **Fully offline** - No account, no telemetry, runs entirely on your machine

## Requirements

- Windows 11, 64-bit
- FFmpeg + FFprobe

```powershell
winget install --id Gyan.FFmpeg -e
```

The app looks for them on `PATH`, or you can set paths in `config.toml`.

- Python 3.12+ only if you run from source

## Install

### Recommended: Installer

1. Install FFmpeg (see above).
2. Download `akin-video-finder-setup.exe` from the [latest release](../../releases/latest).
3. Run it. Default install is per-user (no admin rights). Adds a Start menu entry; desktop shortcut is optional.
4. Launch **Akin Video Finder** from the Start menu.

The build is unsigned, so SmartScreen may warn on first launch. If you downloaded from this repo’s Releases page, choose **More info → Run anyway**. Checksums are provided on the release page.

### Portable

Download `akin-video-finder-gui.exe`, put it anywhere, and double-click. It creates `config.toml` next to the exe (or falls back to `%APPDATA%\akin-video-finder` if the folder is read-only).

### From source

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e . -c constraints.txt
akin-video-finder init-config --output config.toml
akin-video-finder gui --config config.toml
```

## First scan

The app has four tabs: **Scan**, **Scan log**, **Results**, and **Tools**.

1. Open the **Scan** tab.
2. Add folders (drag & drop, browse, or **+ Add folder**). Hold `Alt` while dropping to exclude. You can also add exclude folders or patterns.
3. Choose matching strictness:
   - **Only near-identical videos** - fewest results
   - **Similar copies** - balanced default
   - **Anything similar** - most results (more false positives)
   - **Custom** - control the Strictness slider yourself
4. Set the **Duration difference limit** (default 12%, max 25%).
5. Click **Scan now**. The first scan of a large library can take hours; later scans are much faster because previous work is cached.
6. Watch the status bar and **Scan log**. You can stop after the current video. When the scan and preview generation finish, a short chime plays and the **Results** tab opens.

If you change folders or matching options later, the status bar warns you and you need a new scan.

Connect your storage before you scan. If a folder is not available, the app skips it and keeps its earlier records.

## Reviewing results

Every match is shown as a pair (exact and similar together). Labels are **Exact copy**, **Very likely duplicate**, or **Possible duplicate**, with match percentage and a short reason.

- Sort by best match or by size
- Hide already-reviewed pairs
- **Keep** / **Mark for removal** (click again to clear)
- **Ignore pair** (or **Ignore all shown…**)
- **Compare pairs** - full-window view with keyboard shortcuts (`1`/`2` keep, `C` clear, `I` ignore, arrows, `Esc`)
- Recoverable space estimate and **Marked files** button in the toolbar

## Removing videos

Marking does nothing to your videos. Open **Marked files** when you’re ready:

1. Review the list.
2. **Move N…** to a folder of your choice, or
3. **Delete N…** (goes to Recycle Bin; falls back to same-drive quarantine if needed).

Videos that fail safety checks are listed separately with reasons. Protected folders (configured under **Tools**) are always skipped by actions. Moved/quarantined items appear under **Moved / quarantined** with a **Restore** button. Permanent deletion is a separate explicit step.

## Tools tab

- **Export report** - JSON or CSV
- **Ignored pairs** - review and restore ignored matches
- **Protected folders** - folders that actions must never touch
- **Clean up records** - remove database entries for videos removed outside the app. It skips drives that are not available and does not touch your videos

## How it works

1. Lists videos and reads basic metadata.
2. Detects exact content matches (names and paths ignored).
3. For near-duplicates, compares frames sampled across the video plus audio. Handles start/end cuts.
4. Caches work so subsequent scans are fast.

A match percentage is a guide. Only **Exact copy** means the videos are identical.

## Command line

Available with a source install (the installer ships GUI only):

| Command | Purpose |
|---------|---------|
| `akin-video-finder doctor` | Config, FFmpeg, folders, output paths |
| `akin-video-finder init-config` | Write a sample `config.toml` |
| `akin-video-finder scan` | Run a scan (`Ctrl+C` cancels) |
| `akin-video-finder status` | Last scan summary |
| `akin-video-finder report` | Export results (JSON/CSV) |
| `akin-video-finder cleanup` | Prune records for videos removed outside the app (dry-run by default; `--apply` to write) |
| `akin-video-finder gui` | Launch the GUI |
| `akin-video-finder merge-db` | Merge another database (backups by default) |

Review, move and delete actions, ignored pairs, and protected folders are GUI-only.

```powershell
akin-video-finder doctor --config config.toml
akin-video-finder scan --config config.toml
akin-video-finder report --config config.toml --format csv --output scan-report.csv
akin-video-finder cleanup --config config.toml --apply
```

## Configuration

See `config.example.toml` for every option. The packaged app creates `config.toml` next to the exe (or in `%APPDATA%\akin-video-finder`). The GUI auto-saves Scan-tab changes.

Key settings:
- `[[roots]]` - folders to scan (`recursive = true` for subfolders)
- `exclude_patterns` - skip by name/pattern
- `include_hidden` - scan hidden and system items
- `video_extensions` - defaults cover the common formats; `.ts`/`.mts` are omitted by default (common TypeScript collision). Add them if your videos use those formats
- `[actions] protected_paths`
- Database and report output paths

Close the app before hand-editing `config.toml`.

## Known limitations

- Audio-only duplicates are not detected
- Short middle overlaps, mirrored, or heavily cropped videos may be missed
- Very short overlapping segments can be missed
- Related but non-duplicate clips (same series, consecutive shots) can appear - just ignore them
- The app does not learn from ignored pairs (use **Ignore all shown…** for groups)
- First scan is deliberately thorough and therefore slower

## Troubleshooting

- **FFmpeg missing** → install with the winget command above and restart (or run `akin-video-finder doctor`)
- **Unreadable video** → check **Scan log**; exclude persistently failing videos
- **SmartScreen warning** → confirm the download came from this repo’s Releases, then **More info → Run anyway**
- **Scan appears stuck** → large videos take time; open **Scan log** and use **Stop** if needed. `Ctrl+C` does not stop the scan in the app
- **Empty Results** → turn off **Hide reviewed**, check **Ignored pairs**, or re-scan if settings changed
- **Action skipped a video** → check **Protected folders**. The action dialog also shows the reason

## Privacy

Everything runs locally. No account, no telemetry, offline-capable. Videos are opened read-only. Videos are only moved or deleted after you confirm.

## For developers

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and contribution rules. The public repository is a source snapshot.

```powershell
python -m pip install -e ".[dev]" -c constraints.txt
python -m ruff check src tests tools
python -m pytest
```

Build:

```powershell
.\build-exe.ps1
iscc packaging\installer.iss
```

(Optional: place `ffmpeg.exe` + `ffprobe.exe` in `packaging\bin\` to bundle them.)

## License

MIT - see [LICENSE](LICENSE).

Third-party notices for the Windows build: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

FFmpeg is not bundled and keeps its own license.

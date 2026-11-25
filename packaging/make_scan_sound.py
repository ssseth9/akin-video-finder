"""Synthesise the scan-completion chime and write it as a WAV file.

The cue is original: a rising D-major arpeggio, each note a decaying bell with
two overtones over a soft low root. Windows plays it asynchronously when a
completed scan's preview queue drains.

Run from the repository root:

    .venv/Scripts/python packaging/make_scan_sound.py

Output (committed, so a build does not need this script):

    resources/sounds/scan-complete.wav   44.1 kHz, 16-bit mono PCM
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 44_100
DURATION = 1.6
PEAK = 0.5
ATTACK_S = 0.006
FADE_OUT_S = 0.05
OUT = Path(__file__).resolve().parents[1] / "resources" / "sounds" / "scan-complete.wav"

# (start seconds, frequency Hz, amplitude, decay seconds)
NOTES = (
    (0.00, 587.33, 0.40, 0.55),  # D5
    (0.11, 739.99, 0.38, 0.55),  # F#5
    (0.22, 880.00, 0.36, 0.60),  # A5
    (0.31, 1174.66, 0.16, 0.45),  # D6
)
BODY = (0.00, 293.66, 0.18, 1.00)  # D4
PARTIALS = ((1.0, 1.0), (2.0, 0.24), (3.0, 0.08))


def _decaying_note(t: float, frequency: float, amplitude: float, decay: float) -> float:
    envelope = (1.0 - math.exp(-t / ATTACK_S)) * math.exp(-t / decay)
    tone = sum(
        gain * math.sin(2.0 * math.pi * frequency * ratio * t) for ratio, gain in PARTIALS
    )
    return amplitude * envelope * tone


def _sample(t: float) -> float:
    value = 0.0
    for start, frequency, amplitude, decay in (BODY, *NOTES):
        if t >= start:
            value += _decaying_note(t - start, frequency, amplitude, decay)
    return value


def main() -> None:
    count = int(RATE * DURATION)
    raw = [_sample(index / RATE) for index in range(count)]
    loudest = max(abs(value) for value in raw) or 1.0
    scale = PEAK / loudest
    fade = max(1, int(RATE * FADE_OUT_S))
    frames = bytearray()
    for index, value in enumerate(raw):
        tail = min(1.0, (count - index) / fade)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, value * scale * tail)) * 32767))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(bytes(frames))
    print(f"Wrote {OUT} ({OUT.stat().st_size:,} bytes, {DURATION:.1f} s)")


if __name__ == "__main__":
    main()

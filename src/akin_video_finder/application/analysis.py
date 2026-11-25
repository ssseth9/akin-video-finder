"""Pair analysis: keeper recommendations and evidence consistency checks.

PRODUCT_SPEC section 15: rank members by content completeness, decode
health, bitrate, native resolution, demonstrated visual detail, audio
quality, codec evidence, preferred folder/format, then file size as
supporting evidence. Bitrate is the primary quality signal: it rewards
both higher-resolution originals and densely-encoded sources.

The matcher records every direct match as its own pair; this module ranks
the two files of a pair and describes why one copy is recommended.
"""

import logging
import math
from pathlib import Path
from typing import Any

from akin_video_finder.configuration.models import KeeperConfig
from akin_video_finder.core.models import ProbeStatus

logger = logging.getLogger(__name__)

# Coarse audio-quality ordering for keeper evidence
_AUDIO_CODEC_RANK = {
    "flac": 8,
    "alac": 8,
    "pcm_s16le": 8,
    "aac": 7,
    "opus": 7,
    "vorbis": 6,
    "eac3": 6,
    "ac3": 6,
    "dts": 6,
    "wmapro": 5,
    "wmav2": 4,
    "mp3": 4,
    "mp2": 3,
}

# Codec quality/efficiency ordering used as supporting evidence
_VIDEO_CODEC_RANK = {
    "av1": 6,
    "hevc": 6,
    "h265": 6,
    "vp9": 5,
    "h264": 4,
    "avc": 4,
    "avc1": 4,
    "vp8": 3,
    "wmv3": 3,
    "mpeg4": 2,
    "mpeg2video": 2,
    "wmv2": 2,
    "wmv1": 2,
    "mpeg1video": 1,
}

# Resolution stops are capped at the 4K value: beyond that the extra pixels
# are almost always an upscale, and spec KEEP-004 says declared resolution
# alone must not decide the keeper
_MAX_PIXEL_STOPS = 0.25 * math.log2(3840 * 2160)


# Quality weights, in log2 "stops". Bitrate carries a full weight because it
# is the spec's primary quality signal; resolution gets a quarter, so a dense
# 720p encode still beats a thin 4K one while four times the pixels remains
# worth a real half-stop. Codec, audio and frame rate are nudges: enough to
# break a tie, never enough to outvote a visible quality gap.
_BITRATE_WEIGHT = 1.0
_PIXEL_WEIGHT = 0.25
_CODEC_WEIGHT = 0.25
_AUDIO_CODEC_WEIGHT = 0.1
_AUDIO_CHANNEL_WEIGHT = 0.05
_FPS_WEIGHT = 0.25


def _parse_fps(value: Any) -> float:
    """Parse an FFprobe rational frame rate like '30000/1001' or '25/1'."""
    if value is None:
        return 0.0
    text = str(value)
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(text)
    except (ValueError, TypeError):
        return 0.0


def _quality_stops(
    member: dict[str, Any], probe: dict[str, Any] | None, keeper: KeeperConfig
) -> float:
    """Quality in log2 units, so a pairwise difference is a ratio.

    Only the difference between two copies reaches a decision, so the
    arbitrary origins of these terms cancel and the units stay honest: one
    stop is a doubling of bitrate, or 16 times the pixels at the pixel weight.

    Bitrate leads (spec section 15: it rewards both higher-resolution
    originals and densely-encoded ones). Pixels count for less because
    resolution only buys detail when the bitrate can fill it. Codec, audio
    and frame rate are nudges, deliberately small enough that they break ties
    rather than outvote a visible quality gap.
    """
    if not probe:
        return 0.0
    bitrate = float(probe["container_bitrate"] or 0.0)
    width = int(probe["primary_video_width"] or 0)
    height = int(probe["primary_video_height"] or 0)
    pixels = float(width * height)
    fps = _parse_fps(probe["primary_video_fps"])

    stops = 0.0
    if bitrate > 0:
        stops += _BITRATE_WEIGHT * math.log2(bitrate / 1000.0)
    if pixels > 0:
        # Capped at 4K: past that the extra pixels are almost always an
        # upscale, and declared size alone must not decide the keeper
        stops += min(_PIXEL_WEIGHT * math.log2(pixels), _MAX_PIXEL_STOPS)
    if fps > 0:
        stops += _FPS_WEIGHT * math.log2(fps)

    video_codec = str(probe["primary_video_codec"] or "").lower()
    stops += _CODEC_WEIGHT * float(_VIDEO_CODEC_RANK.get(video_codec, 0))

    audio_codec = str(probe["primary_audio_codec"] or "").lower()
    stops += _AUDIO_CODEC_WEIGHT * float(_AUDIO_CODEC_RANK.get(audio_codec, 0))
    stops += _AUDIO_CHANNEL_WEIGHT * float(probe["primary_audio_channels"] or 0)
    return stops


def _member_key(
    member: dict[str, Any],
    probe: dict[str, Any] | None,
    max_duration: float,
    keeper: KeeperConfig,
) -> tuple[float, ...]:
    """Ordered ranking key; larger is better.

    Length used to be the first element of this tuple, which made any copy a
    fraction of a second longer unbeatable however much better the other
    looked: a 640x352 MPEG-4 copy was recommended over a 1280x720 h264 one
    with twice the bitrate because it ran 30s longer. Completeness is now a
    penalty priced inside the quality score, so a small length difference can
    be outweighed while a real truncation still loses — and
    ``keeper.length_penalty_per_percent`` states the exchange rate rather than
    hiding it in the ordering.

    A copy that will not decode still cannot win, and duration remains the
    final tiebreak, so two otherwise identical copies keep preferring the
    longer one.
    """
    duration = float(probe["container_duration"] or 0.0) if probe else 0.0
    decode_ok = 1.0 if probe and probe["status"] == ProbeStatus.SUCCESS.value else 0.0

    score = _quality_stops(member, probe, keeper)
    if max_duration > 0 and 0 < duration < max_duration:
        shortfall_percent = (max_duration - duration) / max_duration * 100.0
        score -= shortfall_percent * keeper.length_penalty_per_percent

    ext = Path(str(member.get("file_name") or "")).suffix.lower()
    path_lower = str(member.get("original_path") or "").lower()
    preferred = 0.0
    if keeper.preferred_extensions and ext in [e.lower() for e in keeper.preferred_extensions]:
        preferred += 1.0
    preferred += sum(1.0 for kw in keeper.preferred_path_keywords if kw.lower() in path_lower)

    return (
        decode_ok,
        score,
        preferred,
        float(member.get("size") or 0),
        duration,
    )


def _describe(
    member: dict[str, Any],
    probe: dict[str, Any] | None,
    winner_probe: dict[str, Any] | None,
    max_duration: float,
) -> list[str]:
    """Human-readable reasons comparing a member against the keeper."""
    reasons: list[str] = []
    if probe and probe["status"] != ProbeStatus.SUCCESS.value:
        reasons.append(f"probe status: {probe['status']}")

    if probe and winner_probe:
        dur = float(probe["container_duration"] or 0.0)
        wdur = float(winner_probe["container_duration"] or 0.0)
        if wdur - dur > 0.5:
            reasons.append(f"less content ({dur:.2f}s vs {wdur:.2f}s)")
        elif dur - wdur > 0.5:
            # The longer copy lost on quality: say so, or the badge looks
            # like an oversight rather than a judgement
            reasons.append(f"{dur - wdur:.2f}s longer, but weaker quality")

        w = int(probe["primary_video_width"] or 0)
        h = int(probe["primary_video_height"] or 0)
        ww = int(winner_probe["primary_video_width"] or 0)
        wh = int(winner_probe["primary_video_height"] or 0)
        if ww * wh > w * h:
            reasons.append(f"lower resolution ({w}x{h} vs {ww}x{wh})")

        wb = float(winner_probe["container_bitrate"] or 0.0)
        b = float(probe["container_bitrate"] or 0.0)
        if wb > b * 1.5:
            reasons.append(f"lower bitrate ({b / 1e6:.1f} vs {wb / 1e6:.1f} Mbps)")

        loser_codec = str(probe["primary_video_codec"] or "").lower()
        winner_codec = str(winner_probe["primary_video_codec"] or "").lower()
        if _VIDEO_CODEC_RANK.get(loser_codec, 0) < _VIDEO_CODEC_RANK.get(winner_codec, 0):
            reasons.append(f"older codec ({loser_codec} vs {winner_codec})")
    return reasons

"""FFprobe integration for video metadata extraction."""

import contextlib
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

from akin_video_finder.core.models import (
    AudioStreamInfo,
    ContainerMetadata,
    FileMetadata,
    ProbeResult,
    ProbeStatus,
    VideoStreamInfo,
)
from akin_video_finder.metadata.process import kill_process_tree as _kill_process_tree


class FFprobeError(Exception):
    """Base exception for FFprobe errors."""

    pass


class FFprobeTimeoutError(FFprobeError):
    """Raised when FFprobe times out."""

    pass


class FFprobeExecutionError(FFprobeError):
    """Raised when FFprobe execution fails."""

    pass


class FFprobeParseError(FFprobeError):
    """Raised when FFprobe output cannot be parsed."""

    pass


PROBE_ALGORITHM_VERSION = "1.0"


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_rotation(ffprobe_data: dict[str, Any]) -> int | None:
    """Extract rotation from FFprobe data.

    Rotation can be in tags or side data.

    Args:
        ffprobe_data: Parsed FFprobe JSON data.

    Returns:
        Rotation in degrees, or None if not found.
    """
    # Check tags first
    tags = ffprobe_data.get("tags", {})
    if "rotate" in tags:
        try:
            return int(tags["rotate"])
        except (ValueError, TypeError):
            pass

    # ffprobe emits display-matrix rotation under side_data_list. Older
    # containers still write a tags.rotate value, handled above.
    side_data = ffprobe_data.get("side_data_list") or ffprobe_data.get("side_data", [])
    for item in side_data:
        if item.get("side_data_type") == "Display Matrix":
            rotation = item.get("rotation")
            if rotation is not None:
                try:
                    return int(rotation)
                except (ValueError, TypeError):
                    pass

    return None


def parse_video_stream(stream_data: dict[str, Any]) -> VideoStreamInfo:
    """Parse a video stream from FFprobe data.

    Args:
        stream_data: Stream data from FFprobe.

    Returns:
        VideoStreamInfo object.
    """
    rotation = parse_rotation(stream_data)
    width = stream_data.get("width")
    height = stream_data.get("height")
    if rotation in (90, 270) and width and height:
        # Store display dimensions so a portrait phone clip does not read as
        # landscape in the results.
        width, height = height, width
    return VideoStreamInfo(
        index=stream_data.get("index", 0),
        codec_name=stream_data.get("codec_name", "unknown"),
        codec_long_name=stream_data.get("codec_long_name"),
        width=width,
        height=height,
        coded_width=stream_data.get("coded_width"),
        coded_height=stream_data.get("coded_height"),
        sample_aspect_ratio=stream_data.get("sample_aspect_ratio"),
        display_aspect_ratio=stream_data.get("display_aspect_ratio"),
        avg_frame_rate=stream_data.get("avg_frame_rate"),
        r_frame_rate=stream_data.get("r_frame_rate"),
        pixel_format=stream_data.get("pix_fmt"),
        bits_per_raw_sample=stream_data.get("bits_per_raw_sample"),
        field_order=stream_data.get("field_order"),
        rotation=rotation,
        bitrate=stream_data.get("bit_rate"),
        duration=stream_data.get("duration"),
        frame_count=stream_data.get("nb_frames"),
    )


def parse_audio_stream(stream_data: dict[str, Any]) -> AudioStreamInfo:
    """Parse an audio stream from FFprobe data.

    Args:
        stream_data: Stream data from FFprobe.

    Returns:
        AudioStreamInfo object.
    """
    sample_rate_value = stream_data.get("sample_rate")
    sample_rate = _to_int(sample_rate_value)
    return AudioStreamInfo(
        index=stream_data.get("index", 0),
        codec_name=stream_data.get("codec_name", "unknown"),
        codec_long_name=stream_data.get("codec_long_name"),
        sample_rate=sample_rate,
        channel_count=stream_data.get("channels"),
        channel_layout=stream_data.get("channel_layout"),
        bitrate=stream_data.get("bit_rate"),
        duration=stream_data.get("duration"),
    )


def parse_ffprobe_output(ffprobe_json: str) -> FileMetadata:
    """Parse FFprobe JSON output into FileMetadata.

    Args:
        ffprobe_json: JSON string from FFprobe.

    Returns:
        FileMetadata object.

    Raises:
        FFprobeParseError: If JSON cannot be parsed.
    """
    try:
        data = json.loads(ffprobe_json)
    except json.JSONDecodeError as e:
        raise FFprobeParseError(f"Failed to parse FFprobe JSON: {e}") from e

    if not isinstance(data, dict):
        raise FFprobeParseError("FFprobe output is not a dictionary")

    # Parse format/container info
    format_data = data.get("format", {})
    container = ContainerMetadata(
        format_name=format_data.get("format_name", "unknown"),
        format_long_name=format_data.get("format_long_name"),
        duration=_to_float(format_data.get("duration")),
        size=format_data.get("size"),
        bitrate=format_data.get("bit_rate"),
        start_time=format_data.get("start_time"),
        tags=format_data.get("tags", {}),
    )

    # Parse streams
    streams = data.get("streams", [])
    video_streams: list[VideoStreamInfo] = []
    audio_streams: list[AudioStreamInfo] = []
    subtitle_count = 0

    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            video_streams.append(parse_video_stream(stream))
        elif codec_type == "audio":
            audio_streams.append(parse_audio_stream(stream))
        elif codec_type == "subtitle":
            subtitle_count += 1

    return FileMetadata(
        container=container,
        video_streams=video_streams,
        audio_streams=audio_streams,
        subtitle_stream_count=subtitle_count,
    )


def probe_file(
    file_path: Path,
    ffprobe_path: str = "ffprobe",
    timeout_seconds: int = 60,
    max_stderr_bytes: int = 8192,
) -> ProbeResult:
    """Probe a video file with FFprobe.

    Args:
        file_path: Path to the video file.
        ffprobe_path: Path to FFprobe executable.
        timeout_seconds: Timeout in seconds.
        max_stderr_bytes: Maximum stderr bytes to capture.

    Returns:
        ProbeResult with metadata or error information.

    Raises:
        FFprobeTimeoutError: If FFprobe times out.
        FFprobeExecutionError: If FFprobe execution fails.
    """
    args = [
        ffprobe_path,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-print_format",
        "json",
        str(file_path),
    ]

    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0,
        )

        timed_out = threading.Event()

        def _on_timeout() -> None:
            timed_out.set()
            _kill_process_tree(process)

        watchdog = threading.Timer(timeout_seconds, _on_timeout)
        watchdog.daemon = True
        watchdog.start()

        # Read stdout directly and cap stderr while it arrives. communicate()
        # buffered the whole stderr stream before slicing it, so a chatty
        # decoder could grow memory without bound.
        stderr_parts: list[bytes] = []
        stderr_total = 0

        def _read_stderr() -> None:
            nonlocal stderr_total
            stream = process.stderr
            if stream is None:
                return
            try:
                while chunk := stream.read(65536):
                    if stderr_total >= max_stderr_bytes:
                        continue
                    keep = chunk[: max_stderr_bytes - stderr_total]
                    stderr_parts.append(keep)
                    stderr_total += len(keep)
            except (OSError, ValueError):
                return

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()
        try:
            stdout = process.stdout.read() if process.stdout is not None else b""
        except (OSError, ValueError):
            stdout = b""
        with contextlib.suppress(OSError, ValueError):
            process.wait()
        stderr_thread.join(timeout=5)
        stderr = b"".join(stderr_parts)
        watchdog.cancel()

        if timed_out.is_set():
            return ProbeResult(
                status=ProbeStatus.TIMEOUT,
                error_category="timeout",
                error_message=f"FFprobe timed out after {timeout_seconds} seconds",
                algorithm_version=PROBE_ALGORITHM_VERSION,
            )

        exit_code = process.returncode
        stderr_text = stderr.decode("utf-8", errors="replace")[:max_stderr_bytes]

        if exit_code != 0:
            # Check if it's a no-video-stream error
            if (
                "no video stream" in stderr_text.lower()
                or "could not find codec parameters" in stderr_text.lower()
            ):
                return ProbeResult(
                    status=ProbeStatus.NO_VIDEO_STREAM,
                    error_category="no_video_stream",
                    error_message="File has no video stream",
                    stderr=stderr_text,
                    exit_code=exit_code,
                    algorithm_version=PROBE_ALGORITHM_VERSION,
                )

            return ProbeResult(
                status=ProbeStatus.FAILED,
                error_category="execution_error",
                error_message=f"FFprobe exited with code {exit_code}",
                stderr=stderr_text,
                exit_code=exit_code,
                algorithm_version=PROBE_ALGORITHM_VERSION,
            )

        # Parse the output
        try:
            metadata = parse_ffprobe_output(stdout.decode("utf-8"))

            if not metadata.has_video:
                return ProbeResult(
                    status=ProbeStatus.NO_VIDEO_STREAM,
                    error_category="no_video_stream",
                    error_message="File has no video stream",
                    stderr=stderr_text,
                    exit_code=exit_code,
                    algorithm_version=PROBE_ALGORITHM_VERSION,
                )

            return ProbeResult(
                status=ProbeStatus.SUCCESS,
                metadata=metadata,
                stderr=stderr_text,
                exit_code=exit_code,
                algorithm_version=PROBE_ALGORITHM_VERSION,
            )
        except FFprobeParseError as e:
            return ProbeResult(
                status=ProbeStatus.FAILED,
                error_category="parse_error",
                error_message=str(e),
                stderr=stderr_text,
                exit_code=exit_code,
                algorithm_version=PROBE_ALGORITHM_VERSION,
            )

    except subprocess.TimeoutExpired:
        _kill_process_tree(process)

        return ProbeResult(
            status=ProbeStatus.TIMEOUT,
            error_category="timeout",
            error_message=f"FFprobe timed out after {timeout_seconds} seconds",
            algorithm_version=PROBE_ALGORITHM_VERSION,
        )

    except FileNotFoundError:
        return ProbeResult(
            status=ProbeStatus.FAILED,
            error_category="executable_not_found",
            error_message=f"FFprobe executable not found: {ffprobe_path}",
            algorithm_version=PROBE_ALGORITHM_VERSION,
        )

    except OSError as e:
        return ProbeResult(
            status=ProbeStatus.FAILED,
            error_category="os_error",
            error_message=f"OS error during FFprobe execution: {e}",
            algorithm_version=PROBE_ALGORITHM_VERSION,
        )

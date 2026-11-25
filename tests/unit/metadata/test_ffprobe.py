"""Tests for FFprobe integration."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from akin_video_finder.metadata.ffprobe import (
    FFprobeParseError,
    ProbeStatus,
    _kill_process_tree,
    parse_audio_stream,
    parse_ffprobe_output,
    parse_rotation,
    parse_video_stream,
    probe_file,
)


class TestParseRotation:
    """Tests for parse_rotation function."""

    def test_rotation_in_tags(self) -> None:
        """Test rotation from tags."""
        data = {"tags": {"rotate": "90"}}
        assert parse_rotation(data) == 90

    def test_rotation_in_side_data(self) -> None:
        """Test rotation from side data."""
        data = {"side_data": [{"side_data_type": "Display Matrix", "rotation": "180"}]}
        assert parse_rotation(data) == 180

    def test_no_rotation(self) -> None:
        """Test no rotation present."""
        data = {}
        assert parse_rotation(data) is None

    def test_invalid_rotation_string(self) -> None:
        """Test invalid rotation string."""
        data = {"tags": {"rotate": "invalid"}}
        assert parse_rotation(data) is None


class TestParseVideoStream:
    """Tests for parse_video_stream function."""

    def test_basic_video_stream(self) -> None:
        """Test basic video stream parsing."""
        data = {
            "index": 0,
            "codec_name": "h264",
            "codec_long_name": "H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10",
            "width": 1920,
            "height": 1080,
            "r_frame_rate": "30/1",
        }
        stream = parse_video_stream(data)
        assert stream.index == 0
        assert stream.codec_name == "h264"
        assert stream.width == 1920
        assert stream.height == 1080
        assert stream.r_frame_rate == "30/1"

    def test_video_stream_with_defaults(self) -> None:
        """Test video stream with missing fields."""
        data = {"index": 0, "codec_name": "h264"}
        stream = parse_video_stream(data)
        assert stream.index == 0
        assert stream.codec_name == "h264"
        assert stream.width is None
        assert stream.height is None


class TestParseAudioStream:
    """Tests for parse_audio_stream function."""

    def test_basic_audio_stream(self) -> None:
        """Test basic audio stream parsing."""
        data = {
            "index": 1,
            "codec_name": "aac",
            "sample_rate": "48000",
            "channels": 2,
        }
        stream = parse_audio_stream(data)
        assert stream.index == 1
        assert stream.codec_name == "aac"
        assert stream.sample_rate == 48000
        assert stream.channel_count == 2


class TestParseFFprobeOutput:
    """Tests for parse_ffprobe_output function."""

    def test_valid_json(self) -> None:
        """Test parsing valid FFprobe JSON."""
        json_str = json.dumps(
            {
                "format": {
                    "format_name": "mp4",
                    "duration": "120.5",
                },
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "sample_rate": "48000",
                        "channels": 2,
                    },
                ],
            }
        )
        metadata = parse_ffprobe_output(json_str)
        assert metadata.container.format_name == "mp4"
        assert metadata.container.duration == 120.5
        assert len(metadata.video_streams) == 1
        assert len(metadata.audio_streams) == 1
        assert metadata.has_video is True

    def test_no_video_stream(self) -> None:
        """Test file with no video stream."""
        json_str = json.dumps(
            {
                "format": {"format_name": "mp3"},
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "audio",
                        "codec_name": "mp3",
                    }
                ],
            }
        )
        metadata = parse_ffprobe_output(json_str)
        assert metadata.has_video is False
        assert len(metadata.video_streams) == 0

    def test_invalid_json(self) -> None:
        """Test parsing invalid JSON."""
        with pytest.raises(FFprobeParseError, match="Failed to parse FFprobe JSON"):
            parse_ffprobe_output("not json")

    def test_not_a_dict(self) -> None:
        """Test JSON that is not a dict."""
        with pytest.raises(FFprobeParseError, match="not a dictionary"):
            parse_ffprobe_output(json.dumps(["list", "not", "dict"]))


class TestProbeFile:
    """Tests for probe_file function."""

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        """Test probing a non-existent file."""
        result = probe_file(tmp_path / "nonexistent.mp4")
        assert result.status == ProbeStatus.FAILED
        assert result.error_category == "execution_error"

    def test_regular_file_not_video(self, tmp_path: Path) -> None:
        """Test probing a non-video file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("not a video")
        result = probe_file(test_file)
        # Should fail or have no video stream
        assert result.status in (ProbeStatus.FAILED, ProbeStatus.NO_VIDEO_STREAM)


def test_kill_process_tree_stops_a_sleeping_child() -> None:
    """A timed-out probe must not leave its process alive."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _kill_process_tree(process)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_probe_timeout_kills_the_child(tmp_path: Path, monkeypatch) -> None:
    """The watchdog returns a timeout even when communicate does not.

    ``sys.executable`` stands in for ffprobe. Python reads ``-v``, then runs
    the file named ``error`` as the script, which sleeps past the timeout.
    """
    (tmp_path / "error").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = probe_file(tmp_path / "x.mp4", ffprobe_path=sys.executable, timeout_seconds=1)

    assert result.status == ProbeStatus.TIMEOUT
    assert result.error_category == "timeout"


def test_rotation_reads_side_data_list() -> None:
    """ffprobe emits display-matrix rotation under side_data_list."""
    assert (
        parse_rotation(
            {
                "side_data_list": [
                    {"side_data_type": "Display Matrix", "rotation": 90}
                ]
            }
        )
        == 90
    )


def test_portrait_rotation_swaps_display_dimensions() -> None:
    info = parse_video_stream(
        {
            "width": 1920,
            "height": 1080,
            "side_data_list": [{"side_data_type": "Display Matrix", "rotation": 90}],
        }
    )

    assert (info.width, info.height) == (1080, 1920)


def test_non_numeric_duration_does_not_fail_the_file() -> None:
    metadata = parse_ffprobe_output(
        json.dumps(
            {
                "format": {"format_name": "mp4", "duration": "N/A"},
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 16, "height": 16}
                ],
            }
        )
    )

    assert metadata.container.duration is None
    assert metadata.has_video


def test_non_numeric_sample_rate_is_none() -> None:
    info = parse_audio_stream({"codec_type": "audio", "sample_rate": "N/A"})

    assert info.sample_rate is None


def test_stderr_is_capped_while_reading(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "error").write_text(
        "import sys\nsys.stderr.write('x' * 100000)\nsys.exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = probe_file(
        tmp_path / "x.mp4",
        ffprobe_path=sys.executable,
        timeout_seconds=10,
        max_stderr_bytes=64,
    )

    assert result.status == ProbeStatus.FAILED
    assert len(result.stderr.encode("utf-8")) <= 64

"""Core domain models for Akin Video Finder."""

from dataclasses import dataclass, field
from enum import StrEnum


class ScanStatus(StrEnum):
    """Status of a scan run."""

    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class RootAvailability(StrEnum):
    """Availability state of a configured root."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class FilePresence(StrEnum):
    """Presence state of a file in the inventory."""

    PRESENT = "present"
    MISSING = "missing"


class ProbeStatus(StrEnum):
    """Status of a file probe operation."""

    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    NO_VIDEO_STREAM = "no_video_stream"
    PENDING = "pending"


@dataclass(frozen=True)
class VideoStreamInfo:
    """Information about a video stream."""

    index: int
    codec_name: str
    codec_long_name: str | None = None
    width: int | None = None
    height: int | None = None
    coded_width: int | None = None
    coded_height: int | None = None
    sample_aspect_ratio: str | None = None
    display_aspect_ratio: str | None = None
    avg_frame_rate: str | None = None
    r_frame_rate: str | None = None
    pixel_format: str | None = None
    bits_per_raw_sample: int | None = None
    field_order: str | None = None
    rotation: int | None = None
    bitrate: int | None = None
    duration: float | None = None
    frame_count: int | None = None


@dataclass(frozen=True)
class AudioStreamInfo:
    """Information about an audio stream."""

    index: int
    codec_name: str
    codec_long_name: str | None = None
    sample_rate: int | None = None
    channel_count: int | None = None
    channel_layout: str | None = None
    bitrate: int | None = None
    duration: float | None = None


@dataclass(frozen=True)
class ContainerMetadata:
    """Container-level metadata from FFprobe."""

    format_name: str
    format_long_name: str | None = None
    duration: float | None = None
    size: int | None = None
    bitrate: int | None = None
    start_time: float | None = None
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FileMetadata:
    """Complete metadata for a video file."""

    container: ContainerMetadata
    video_streams: list[VideoStreamInfo]
    audio_streams: list[AudioStreamInfo]
    subtitle_stream_count: int = 0

    @property
    def has_video(self) -> bool:
        """Check if the file has at least one video stream."""
        return len(self.video_streams) > 0

    @property
    def video_stream_count(self) -> int:
        """Get the number of video streams."""
        return len(self.video_streams)

    @property
    def audio_stream_count(self) -> int:
        """Get the number of audio streams."""
        return len(self.audio_streams)


@dataclass(frozen=True)
class ProbeResult:
    """Result of probing a file with FFprobe."""

    status: ProbeStatus
    metadata: FileMetadata | None = None
    error_category: str | None = None
    error_message: str | None = None
    stderr: str = ""
    exit_code: int | None = None
    algorithm_version: str = "1.0"


@dataclass(frozen=True)
class DiscoveredFile:
    """A file discovered during scanning."""

    root_id: int
    original_path: str
    normalized_path_key: str
    file_name: str
    extension: str
    size: int
    mtime_ns: int
    presence: FilePresence = FilePresence.PRESENT
    is_symlink: bool = False
    link_target: str | None = None


@dataclass(frozen=True)
class ProgressEvent:
    """Structured progress event for scan operations."""

    phase: str
    completed_count: int
    total_count: int | None = None
    current_root: str | None = None
    current_path: str | None = None
    # The other file of a pair, for phases that work on pairs: "comparing A"
    # leaves the reader asking with what, so the pair travels with the event
    peer_path: str | None = None
    cache_hit_count: int = 0
    probe_success_count: int = 0
    error_count: int = 0
    elapsed_seconds: float = 0.0
    message: str = ""


class CancellationToken:
    """Token for signalling cancellation of long-running operations."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Signal cancellation."""
        self._cancelled = True

    def is_cancelled(self) -> bool:
        """Check if cancellation has been requested."""
        return self._cancelled

    def reset(self) -> None:
        """Reset the cancellation state."""
        self._cancelled = False

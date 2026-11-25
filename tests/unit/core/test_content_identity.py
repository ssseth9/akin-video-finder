"""Content keys must identify the videos, not the disk they sit on.

An ignored pair is remembered by the two paths it named, which makes the
decision local to one copy of a library: the same videos under a mirror
root are different paths, so every false positive cleared in one copy came
back in the other. The content key is the part of the signature that says
"this video", so it matches across copies and never across different ones.
"""

from akin_video_finder.core.content_identity import KEY_LENGTH, content_key

HASHES = bytes(range(96))
STAMPS = "[0.0, 10.0, 20.0]"


def _key(
    version: str = "ahash8-v3",
    frames: int = 12,
    stamps: str | bytes = STAMPS,
    hashes: bytes = HASHES,
) -> str:
    return content_key(version, frames, stamps, hashes)


class TestContentKey:
    def test_a_copy_of_a_file_has_the_same_key(self) -> None:
        # Same bytes on another drive: identical signature, identical key
        assert _key() == _key()

    def test_different_frames_are_a_different_content(self) -> None:
        assert _key() != _key(hashes=bytes(96))

    def test_one_changed_frame_changes_the_key(self) -> None:
        # A single differing sample position is enough — the key is not a
        # coarse summary like "same size and duration"
        changed = bytearray(HASHES)
        changed[40] ^= 0xFF
        assert _key() != _key(hashes=bytes(changed))

    def test_the_sampling_that_produced_the_hashes_is_part_of_it(self) -> None:
        assert _key() != _key(version="ahash8-v4")
        assert _key() != _key(frames=17)
        assert _key() != _key(stamps="[0.0, 10.0]")

    def test_timestamps_may_be_text_or_bytes(self) -> None:
        # The column is TEXT, the extractor hands over str — but an old row
        # read back as bytes must not hash differently
        assert _key(stamps=STAMPS.encode()) == _key()

    def test_the_key_is_short_hex(self) -> None:
        key = _key()
        assert len(key) == KEY_LENGTH
        assert all(character in "0123456789abcdef" for character in key)

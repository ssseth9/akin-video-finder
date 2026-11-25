"""Content identity: the same video, wherever it happens to be stored.

An ignored pair used to be remembered by its two absolute paths, which made
the decision local to one copy of a library. A mirror drive keeps the same
files under a different root, so every false positive cleared in one copy
came back as a brand-new pair in the other and had to be cleared again.

Mirrored copies produce the same sparse content signature — the matcher
already relies on that to find them — so a short hash of that signature
identifies the *content* rather than the location, and one decision covers
every copy of those two videos.
"""

import hashlib
from typing import Final

# Hex characters kept from the digest. Sixteen is far more than a library
# of videos needs to stay unambiguous, and short enough to read in a query.
KEY_LENGTH: Final = 16


def content_key(
    algorithm_version: str,
    frame_count: int,
    timestamps: str | bytes,
    hashes: bytes,
) -> str:
    """Return a short, stable key for one file's content.

    Built from the sparse signature the matcher blocks candidates on, so two
    files share a key exactly when their signatures agree — a copy of a file,
    not a re-encode of it.

    Args:
        algorithm_version: Signature algorithm that produced the payload.
        frame_count: Number of sampled frames.
        timestamps: JSON array of sample positions, as text or bytes.
        hashes: Concatenated per-frame average hashes.

    Returns:
        ``KEY_LENGTH`` hexadecimal characters.
    """
    stamps = timestamps.encode() if isinstance(timestamps, str) else timestamps
    digest = hashlib.sha256()
    for part in (
        algorithm_version.encode(),
        b"|",
        str(frame_count).encode(),
        b"|",
        stamps,
        b"|",
        hashes,
    ):
        digest.update(part)
    return digest.hexdigest()[:KEY_LENGTH]

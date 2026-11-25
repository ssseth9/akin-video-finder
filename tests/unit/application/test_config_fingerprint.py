"""The scan fingerprint must describe what changes results — nothing else.

It exists so the GUI can say "settings changed since this scan". That claim
is only useful if a worker-count tweak does not trigger it and a loosened
matching threshold does. The payload grew from roots+extensions to the
matching thresholds too, so the version prefix separates old rows and lets
them be compared the way they were written.
"""

from dataclasses import replace

from akin_video_finder.application.scan_service import (
    config_fingerprint,
    fingerprint_matches,
    legacy_config_fingerprint,
)
from akin_video_finder.configuration.models import Config, RootConfig


def _config(**matching):
    cfg = Config(roots=[RootConfig(path="/library")])
    return replace(cfg, matching=replace(cfg.matching, **matching))


class TestConfigFingerprint:
    def test_is_versioned(self):
        assert config_fingerprint(_config()).startswith("v3:")

    def test_changes_with_a_matching_threshold(self):
        assert config_fingerprint(_config()) != config_fingerprint(_config(possible_threshold=0.6))

    def test_changes_with_the_duration_gate(self):
        # The gate decides which pairs exist at all, so a scan made under a
        # looser one is a different result set — the stale banner has to fire
        assert config_fingerprint(_config()) != config_fingerprint(_config(min_duration_ratio=0.5))

    def test_changes_with_the_roots(self):
        a = Config(roots=[RootConfig(path="/a")])
        b = Config(roots=[RootConfig(path="/b")])
        assert config_fingerprint(a) != config_fingerprint(b)

    def test_changes_with_scan_patterns(self):
        a = Config(roots=[RootConfig(path="/library")])
        b = replace(a, exclude_patterns=["_AI"])
        assert config_fingerprint(a) != config_fingerprint(b)

    def test_changes_with_the_video_extensions(self):
        cfg = _config()
        assert config_fingerprint(cfg) != config_fingerprint(
            replace(cfg, video_extensions=["*.mkv"])
        )

    def test_ignores_performance_and_tool_settings(self):
        # Worker counts and the ffmpeg path change how long a scan takes,
        # never what it finds — they must not mark results stale
        assert config_fingerprint(_config()) == config_fingerprint(
            _config(
                signature_workers=8, ffmpeg_path="C:/tools/ffmpeg.exe", frame_timeout_seconds=99
            )
        )


class TestFingerprintMatches:
    def test_current_config_matches(self):
        cfg = _config()
        assert fingerprint_matches(config_fingerprint(cfg), cfg) is True

    def test_changed_thresholds_do_not_match(self):
        stored = config_fingerprint(_config(possible_threshold=0.6))
        assert fingerprint_matches(stored, _config()) is False

    def test_legacy_hash_is_compared_the_way_it_was_written(self):
        cfg = _config()
        stored = legacy_config_fingerprint(cfg)
        assert fingerprint_matches(stored, cfg) is True
        # The old hash only covered roots and extensions
        assert fingerprint_matches(stored, _config(possible_threshold=0.6)) is True
        assert fingerprint_matches(stored, Config(roots=[RootConfig(path="/other")])) is False

    def test_missing_fingerprint_never_claims_staleness(self):
        assert fingerprint_matches(None, _config()) is True
        assert fingerprint_matches("", _config()) is True

#!/usr/bin/env python3
"""
Tests for special characters in directory paths and config files.

Covers:
  - YAML config with paths containing escape characters (!, spaces, unicode)
  - scan_directory() on directories with special characters in their names
  - _collect_file_paths() with special character directories
  - Config load_config() reading scan_dirs with special paths
"""

import os
import shutil
import sys
import tempfile

import pytest
import yaml

# Add parent to path for package imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from geosnag.cli import load_config
from geosnag.parallel import _collect_file_paths
from geosnag.scanner import scan_directory

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

# Special directory name patterns modelled on real NAS paths like:
# /volume1/homes/zheqd/Photos/nest-backup-Pictures/\!PhotoLibrary/2016/08
SPECIAL_DIR_NAMES = [
    "!PhotoLibrary",
    "photo library",
    "2016 - Summer",
    "Ñoño_fotos",
    "日本旅行",
    "folder with (parens)",
    "brackets[1]",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_jpeg(path: str) -> None:
    """Write a minimal valid JPEG (no EXIF) so scan_directory can find it."""
    with open(path, "wb") as f:
        # SOI + APP0 marker + EOI — enough to be recognized as a JPEG
        f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9")


def _write_config(path: str, scan_dirs: list) -> None:
    """Write a minimal config.yaml with the given scan_dirs list."""
    config = {"scan_dirs": scan_dirs}
    with open(path, "w") as f:
        yaml.dump(config, f)


# ---------------------------------------------------------------------------
# Config loading tests
# ---------------------------------------------------------------------------


class TestConfigSpecialPaths:
    """load_config() must preserve special characters in scan_dirs exactly."""

    def test_exclamation_mark_in_path(self, tmp_path):
        """Path with leading ! must not be mangled by YAML."""
        path = str(tmp_path / "!PhotoLibrary")
        cfg_file = str(tmp_path / "config.yaml")
        _write_config(cfg_file, [path])
        config = load_config(cfg_file)
        assert config["scan_dirs"] == [path]

    def test_escaped_exclamation_in_path(self, tmp_path):
        """Path written as \\!PhotoLibrary in YAML must round-trip cleanly."""
        raw_path = str(tmp_path / "!PhotoLibrary")
        cfg_file = str(tmp_path / "config.yaml")
        # Write the YAML manually with the path quoted (as a user would)
        with open(cfg_file, "w") as f:
            f.write(f"scan_dirs:\n  - '{raw_path}'\n")
        config = load_config(cfg_file)
        assert config["scan_dirs"] == [raw_path]

    def test_spaces_in_path(self, tmp_path):
        path = str(tmp_path / "photo library")
        cfg_file = str(tmp_path / "config.yaml")
        _write_config(cfg_file, [path])
        config = load_config(cfg_file)
        assert config["scan_dirs"] == [path]

    def test_unicode_in_path(self, tmp_path):
        path = str(tmp_path / "日本旅行")
        cfg_file = str(tmp_path / "config.yaml")
        _write_config(cfg_file, [path])
        config = load_config(cfg_file)
        assert config["scan_dirs"] == [path]

    def test_multiple_special_paths(self, tmp_path):
        paths = [
            str(tmp_path / "!PhotoLibrary"),
            str(tmp_path / "photo library"),
            str(tmp_path / "日本旅行"),
            str(tmp_path / "2016 - Summer"),
        ]
        cfg_file = str(tmp_path / "config.yaml")
        _write_config(cfg_file, paths)
        config = load_config(cfg_file)
        assert config["scan_dirs"] == paths

    def test_deeply_nested_special_path(self, tmp_path):
        """Synology-style path: /volume1/homes/user/Photos/!PhotoLibrary/2016/08"""
        path = str(tmp_path / "volume1" / "homes" / "user" / "Photos" / "!PhotoLibrary" / "2016" / "08")
        cfg_file = str(tmp_path / "config.yaml")
        _write_config(cfg_file, [path])
        config = load_config(cfg_file)
        assert config["scan_dirs"] == [path]

    def test_yaml_bang_unquoted_raises_or_parses(self, tmp_path):
        """
        Bare ! at the start of a YAML scalar is a tag indicator and causes a
        parse error in strict YAML. Verify load_config raises a clear error
        rather than silently returning a wrong value, so users know they must
        quote the path.
        """
        cfg_file = str(tmp_path / "config.yaml")
        # Write deliberately broken YAML with unquoted leading !
        with open(cfg_file, "w") as f:
            f.write("scan_dirs:\n  - !PhotoLibrary\n")
        with pytest.raises(Exception):
            load_config(cfg_file)

    def test_yaml_space_hash_in_path_must_be_quoted(self, tmp_path):
        """
        YAML treats ' #' (space then hash) as a comment — everything after it
        is silently dropped. The path '/photos/summer #2016' becomes '/photos/summer'.
        Single-quoted path must round-trip intact.

        Note: '#' directly after a non-space character (e.g. '/#recycle/') is
        safe unquoted — only ' #' (space-hash) triggers comment stripping.
        """
        # This path has a space before # — the dangerous pattern
        path_space_hash = "/volume1/photos/summer #2016"
        cfg_file = str(tmp_path / "config.yaml")

        # Unquoted: ' #2016' is stripped as a comment
        with open(cfg_file, "w") as f:
            f.write(f"scan_dirs:\n  - {path_space_hash}\n")
        config_unquoted = load_config(cfg_file)
        assert config_unquoted["scan_dirs"][0] != path_space_hash, (
            "Expected YAML to truncate unquoted path containing ' #'"
        )
        assert config_unquoted["scan_dirs"][0] == "/volume1/photos/summer"

        # Quoted: must round-trip intact
        with open(cfg_file, "w") as f:
            f.write(f"scan_dirs:\n  - '{path_space_hash}'\n")
        config_quoted = load_config(cfg_file)
        assert config_quoted["scan_dirs"][0] == path_space_hash

    def test_yaml_hash_after_slash_is_safe_unquoted(self, tmp_path):
        """
        '#' immediately after '/' is NOT a comment in YAML — it's safe unquoted.
        e.g. Synology's '*/#recycle/*' exclusion pattern and paths like
        '/volume1/#recycle/photos' parse correctly without quoting.
        """
        path_hash_after_slash = "/volume1/#recycle/photos"
        cfg_file = str(tmp_path / "config.yaml")
        with open(cfg_file, "w") as f:
            f.write(f"scan_dirs:\n  - {path_hash_after_slash}\n")
        config = load_config(cfg_file)
        assert config["scan_dirs"][0] == path_hash_after_slash

    def test_yaml_colon_space_in_path_must_be_quoted(self, tmp_path):
        """
        Unquoted path with ': ' is parsed as a YAML mapping, corrupting the value.
        Single-quoted path must round-trip intact.
        """
        path_with_colon = "/volume1/note: archive"
        cfg_file = str(tmp_path / "config.yaml")

        # Unquoted: parsed as a mapping key, not a string
        with open(cfg_file, "w") as f:
            f.write(f"scan_dirs:\n  - {path_with_colon}\n")
        config_unquoted = load_config(cfg_file)
        assert config_unquoted["scan_dirs"][0] != path_with_colon, (
            "Expected YAML to mangle unquoted path containing ': '"
        )

        # Quoted: must round-trip intact
        with open(cfg_file, "w") as f:
            f.write(f"scan_dirs:\n  - '{path_with_colon}'\n")
        config_quoted = load_config(cfg_file)
        assert config_quoted["scan_dirs"][0] == path_with_colon

    def test_spaces_in_path_safe_unquoted(self, tmp_path):
        """
        Spaces mid-path are handled correctly by YAML even without quoting,
        but single-quoting is still the recommended style for consistency.
        """
        path = str(tmp_path / "photo library")
        cfg_file = str(tmp_path / "config.yaml")

        # Unquoted spaces mid-path should still parse correctly
        with open(cfg_file, "w") as f:
            f.write(f"scan_dirs:\n  - {path}\n")
        config = load_config(cfg_file)
        assert config["scan_dirs"][0] == path


# ---------------------------------------------------------------------------
# scan_directory tests
# ---------------------------------------------------------------------------


class TestScanDirectorySpecialPaths:
    """scan_directory() must correctly traverse directories with special names."""

    def _setup_photo_dir(self, base: str, dir_name: str) -> tuple[str, str]:
        """Create <base>/<dir_name>/photo.jpg and return (dir_path, file_path)."""
        dir_path = os.path.join(base, dir_name)
        os.makedirs(dir_path, exist_ok=True)
        file_path = os.path.join(dir_path, "photo.jpg")
        _make_minimal_jpeg(file_path)
        return dir_path, file_path

    @pytest.mark.parametrize("dir_name", SPECIAL_DIR_NAMES)
    def test_scan_finds_photo_in_special_dir(self, tmp_path, dir_name):
        """scan_directory should find the photo regardless of dir name."""
        dir_path, _ = self._setup_photo_dir(str(tmp_path), dir_name)
        results = scan_directory(dir_path, {".jpg"})
        assert len(results) == 1, f"Expected 1 photo in '{dir_name}', got {len(results)}"
        assert results[0].extension == ".jpg"

    def test_scan_exclamation_nested(self, tmp_path):
        """Synology !PhotoLibrary nested two levels deep."""
        base = str(tmp_path)
        nested = os.path.join(base, "!PhotoLibrary", "2016", "08")
        os.makedirs(nested)
        file_path = os.path.join(nested, "photo.jpg")
        _make_minimal_jpeg(file_path)

        results = scan_directory(os.path.join(base, "!PhotoLibrary"), {".jpg"}, recursive=True)
        assert len(results) == 1
        assert results[0].filepath == file_path

    def test_scan_preserves_full_path(self, tmp_path, dir_name="!PhotoLibrary"):
        """PhotoMeta.filepath must contain the exact original path."""
        dir_path, file_path = self._setup_photo_dir(str(tmp_path), dir_name)
        results = scan_directory(dir_path, {".jpg"})
        assert results[0].filepath == file_path

    def test_scan_non_recursive_special_dir(self, tmp_path):
        """Non-recursive scan on a special-char dir must still find top-level files."""
        dir_path, _ = self._setup_photo_dir(str(tmp_path), "!PhotoLibrary")
        results = scan_directory(dir_path, {".jpg"}, recursive=False)
        assert len(results) == 1

    def test_scan_multiple_special_dirs(self, tmp_path):
        """scan_directory across siblings with special names."""
        base = str(tmp_path)
        for name in ["!PhotoLibrary", "photo library", "2016 - Summer"]:
            d = os.path.join(base, name)
            os.makedirs(d)
            _make_minimal_jpeg(os.path.join(d, "photo.jpg"))

        # Scan the parent — recursive
        results = scan_directory(base, {".jpg"}, recursive=True)
        assert len(results) == 3

    def test_scan_real_fixture_nef(self):
        """Existing fixture still scans correctly (sanity check)."""
        nef_path = os.path.join(FIXTURES_DIR, "camera_no_gps.nef")
        if not os.path.exists(nef_path):
            pytest.skip("Fixture not found")
        d = os.path.dirname(nef_path)
        results = scan_directory(d, {".nef"})
        assert any(r.extension == ".nef" for r in results)


# ---------------------------------------------------------------------------
# _collect_file_paths tests
# ---------------------------------------------------------------------------


class TestCollectFilePathsSpecialPaths:
    """_collect_file_paths() must handle special-character directories."""

    @pytest.mark.parametrize("dir_name", SPECIAL_DIR_NAMES)
    def test_collect_finds_files(self, tmp_path, dir_name):
        d = os.path.join(str(tmp_path), dir_name)
        os.makedirs(d)
        _make_minimal_jpeg(os.path.join(d, "photo.jpg"))
        paths = _collect_file_paths([d], {".jpg"}, recursive=True, exclude_patterns=[])
        assert len(paths) == 1
        assert paths[0].endswith("photo.jpg")

    def test_collect_exclamation_nested(self, tmp_path):
        nested = os.path.join(str(tmp_path), "!PhotoLibrary", "2016", "08")
        os.makedirs(nested)
        _make_minimal_jpeg(os.path.join(nested, "IMG_001.jpg"))
        paths = _collect_file_paths(
            [os.path.join(str(tmp_path), "!PhotoLibrary")],
            {".jpg"},
            recursive=True,
            exclude_patterns=[],
        )
        assert len(paths) == 1
        assert "!PhotoLibrary" in paths[0]

    def test_collect_multiple_special_scan_dirs(self, tmp_path):
        """Multiple scan_dirs each with special chars."""
        scan_dirs = []
        for name in ["!PhotoLibrary", "photo library"]:
            d = os.path.join(str(tmp_path), name)
            os.makedirs(d)
            _make_minimal_jpeg(os.path.join(d, "photo.jpg"))
            scan_dirs.append(d)
        paths = _collect_file_paths(scan_dirs, {".jpg"}, recursive=True, exclude_patterns=[])
        assert len(paths) == 2

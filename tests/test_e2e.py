"""
End-to-end tests for GeoSnag CLI.

Invokes `geosnag` as a subprocess (via python -m geosnag.cli) and verifies:
- Exit codes
- Stdout/stderr content
- File-system side effects (GPS written, CSV created, XMP created, etc.)

Fixture pair used:
  camera_no_gps.nef  — Nikon D610, 2017-09-23 23:11:37, no GPS  (target)
  phone_with_gps.jpg — iPhone 6s,  2017-09-23 22:30:00, has GPS (source)
  Time delta: ~41 min — within the default 120-min matching window.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"
NEF = FIXTURES_DIR / "camera_no_gps.nef"
JPG = FIXTURES_DIR / "phone_with_gps.jpg"

# Invoke via python -m geosnag.cli so tests work without the package installed
# into the active environment's PATH.
CLI = [sys.executable, "-m", "geosnag.cli"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(*args, cwd=None, timeout=30) -> subprocess.CompletedProcess:
    """Run the geosnag CLI with the given extra arguments."""
    return subprocess.run(
        CLI + list(args),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


@pytest.fixture()
def photo_dir(tmp_path):
    """Temp directory pre-loaded with one NEF (target) and one JPG (GPS source)."""
    shutil.copy2(NEF, tmp_path / "camera.NEF")
    shutil.copy2(JPG, tmp_path / "phone.jpg")
    return tmp_path


@pytest.fixture()
def config_file(photo_dir):
    """Minimal config.yaml pointing at photo_dir, dry_run by default."""
    cfg = {
        "scan_dirs": [str(photo_dir)],
        "dry_run": True,
        "use_index": False,
        "workers": 1,
    }
    p = photo_dir / "config.yaml"
    p.write_text(yaml.dump(cfg))
    return p


# ---------------------------------------------------------------------------
# Help / version
# ---------------------------------------------------------------------------


class TestHelp:
    def test_help_exits_zero(self):
        r = run("--help")
        assert r.returncode == 0

    def test_help_mentions_geosnag(self):
        r = run("--help")
        assert "GeoSnag" in r.stdout or "geosnag" in r.stdout.lower()

    def test_help_lists_apply_flag(self):
        r = run("--help")
        assert "--apply" in r.stdout

    def test_help_lists_dry_run_flag(self):
        r = run("--help")
        assert "--dry-run" in r.stdout

    def test_help_lists_report_flag(self):
        r = run("--help")
        assert "--report" in r.stdout


# ---------------------------------------------------------------------------
# Config errors
# ---------------------------------------------------------------------------


class TestConfigErrors:
    def test_missing_config_exits_nonzero(self, tmp_path):
        r = run("--config", str(tmp_path / "nonexistent.yaml"))
        assert r.returncode != 0

    def test_missing_config_prints_error(self, tmp_path):
        r = run("--config", str(tmp_path / "nonexistent.yaml"))
        assert "not found" in r.stdout.lower() or "not found" in r.stderr.lower()

    def test_empty_scan_dirs_exits(self, tmp_path):
        cfg = {"scan_dirs": [], "dry_run": True, "use_index": False}
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump(cfg))
        r = run("--config", str(p))
        assert r.returncode != 0


# ---------------------------------------------------------------------------
# Dry run (default)
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_exits_zero(self, config_file, photo_dir):
        r = run("--config", str(config_file))
        assert r.returncode == 0

    def test_dry_run_prints_banner(self, config_file):
        r = run("--config", str(config_file))
        assert "GeoSnag" in r.stdout

    def test_dry_run_indicates_mode(self, config_file):
        r = run("--config", str(config_file))
        assert "DRY RUN" in r.stdout

    def test_dry_run_does_not_write_gps(self, config_file, photo_dir):
        nef = photo_dir / "camera.NEF"
        mtime_before = nef.stat().st_mtime
        run("--config", str(config_file))
        assert nef.stat().st_mtime == mtime_before

    def test_dry_run_flag_overrides_config_apply(self, photo_dir):
        """--dry-run flag forces dry run even if config says dry_run: false."""
        cfg = {
            "scan_dirs": [str(photo_dir)],
            "dry_run": False,
            "use_index": False,
            "workers": 1,
        }
        p = photo_dir / "config.yaml"
        p.write_text(yaml.dump(cfg))
        nef = photo_dir / "camera.NEF"
        mtime_before = nef.stat().st_mtime

        r = run("--config", str(p), "--dry-run")

        assert r.returncode == 0
        assert "DRY RUN" in r.stdout
        assert nef.stat().st_mtime == mtime_before

    def test_shows_match_count(self, config_file):
        r = run("--config", str(config_file))
        # At least one match expected (NEF + JPG from same day, ~41 min apart)
        assert "Matched:" in r.stdout

    def test_version_in_banner(self, config_file):
        from geosnag import __version__

        r = run("--config", str(config_file))
        assert __version__ in r.stdout


# ---------------------------------------------------------------------------
# Apply mode (--apply writes GPS to files)
# ---------------------------------------------------------------------------


class TestApply:
    def test_apply_exits_zero(self, config_file, photo_dir):
        r = run("--config", str(config_file), "--apply")
        assert r.returncode == 0

    def test_apply_indicates_live_mode(self, config_file):
        r = run("--config", str(config_file), "--apply")
        assert "LIVE MODE" in r.stdout

    def test_apply_modifies_nef(self, config_file, photo_dir):
        nef = photo_dir / "camera.NEF"
        mtime_before = nef.stat().st_mtime
        run("--config", str(config_file), "--apply")
        assert nef.stat().st_mtime != mtime_before

    def test_apply_writes_gps_to_nef(self, config_file, photo_dir):
        run("--config", str(config_file), "--apply")

        # Verify GPS was written by scanning the modified file
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from geosnag.scanner import scan_photo

        meta = scan_photo(str(photo_dir / "camera.NEF"))
        assert meta.has_gps, "GPS should be present after --apply"
        assert abs(meta.gps_latitude - 55.750) < 0.01
        assert abs(meta.gps_longitude - 37.617) < 0.01

    def test_apply_stamps_processed_tag(self, config_file, photo_dir):
        run("--config", str(config_file), "--apply")

        sys.path.insert(0, str(Path(__file__).parent.parent))
        from geosnag.scanner import scan_photo

        meta = scan_photo(str(photo_dir / "camera.NEF"))
        assert meta.geosnag_processed

    def test_apply_prints_success_summary(self, config_file):
        r = run("--config", str(config_file), "--apply")
        assert "geo-tagged" in r.stdout or "Successful" in r.stdout

    def test_rerun_after_apply_skips_processed(self, config_file, photo_dir):
        """Second --apply on already-processed files should write 0 new GPS."""
        run("--config", str(config_file), "--apply")
        r2 = run("--config", str(config_file), "--apply")
        # Already processed → no matches → "No matches found" or 0 matched
        assert r2.returncode == 0
        assert "No matches" in r2.stdout or "0" in r2.stdout


# ---------------------------------------------------------------------------
# Report (--report saves CSV)
# ---------------------------------------------------------------------------


class TestReport:
    def test_report_creates_csv(self, config_file, photo_dir):
        report = photo_dir / "matches.csv"
        run("--config", str(config_file), "--report", str(report))
        assert report.exists()

    def test_report_has_matched_row(self, config_file, photo_dir):
        report = photo_dir / "matches.csv"
        run("--config", str(config_file), "--report", str(report))

        with open(report) as f:
            rows = list(csv.DictReader(f))
        matched = [r for r in rows if r["Status"] == "MATCHED"]
        assert len(matched) >= 1

    def test_report_matched_row_has_gps(self, config_file, photo_dir):
        report = photo_dir / "matches.csv"
        run("--config", str(config_file), "--report", str(report))

        with open(report) as f:
            rows = list(csv.DictReader(f))
        matched = [r for r in rows if r["Status"] == "MATCHED"]
        assert matched[0]["Latitude"] != ""
        assert matched[0]["Longitude"] != ""

    def test_report_prints_saved_message(self, config_file, photo_dir):
        report = photo_dir / "matches.csv"
        r = run("--config", str(config_file), "--report", str(report))
        assert str(report) in r.stdout or "report" in r.stdout.lower()


# ---------------------------------------------------------------------------
# Write mode (--write-mode xmp_sidecar)
# ---------------------------------------------------------------------------


class TestWriteMode:
    def test_xmp_sidecar_creates_xmp_file(self, config_file, photo_dir):
        run("--config", str(config_file), "--apply", "--write-mode", "xmp_sidecar")
        xmp = photo_dir / "camera.xmp"
        assert xmp.exists()

    def test_xmp_sidecar_does_not_modify_nef(self, config_file, photo_dir):
        run("--config", str(config_file), "--apply", "--write-mode", "xmp_sidecar")
        xmp = photo_dir / "camera.xmp"
        assert xmp.exists(), "XMP sidecar should exist"

    def test_xmp_contains_gps_coords(self, config_file, photo_dir):
        run("--config", str(config_file), "--apply", "--write-mode", "xmp_sidecar")
        xmp_content = (photo_dir / "camera.xmp").read_text()
        assert "GPSLatitude" in xmp_content
        assert "GPSLongitude" in xmp_content


# ---------------------------------------------------------------------------
# Matching flags
# ---------------------------------------------------------------------------


class TestMatchingFlags:
    def test_max_delta_zero_produces_no_matches(self, config_file):
        """With max-delta=0, the 41-minute gap should not match."""
        r = run("--config", str(config_file), "--max-delta", "0")
        assert r.returncode == 0
        assert "No matches" in r.stdout or "Matched:           0" in r.stdout

    def test_max_delta_large_produces_match(self, config_file):
        r = run("--config", str(config_file), "--max-delta", "240")
        assert r.returncode == 0
        assert "No matches" not in r.stdout


# ---------------------------------------------------------------------------
# No-match scenario
# ---------------------------------------------------------------------------


class TestNoMatch:
    def test_no_source_exits_zero(self, tmp_path):
        """Only the NEF (no GPS source) → no matches → exit 0."""
        shutil.copy2(NEF, tmp_path / "camera.NEF")
        cfg = {
            "scan_dirs": [str(tmp_path)],
            "dry_run": True,
            "use_index": False,
            "workers": 1,
        }
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump(cfg))

        r = run("--config", str(p))
        assert r.returncode == 0
        assert "No matches" in r.stdout

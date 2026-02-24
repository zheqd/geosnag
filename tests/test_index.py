"""
Tests for ScanIndex (cache) and parallel scanning.

Covers:
  - ScanIndex: load, save, lookup, update, prune, clear
  - Cache invalidation via mtime/size changes
  - Corrupt/missing index file handling
  - Version mismatch handling
  - Parallel scan_with_index: cache hits, cache misses, mixed
  - Thread pool integration
  - Integration with real sample files
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta

import pytest

from geosnag.index import ScanIndex, _entry_to_photo, _photo_to_entry
from geosnag.parallel import _collect_file_paths, scan_with_index
from geosnag.scanner import PHOTO_EXTS, PhotoMeta

SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")

needs_samples = pytest.mark.skipif(
    not os.path.exists(os.path.join(SAMPLES_DIR, "NIK_7953.NEF")),
    reason="Sample NEF not available",
)


def _has_pyexiv2_and_pil() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("pyexiv2") is not None and importlib.util.find_spec("PIL") is not None
    except (ImportError, ModuleNotFoundError):
        return False


needs_pyexiv2_pil = pytest.mark.skipif(
    not _has_pyexiv2_and_pil(),
    reason="pyexiv2/PIL not available",
)


def _create_test_jpeg(path):
    """Create a minimal JPEG file for testing."""
    try:
        from PIL import Image

        img = Image.new("RGB", (10, 10), color="red")
        img.save(path, "JPEG")
        img.close()
        return True
    except ImportError:
        # Fallback: write minimal JPEG bytes
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9")
        return True


def _create_test_jpeg_with_exif(path, dt_str="2023:06:15 14:30:00", gps=None):
    """Create a JPEG with EXIF metadata for testing."""
    try:
        import pyexiv2
        from PIL import Image

        img = Image.new("RGB", (10, 10), color="blue")
        img.save(path, "JPEG")
        img.close()

        exif_data = {
            "Exif.Photo.DateTimeOriginal": dt_str,
            "Exif.Image.Make": "TestCam",
            "Exif.Image.Model": "Model1",
        }

        if gps:
            lat, lon = gps
            lat_ref = "N" if lat >= 0 else "S"
            lon_ref = "E" if lon >= 0 else "W"
            lat_abs = abs(lat)
            lon_abs = abs(lon)
            lat_d = int(lat_abs)
            lat_m = int((lat_abs - lat_d) * 60)
            lat_s = int(((lat_abs - lat_d) * 60 - lat_m) * 60 * 10000)
            lon_d = int(lon_abs)
            lon_m = int((lon_abs - lon_d) * 60)
            lon_s = int(((lon_abs - lon_d) * 60 - lon_m) * 60 * 10000)
            exif_data.update(
                {
                    "Exif.GPSInfo.GPSVersionID": "2 3 0 0",
                    "Exif.GPSInfo.GPSLatitudeRef": lat_ref,
                    "Exif.GPSInfo.GPSLatitude": f"{lat_d}/1 {lat_m}/1 {lat_s}/10000",
                    "Exif.GPSInfo.GPSLongitudeRef": lon_ref,
                    "Exif.GPSInfo.GPSLongitude": f"{lon_d}/1 {lon_m}/1 {lon_s}/10000",
                }
            )

        pimg = pyexiv2.Image(path)
        pimg.modify_exif(exif_data)
        pimg.close()
        return True
    except ImportError:
        return False


# ─── ScanIndex Unit Tests ───────────────────────────────────────────────


class TestScanIndexEmpty:
    """Test fresh index behavior."""

    def test_size_is_zero(self, tmp_path):
        idx = ScanIndex(str(tmp_path / "test_index.json"))
        assert idx.size == 0

    def test_load_returns_zero(self, tmp_path):
        idx = ScanIndex(str(tmp_path / "test_index.json"))
        assert idx.load() == 0

    def test_lookup_returns_none(self, tmp_path):
        idx = ScanIndex(str(tmp_path / "test_index.json"))
        assert idx.lookup("/nonexistent/file.jpg") is None


class TestScanIndexSaveLoad:
    """Test save and load round-trip."""

    def test_round_trip(self, tmp_path):
        idx_path = str(tmp_path / "test_index.json")
        test_file = str(tmp_path / "photo.jpg")
        _create_test_jpeg(test_file)

        idx = ScanIndex(idx_path)
        meta = PhotoMeta(
            filepath=test_file,
            filename="photo.jpg",
            extension=".jpg",
            datetime_original=datetime(2023, 6, 15, 14, 30, 0),
            has_gps=True,
            gps_latitude=55.7539,
            gps_longitude=37.6208,
            gps_altitude=150.0,
            camera_make="NIKON CORPORATION",
            camera_model="NIKON D610",
            geosnag_processed=False,
        )
        idx.update(meta)
        assert idx._dirty is True
        idx.save()

        assert os.path.exists(idx_path)
        assert idx._dirty is False

        # Load in new instance
        idx2 = ScanIndex(idx_path)
        count = idx2.load()
        assert count == 1

        cached = idx2.lookup(test_file)
        assert cached is not None
        assert cached.datetime_original == datetime(2023, 6, 15, 14, 30, 0)
        assert cached.has_gps is True
        assert abs(cached.gps_latitude - 55.7539) < 0.0001
        assert abs(cached.gps_longitude - 37.6208) < 0.0001
        assert abs(cached.gps_altitude - 150.0) < 0.1
        assert cached.camera_make == "NIKON CORPORATION"
        assert cached.camera_model == "NIKON D610"
        assert cached.extension == ".jpg"
        assert cached.filename == "photo.jpg"


class TestScanIndexMtimeInvalidation:
    """Test that changed mtime invalidates cache."""

    def test_cache_miss_after_modification(self, tmp_path):
        idx_path = str(tmp_path / "test_index.json")
        test_file = str(tmp_path / "photo.jpg")
        _create_test_jpeg(test_file)

        idx = ScanIndex(idx_path)
        meta = PhotoMeta(
            filepath=test_file,
            filename="photo.jpg",
            extension=".jpg",
            datetime_original=datetime(2023, 1, 1, 12, 0, 0),
        )
        idx.update(meta)
        idx.save()

        idx2 = ScanIndex(idx_path)
        idx2.load()
        assert idx2.lookup(test_file) is not None

        # Modify the file (changes mtime)
        time.sleep(0.05)
        with open(test_file, "ab") as f:
            f.write(b"\x00" * 10)

        assert idx2.lookup(test_file) is None


class TestScanIndexSizeInvalidation:
    """Test that changed file size invalidates cache."""

    def test_cache_miss_after_size_change(self, tmp_path):
        idx_path = str(tmp_path / "test_index.json")
        test_file = str(tmp_path / "photo.jpg")
        _create_test_jpeg(test_file)

        idx = ScanIndex(idx_path)
        meta = PhotoMeta(filepath=test_file, filename="photo.jpg", extension=".jpg")
        idx.update(meta)

        assert idx.lookup(test_file) is not None

        # Change file but preserve mtime
        orig_mtime = os.path.getmtime(test_file)
        with open(test_file, "ab") as f:
            f.write(b"\x00" * 100)
        os.utime(test_file, (orig_mtime, orig_mtime))

        assert idx.lookup(test_file) is None


class TestScanIndexDeletedFile:
    """Test lookup for a deleted file."""

    def test_lookup_returns_none(self, tmp_path):
        idx_path = str(tmp_path / "test_index.json")
        test_file = str(tmp_path / "photo.jpg")
        _create_test_jpeg(test_file)

        idx = ScanIndex(idx_path)
        meta = PhotoMeta(filepath=test_file, filename="photo.jpg", extension=".jpg")
        idx.update(meta)
        os.remove(test_file)

        assert idx.lookup(test_file) is None


class TestScanIndexPrune:
    """Test pruning stale entries."""

    def test_removes_stale_entries(self, tmp_path):
        idx_path = str(tmp_path / "test_index.json")
        idx = ScanIndex(idx_path)

        for i in range(5):
            idx.entries[f"/fake/photo_{i}.jpg"] = {"mtime": 0, "size": 0}

        real_file = str(tmp_path / "real.jpg")
        _create_test_jpeg(real_file)
        meta = PhotoMeta(filepath=real_file, filename="real.jpg", extension=".jpg")
        idx.update(meta)

        assert idx.size == 6
        removed = idx.prune({real_file})
        assert removed == 5
        assert idx.size == 1
        assert real_file in idx.entries


class TestScanIndexClear:
    """Test clearing all entries."""

    def test_clear_resets(self, tmp_path):
        idx_path = str(tmp_path / "test_index.json")
        idx = ScanIndex(idx_path)

        for i in range(3):
            idx.entries[f"/fake/{i}.jpg"] = {}
        idx._dirty = True
        idx.save()

        idx.clear()
        assert idx.size == 0
        assert idx._dirty is True


class TestScanIndexCorruptFile:
    """Test loading a corrupt index file."""

    def test_load_returns_zero(self, tmp_path):
        idx_path = str(tmp_path / "test_index.json")
        with open(idx_path, "w") as f:
            f.write("this is not json {{{")

        idx = ScanIndex(idx_path)
        assert idx.load() == 0
        assert idx.size == 0


class TestScanIndexVersionMismatch:
    """Test loading an index with wrong version."""

    def test_load_clears_entries(self, tmp_path):
        idx_path = str(tmp_path / "test_index.json")
        with open(idx_path, "w") as f:
            json.dump({"version": 1, "entries": {"a.jpg": {}}}, f)

        idx = ScanIndex(idx_path)
        assert idx.load() == 0
        assert idx.size == 0


class TestScanIndexNoDirtySave:
    """Test that save is skipped when nothing changed."""

    def test_file_not_rewritten(self, tmp_path):
        idx_path = str(tmp_path / "test_index.json")
        idx = ScanIndex(idx_path)
        idx.entries = {"a.jpg": {}}
        idx._dirty = True
        idx.save()

        mtime_before = os.path.getmtime(idx_path)
        time.sleep(0.05)

        idx2 = ScanIndex(idx_path)
        idx2.load()
        idx2.save()  # should skip (not dirty)

        assert os.path.getmtime(idx_path) == mtime_before


class TestScanIndexSerializationRoundtrip:
    """Test _photo_to_entry / _entry_to_photo roundtrip."""

    def test_all_fields_preserved(self, tmp_path):
        test_file = str(tmp_path / "photo.nef")
        with open(test_file, "wb") as f:
            f.write(b"\x00" * 50)

        original = PhotoMeta(
            filepath=test_file,
            filename="photo.nef",
            extension=".nef",
            datetime_original=datetime(2023, 12, 25, 10, 30, 45),
            has_gps=True,
            gps_latitude=-33.8688,
            gps_longitude=151.2093,
            gps_altitude=42.5,
            camera_make="Sony",
            camera_model="A7III",
            geosnag_processed=True,
            scan_error=None,
        )

        entry = _photo_to_entry(original)
        restored = _entry_to_photo(test_file, entry)

        assert restored.filepath == original.filepath
        assert restored.filename == original.filename
        assert restored.extension == original.extension
        assert restored.datetime_original == original.datetime_original
        assert restored.has_gps == original.has_gps
        assert restored.gps_latitude == original.gps_latitude
        assert restored.gps_longitude == original.gps_longitude
        assert restored.gps_altitude == original.gps_altitude
        assert restored.camera_make == original.camera_make
        assert restored.camera_model == original.camera_model
        assert restored.geosnag_processed == original.geosnag_processed
        assert restored.scan_error == original.scan_error


# ─── _collect_file_paths Tests ──────────────────────────────────────────


class TestCollectFilePaths:
    """Test file path collection."""

    def test_recursive(self, tmp_path):
        subdir = tmp_path / "sub"
        subdir.mkdir()

        for name in ["a.jpg", "b.nef", "c.txt", "d.arw"]:
            (tmp_path / name).write_bytes(b"\x00")
        (subdir / "e.jpg").write_bytes(b"\x00")

        paths = _collect_file_paths([str(tmp_path)], PHOTO_EXTS, recursive=True)
        photos = [p for p in paths if os.path.splitext(p)[1].lower() in PHOTO_EXTS]
        assert len(photos) == 4

    def test_non_recursive(self, tmp_path):
        subdir = tmp_path / "sub"
        subdir.mkdir()

        for name in ["a.jpg", "b.nef", "c.txt", "d.arw"]:
            (tmp_path / name).write_bytes(b"\x00")
        (subdir / "e.jpg").write_bytes(b"\x00")

        paths = _collect_file_paths([str(tmp_path)], PHOTO_EXTS, recursive=False)
        assert len(paths) == 3

    def test_exclude_pattern(self, tmp_path):
        subdir = tmp_path / "sub"
        subdir.mkdir()

        for name in ["a.jpg", "b.nef", "c.txt", "d.arw"]:
            (tmp_path / name).write_bytes(b"\x00")
        (subdir / "e.jpg").write_bytes(b"\x00")

        paths = _collect_file_paths(
            [str(tmp_path)],
            PHOTO_EXTS,
            recursive=True,
            exclude_patterns=["sub/*"],
        )
        assert len(paths) == 3

    def test_nonexistent_directory(self):
        paths = _collect_file_paths(["/nonexistent/dir"], PHOTO_EXTS)
        assert len(paths) == 0


# ─── scan_with_index Integration Tests ──────────────────────────────────


@needs_samples
class TestScanWithIndexNoCache:
    """Test scan_with_index without an index (all misses)."""

    def test_finds_photo(self, tmp_path):
        nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
        test_nef = str(tmp_path / "test.NEF")
        shutil.copy2(nef_path, test_nef)

        photos = scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef"},
            index=None,
            workers=2,
        )

        assert len(photos) == 1
        assert photos[0].datetime_original is not None
        assert photos[0].camera_make == "NIKON CORPORATION"


@needs_samples
class TestScanWithIndexCacheHit:
    """Test scan_with_index with index (second run = all cache hits)."""

    def test_cache_produces_same_results(self, tmp_path):
        nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
        test_nef = str(tmp_path / "test.NEF")
        shutil.copy2(nef_path, test_nef)

        idx_path = str(tmp_path / "index.json")
        index = ScanIndex(idx_path)

        photos1 = scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef"},
            index=index,
            workers=2,
        )
        index.save()
        assert len(photos1) == 1
        assert index.size == 1

        index2 = ScanIndex(idx_path)
        index2.load()
        photos2 = scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef"},
            index=index2,
            workers=2,
        )

        assert len(photos2) == 1
        assert photos1[0].datetime_original == photos2[0].datetime_original
        assert photos1[0].camera_make == photos2[0].camera_make
        assert photos1[0].has_gps == photos2[0].has_gps


@needs_samples
class TestScanWithIndexMixed:
    """Test scan_with_index with mix of cached and new files."""

    def test_mixed_cache_and_new(self, tmp_path):
        nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
        test_nef = str(tmp_path / "test.NEF")
        shutil.copy2(nef_path, test_nef)

        idx_path = str(tmp_path / "index.json")
        index = ScanIndex(idx_path)
        scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef", ".jpg"},
            index=index,
            workers=2,
        )
        index.save()

        # Add a new JPEG file
        new_jpg = str(tmp_path / "new_photo.jpg")
        _create_test_jpeg(new_jpg)

        index2 = ScanIndex(idx_path)
        index2.load()
        photos2 = scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef", ".jpg"},
            index=index2,
            workers=2,
        )

        assert len(photos2) == 2
        nef_results = [p for p in photos2 if p.extension == ".nef"]
        jpg_results = [p for p in photos2 if p.extension == ".jpg"]
        assert len(nef_results) == 1
        assert len(jpg_results) == 1
        assert nef_results[0].datetime_original == datetime(2017, 9, 23, 23, 11, 37)


@needs_samples
class TestScanWithIndexPruneStale:
    """Test that stale entries are pruned when files are removed."""

    def test_prunes_deleted_file(self, tmp_path):
        nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
        test_nef = str(tmp_path / "test.NEF")
        test_nef2 = str(tmp_path / "test2.NEF")
        shutil.copy2(nef_path, test_nef)
        shutil.copy2(nef_path, test_nef2)

        idx_path = str(tmp_path / "index.json")
        index = ScanIndex(idx_path)
        photos1 = scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef"},
            index=index,
            workers=2,
        )
        index.save()
        assert len(photos1) == 2
        assert index.size == 2

        os.remove(test_nef2)

        index2 = ScanIndex(idx_path)
        index2.load()
        assert index2.size == 2

        photos2 = scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef"},
            index=index2,
            workers=2,
        )
        assert len(photos2) == 1
        assert index2.size == 1


@needs_samples
class TestScanWithIndexReindex:
    """Test that clearing the index forces full rescan."""

    def test_clear_forces_rescan(self, tmp_path):
        nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
        test_nef = str(tmp_path / "test.NEF")
        shutil.copy2(nef_path, test_nef)

        idx_path = str(tmp_path / "index.json")
        index = ScanIndex(idx_path)
        scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef"},
            index=index,
            workers=2,
        )
        index.save()
        assert index.size == 1

        index2 = ScanIndex(idx_path)
        index2.clear()
        assert index2.size == 0

        photos = scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef"},
            index=index2,
            workers=2,
        )
        assert len(photos) == 1
        assert index2.size == 1


@needs_samples
class TestScanWithIndexMultithreaded:
    """Test that multithreaded scanning produces correct results."""

    def test_all_copies_scanned(self, tmp_path):
        nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
        for i in range(6):
            shutil.copy2(nef_path, str(tmp_path / f"photo_{i}.NEF"))

        photos = scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef"},
            index=None,
            workers=4,
        )

        assert len(photos) == 6
        assert all(p.datetime_original is not None for p in photos)
        assert {p.camera_make for p in photos if p.camera_make} == {"NIKON CORPORATION"}
        assert not any(p.scan_error for p in photos)


@needs_samples
@needs_pyexiv2_pil
class TestScanWithIndexFullPipeline:
    """Integration test: scan with index → match → verify round-trip."""

    def test_full_pipeline(self, tmp_path):
        nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
        test_nef = str(tmp_path / "NIK_7953.NEF")
        shutil.copy2(nef_path, test_nef)

        fake_mobile = str(tmp_path / "IMG_0001.jpg")
        _create_test_jpeg_with_exif(
            fake_mobile,
            dt_str="2017:09:23 22:30:00",
            gps=(55.7539, 37.6208),
        )

        idx_path = str(tmp_path / "index.json")
        index = ScanIndex(idx_path)

        photos = scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef", ".jpg"},
            index=index,
            workers=2,
        )
        index.save()

        assert len(photos) == 2
        sources = [p for p in photos if p.has_gps]
        targets = [p for p in photos if not p.has_gps]
        assert len(sources) == 1
        assert len(targets) == 1

        from geosnag.matcher import match_photos

        matches, _, stats = match_photos(photos, max_time_delta=timedelta(hours=2))
        assert stats.matched == 1

        # Second scan (from cache)
        index2 = ScanIndex(idx_path)
        index2.load()
        photos2 = scan_with_index(
            directories=[str(tmp_path)],
            extensions={".nef", ".jpg"},
            index=index2,
            workers=2,
        )
        _, _, stats2 = match_photos(photos2, max_time_delta=timedelta(hours=2))
        assert stats2.matched == stats.matched


# ── Match cache tests ────────────────────────────────────────────────


class TestMatchCacheWriteRead:
    """Test storing and retrieving match results, including persistence."""

    def test_write_read_persist(self, tmp_path):
        test_file = str(tmp_path / "photo.jpg")
        _create_test_jpeg(test_file)

        idx_path = str(tmp_path / "index.json")
        idx = ScanIndex(idx_path)

        meta = PhotoMeta(
            filepath=test_file,
            filename="photo.jpg",
            extension=".jpg",
            datetime_original=datetime(2023, 6, 15, 10, 0, 0),
        )
        idx.update(meta)

        # Initially no match result
        status, fp = idx.get_match_result(test_file)
        assert status is None and fp is None

        # Store match result
        idx.update_match_result(test_file, "no_match", "abc123def456")
        status, fp = idx.get_match_result(test_file)
        assert status == "no_match"
        assert fp == "abc123def456"

        # Persist and reload
        idx.save()
        idx2 = ScanIndex(idx_path)
        idx2.load()

        status2, fp2 = idx2.get_match_result(test_file)
        assert status2 == "no_match"
        assert fp2 == "abc123def456"

        # Also store "matched" status
        idx2.update_match_result(test_file, "matched", "xyz789")
        status3, _ = idx2.get_match_result(test_file)
        assert status3 == "matched"

        # Non-existent file returns None
        status4, fp4 = idx2.get_match_result("/nonexistent/path.jpg")
        assert status4 is None and fp4 is None


class TestMatchCacheThresholdInvalidation:
    """Test that changing max_time_delta clears all match caches."""

    def test_threshold_changes(self, tmp_path):
        test_file = str(tmp_path / "photo.jpg")
        _create_test_jpeg(test_file)

        idx_path = str(tmp_path / "index.json")
        idx = ScanIndex(idx_path)

        meta = PhotoMeta(filepath=test_file, filename="photo.jpg", extension=".jpg")
        idx.update(meta)
        idx.update_match_result(test_file, "no_match", "fingerprint_a")

        # None → 120 invalidates
        assert not idx.validate_match_threshold(120)

        # Re-add match result after invalidation
        idx.update_match_result(test_file, "no_match", "fingerprint_a")

        # 120 → 120 is valid
        assert idx.validate_match_threshold(120)

        status, fp = idx.get_match_result(test_file)
        assert status == "no_match"

        # 120 → 60 invalidates
        assert not idx.validate_match_threshold(60)

        status2, fp2 = idx.get_match_result(test_file)
        assert status2 is None and fp2 is None

        # Persist and verify
        idx.save()
        idx2 = ScanIndex(idx_path)
        idx2.load()
        assert idx2.validate_match_threshold(60)


class TestMatchCacheClearedOnRescan:
    """Test that update() (re-scan) clears match cache for that entry."""

    def test_rescan_clears_match(self, tmp_path):
        test_file = str(tmp_path / "photo.jpg")
        _create_test_jpeg(test_file)

        idx_path = str(tmp_path / "index.json")
        idx = ScanIndex(idx_path)

        meta1 = PhotoMeta(
            filepath=test_file,
            filename="photo.jpg",
            extension=".jpg",
            datetime_original=datetime(2023, 6, 15, 10, 0, 0),
        )
        idx.update(meta1)
        idx.update_match_result(test_file, "no_match", "fp_original")

        status, _ = idx.get_match_result(test_file)
        assert status == "no_match"

        # Simulate re-scan
        meta2 = PhotoMeta(
            filepath=test_file,
            filename="photo.jpg",
            extension=".jpg",
            datetime_original=datetime(2023, 6, 16, 12, 0, 0),
        )
        idx.update(meta2)

        status2, fp2 = idx.get_match_result(test_file)
        assert status2 is None and fp2 is None


class TestMatchCacheThresholdPersistence:
    """Test that match_threshold_minutes persists in JSON."""

    def test_persists_and_clears(self, tmp_path):
        idx_path = str(tmp_path / "index.json")
        idx = ScanIndex(idx_path)
        idx.validate_match_threshold(120)
        idx.save()

        with open(idx_path, "r") as f:
            data = json.load(f)
        assert data.get("match_threshold_minutes") == 120

        idx2 = ScanIndex(idx_path)
        idx2.load()
        assert idx2.validate_match_threshold(120)

        idx2.clear()
        assert idx2._match_threshold_minutes is None


class TestMatchCacheIndexClear:
    """Test that clear() wipes match results and threshold."""

    def test_clear_wipes_all(self, tmp_path):
        test_file = str(tmp_path / "photo.jpg")
        _create_test_jpeg(test_file)

        idx_path = str(tmp_path / "index.json")
        idx = ScanIndex(idx_path)

        idx.validate_match_threshold(120)
        meta = PhotoMeta(filepath=test_file, filename="photo.jpg", extension=".jpg")
        idx.update(meta)
        idx.update_match_result(test_file, "no_match", "fp1")

        status, _ = idx.get_match_result(test_file)
        assert status == "no_match"

        idx.clear()
        assert idx.size == 0
        assert idx._match_threshold_minutes is None

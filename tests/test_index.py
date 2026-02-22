#!/usr/bin/env python3
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

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta

# Add parent to path for package imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from geosnag.index import ScanIndex, _entry_to_photo, _photo_to_entry
from geosnag.parallel import _collect_file_paths, scan_with_index
from geosnag.scanner import PHOTO_EXTS, PhotoMeta

SAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "samples")
PASS = "\u2713"
FAIL = "\u2717"

results = {"pass": 0, "fail": 0}


def check(name, condition, detail=""):
    global results
    status = PASS if condition else FAIL
    results["pass" if condition else "fail"] += 1
    detail_str = f"  ({detail})" if detail else ""
    print(f"  {status} {name}{detail_str}")
    return condition


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


# ─── ScanIndex Unit Tests ───


def test_index_empty():
    """Test fresh index behavior."""
    print("\n── ScanIndex: Empty / New ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        idx_path = os.path.join(tmpdir, "test_index.json")
        idx = ScanIndex(idx_path)

        check("New index: size is 0", idx.size == 0)
        check("New index: load returns 0", idx.load() == 0)
        check("New index: lookup returns None", idx.lookup("/nonexistent/file.jpg") is None)


def test_index_save_load():
    """Test save and load round-trip."""
    print("\n── ScanIndex: Save / Load ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        idx_path = os.path.join(tmpdir, "test_index.json")
        test_file = os.path.join(tmpdir, "photo.jpg")
        _create_test_jpeg(test_file)

        # Create and populate index
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
        check("Save: dirty after update", idx._dirty is True)
        idx.save()

        check("Save: file created", os.path.exists(idx_path))
        check("Save: not dirty after save", idx._dirty is False)

        # Load in new instance
        idx2 = ScanIndex(idx_path)
        count = idx2.load()
        check("Load: entry count correct", count == 1, f"got {count}")

        # Lookup should return cached data
        cached = idx2.lookup(test_file)
        check("Load: lookup returns PhotoMeta", cached is not None)
        if cached:
            check("Load: datetime preserved", cached.datetime_original == datetime(2023, 6, 15, 14, 30, 0))
            check("Load: has_gps preserved", cached.has_gps is True)
            check("Load: latitude preserved", abs(cached.gps_latitude - 55.7539) < 0.0001)
            check("Load: longitude preserved", abs(cached.gps_longitude - 37.6208) < 0.0001)
            check("Load: altitude preserved", abs(cached.gps_altitude - 150.0) < 0.1)
            check("Load: camera_make preserved", cached.camera_make == "NIKON CORPORATION")
            check("Load: camera_model preserved", cached.camera_model == "NIKON D610")
            check("Load: extension correct", cached.extension == ".jpg")
            check("Load: filename correct", cached.filename == "photo.jpg")


def test_index_mtime_invalidation():
    """Test that changed mtime invalidates cache."""
    print("\n── ScanIndex: Mtime Invalidation ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        idx_path = os.path.join(tmpdir, "test_index.json")
        test_file = os.path.join(tmpdir, "photo.jpg")
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

        # Verify cache hit
        idx2 = ScanIndex(idx_path)
        idx2.load()
        cached = idx2.lookup(test_file)
        check("Mtime: cache hit before change", cached is not None)

        # Modify the file (changes mtime)
        time.sleep(0.05)  # ensure mtime difference
        with open(test_file, "ab") as f:
            f.write(b"\x00" * 10)

        # Cache should miss now
        cached_after = idx2.lookup(test_file)
        check("Mtime: cache miss after modification", cached_after is None)


def test_index_size_invalidation():
    """Test that changed file size invalidates cache."""
    print("\n── ScanIndex: Size Invalidation ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        idx_path = os.path.join(tmpdir, "test_index.json")
        test_file = os.path.join(tmpdir, "photo.jpg")
        _create_test_jpeg(test_file)

        idx = ScanIndex(idx_path)
        meta = PhotoMeta(
            filepath=test_file,
            filename="photo.jpg",
            extension=".jpg",
        )
        idx.update(meta)

        # Verify hit
        cached = idx.lookup(test_file)
        check("Size: cache hit before change", cached is not None)

        # Change file but preserve mtime
        orig_mtime = os.path.getmtime(test_file)
        with open(test_file, "ab") as f:
            f.write(b"\x00" * 100)
        os.utime(test_file, (orig_mtime, orig_mtime))

        # Size changed, so cache should miss
        cached_after = idx.lookup(test_file)
        check("Size: cache miss after size change", cached_after is None)


def test_index_deleted_file():
    """Test lookup for a deleted file."""
    print("\n── ScanIndex: Deleted File ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        idx_path = os.path.join(tmpdir, "test_index.json")
        test_file = os.path.join(tmpdir, "photo.jpg")
        _create_test_jpeg(test_file)

        idx = ScanIndex(idx_path)
        meta = PhotoMeta(filepath=test_file, filename="photo.jpg", extension=".jpg")
        idx.update(meta)

        # Delete the file
        os.remove(test_file)

        # Lookup should return None (file doesn't exist → mtime fails)
        cached = idx.lookup(test_file)
        check("Deleted: lookup returns None", cached is None)


def test_index_prune():
    """Test pruning stale entries."""
    print("\n── ScanIndex: Prune ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        idx_path = os.path.join(tmpdir, "test_index.json")

        idx = ScanIndex(idx_path)
        # Add entries for files that don't exist
        for i in range(5):
            fake_path = f"/fake/photo_{i}.jpg"
            idx.entries[fake_path] = {"mtime": 0, "size": 0}

        # Also add one real entry
        real_file = os.path.join(tmpdir, "real.jpg")
        _create_test_jpeg(real_file)
        meta = PhotoMeta(filepath=real_file, filename="real.jpg", extension=".jpg")
        idx.update(meta)

        check("Prune: 6 entries before", idx.size == 6, f"got {idx.size}")

        # Prune — only real_file is valid
        removed = idx.prune({real_file})
        check("Prune: removed 5 stale", removed == 5, f"removed {removed}")
        check("Prune: 1 entry left", idx.size == 1)
        check("Prune: real file kept", real_file in idx.entries)


def test_index_clear():
    """Test clearing all entries."""
    print("\n── ScanIndex: Clear ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        idx_path = os.path.join(tmpdir, "test_index.json")

        idx = ScanIndex(idx_path)
        for i in range(3):
            idx.entries[f"/fake/{i}.jpg"] = {}
        idx._dirty = True
        idx.save()

        idx.clear()
        check("Clear: size is 0", idx.size == 0)
        check("Clear: dirty after clear", idx._dirty is True)


def test_index_corrupt_file():
    """Test loading a corrupt index file."""
    print("\n── ScanIndex: Corrupt File ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        idx_path = os.path.join(tmpdir, "test_index.json")

        # Write garbage
        with open(idx_path, "w") as f:
            f.write("this is not json {{{")

        idx = ScanIndex(idx_path)
        count = idx.load()
        check("Corrupt: load returns 0", count == 0)
        check("Corrupt: entries empty", idx.size == 0)


def test_index_version_mismatch():
    """Test loading an index with wrong version."""
    print("\n── ScanIndex: Version Mismatch ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        idx_path = os.path.join(tmpdir, "test_index.json")

        # Write index with old version
        with open(idx_path, "w") as f:
            json.dump({"version": 1, "entries": {"a.jpg": {}}}, f)

        idx = ScanIndex(idx_path)
        count = idx.load()
        check("Version: load returns 0", count == 0)
        check("Version: entries cleared", idx.size == 0)


def test_index_no_dirty_on_no_change():
    """Test that save is skipped when nothing changed."""
    print("\n── ScanIndex: No Dirty Save ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        idx_path = os.path.join(tmpdir, "test_index.json")

        idx = ScanIndex(idx_path)
        idx.entries = {"a.jpg": {}}
        idx._dirty = True
        idx.save()

        # Get mtime of saved file
        mtime_before = os.path.getmtime(idx_path)
        time.sleep(0.05)

        # Load and save without changes
        idx2 = ScanIndex(idx_path)
        idx2.load()
        idx2.save()  # should skip (not dirty)

        mtime_after = os.path.getmtime(idx_path)
        check("NoDirty: file not re-written", mtime_before == mtime_after)


def test_index_serialization_roundtrip():
    """Test _photo_to_entry / _entry_to_photo roundtrip."""
    print("\n── ScanIndex: Serialization Roundtrip ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "photo.nef")
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

        check("Roundtrip: filepath", restored.filepath == original.filepath)
        check("Roundtrip: filename", restored.filename == original.filename)
        check("Roundtrip: extension", restored.extension == original.extension)
        check("Roundtrip: datetime", restored.datetime_original == original.datetime_original)
        check("Roundtrip: has_gps", restored.has_gps == original.has_gps)
        check("Roundtrip: latitude", restored.gps_latitude == original.gps_latitude)
        check("Roundtrip: longitude", restored.gps_longitude == original.gps_longitude)
        check("Roundtrip: altitude", restored.gps_altitude == original.gps_altitude)
        check("Roundtrip: camera_make", restored.camera_make == original.camera_make)
        check("Roundtrip: camera_model", restored.camera_model == original.camera_model)
        check("Roundtrip: geosnag_processed", restored.geosnag_processed == original.geosnag_processed)
        check("Roundtrip: scan_error", restored.scan_error == original.scan_error)


# ─── _collect_file_paths Tests ───


def test_collect_file_paths():
    """Test file path collection."""
    print("\n── _collect_file_paths ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test structure
        subdir = os.path.join(tmpdir, "sub")
        os.makedirs(subdir)

        for name in ["a.jpg", "b.nef", "c.txt", "d.arw"]:
            with open(os.path.join(tmpdir, name), "wb") as f:
                f.write(b"\x00")

        with open(os.path.join(subdir, "e.jpg"), "wb") as f:
            f.write(b"\x00")

        # Recursive
        paths = _collect_file_paths([tmpdir], PHOTO_EXTS, recursive=True)
        jpg_nef_arw = [p for p in paths if os.path.splitext(p)[1].lower() in PHOTO_EXTS]
        check("Collect recursive: found photos", len(jpg_nef_arw) == 4, f"got {len(jpg_nef_arw)}")

        # Non-recursive
        paths_flat = _collect_file_paths([tmpdir], PHOTO_EXTS, recursive=False)
        check("Collect non-recursive: found 3", len(paths_flat) == 3, f"got {len(paths_flat)}")

        # Exclude pattern
        paths_excl = _collect_file_paths(
            [tmpdir],
            PHOTO_EXTS,
            recursive=True,
            exclude_patterns=["sub/*"],
        )
        check("Collect excluded: found 3", len(paths_excl) == 3, f"got {len(paths_excl)}")

        # Non-existent directory
        paths_bad = _collect_file_paths(["/nonexistent/dir"], PHOTO_EXTS)
        check("Collect bad dir: returns empty", len(paths_bad) == 0)


# ─── scan_with_index Integration Tests ───


def test_scan_with_index_no_cache():
    """Test scan_with_index without an index (all misses)."""
    print("\n── scan_with_index: No Cache ──")

    nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
    if not os.path.exists(nef_path):
        print(f"  SKIP: Sample NEF not found at {nef_path}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        # Copy sample file
        test_nef = os.path.join(tmpdir, "test.NEF")
        shutil.copy2(nef_path, test_nef)

        # Scan without index
        photos = scan_with_index(
            directories=[tmpdir],
            extensions={".nef"},
            index=None,
            workers=2,
        )

        check("NoCache: found 1 photo", len(photos) == 1, f"got {len(photos)}")
        if photos:
            p = photos[0]
            check("NoCache: datetime parsed", p.datetime_original is not None)
            check("NoCache: camera make", p.camera_make == "NIKON CORPORATION")


def test_scan_with_index_cache_hit():
    """Test scan_with_index with index (second run = all cache hits)."""
    print("\n── scan_with_index: Cache Hit ──")

    nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
    if not os.path.exists(nef_path):
        print(f"  SKIP: Sample NEF not found at {nef_path}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        test_nef = os.path.join(tmpdir, "test.NEF")
        shutil.copy2(nef_path, test_nef)

        idx_path = os.path.join(tmpdir, "index.json")
        index = ScanIndex(idx_path)

        # First scan — builds index
        photos1 = scan_with_index(
            directories=[tmpdir],
            extensions={".nef"},
            index=index,
            workers=2,
        )
        index.save()

        check("CacheHit: first scan found 1", len(photos1) == 1)
        check("CacheHit: index has entry", index.size == 1, f"size={index.size}")

        # Second scan — should hit cache
        index2 = ScanIndex(idx_path)
        index2.load()

        photos2 = scan_with_index(
            directories=[tmpdir],
            extensions={".nef"},
            index=index2,
            workers=2,
        )

        check("CacheHit: second scan found 1", len(photos2) == 1)
        if photos1 and photos2:
            check("CacheHit: datetime matches", photos1[0].datetime_original == photos2[0].datetime_original)
            check("CacheHit: camera_make matches", photos1[0].camera_make == photos2[0].camera_make)
            check("CacheHit: has_gps matches", photos1[0].has_gps == photos2[0].has_gps)


def test_scan_with_index_mixed():
    """Test scan_with_index with mix of cached and new files."""
    print("\n── scan_with_index: Mixed Cache ──")

    nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
    if not os.path.exists(nef_path):
        print(f"  SKIP: Sample NEF not found at {nef_path}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        test_nef = os.path.join(tmpdir, "test.NEF")
        shutil.copy2(nef_path, test_nef)

        idx_path = os.path.join(tmpdir, "index.json")
        index = ScanIndex(idx_path)

        # First scan
        scan_with_index(
            directories=[tmpdir],
            extensions={".nef", ".jpg"},
            index=index,
            workers=2,
        )
        index.save()

        # Add a new JPEG file
        new_jpg = os.path.join(tmpdir, "new_photo.jpg")
        _create_test_jpeg(new_jpg)

        # Second scan — NEF from cache, JPG is new
        index2 = ScanIndex(idx_path)
        index2.load()

        photos2 = scan_with_index(
            directories=[tmpdir],
            extensions={".nef", ".jpg"},
            index=index2,
            workers=2,
        )

        check("Mixed: found 2 photos", len(photos2) == 2, f"got {len(photos2)}")

        # Check that NEF data came from cache (still correct)
        nef_results = [p for p in photos2 if p.extension == ".nef"]
        jpg_results = [p for p in photos2 if p.extension == ".jpg"]
        check("Mixed: NEF present", len(nef_results) == 1)
        check("Mixed: JPG present", len(jpg_results) == 1)

        if nef_results:
            check(
                "Mixed: NEF datetime preserved", nef_results[0].datetime_original == datetime(2017, 9, 23, 23, 11, 37)
            )


def test_scan_with_index_prune_stale():
    """Test that stale entries are pruned when files are removed."""
    print("\n── scan_with_index: Prune Stale ──")

    nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
    if not os.path.exists(nef_path):
        print(f"  SKIP: Sample NEF not found at {nef_path}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        test_nef = os.path.join(tmpdir, "test.NEF")
        test_nef2 = os.path.join(tmpdir, "test2.NEF")
        shutil.copy2(nef_path, test_nef)
        shutil.copy2(nef_path, test_nef2)

        idx_path = os.path.join(tmpdir, "index.json")
        index = ScanIndex(idx_path)

        # First scan — 2 files
        photos1 = scan_with_index(
            directories=[tmpdir],
            extensions={".nef"},
            index=index,
            workers=2,
        )
        index.save()
        check("PruneStale: first scan found 2", len(photos1) == 2, f"got {len(photos1)}")
        check("PruneStale: index has 2", index.size == 2)

        # Delete one file
        os.remove(test_nef2)

        # Second scan — should prune the deleted file
        index2 = ScanIndex(idx_path)
        index2.load()
        check("PruneStale: loaded 2 from disk", index2.size == 2)

        photos2 = scan_with_index(
            directories=[tmpdir],
            extensions={".nef"},
            index=index2,
            workers=2,
        )

        check("PruneStale: second scan found 1", len(photos2) == 1, f"got {len(photos2)}")
        check("PruneStale: index pruned to 1", index2.size == 1, f"size={index2.size}")


def test_scan_with_index_reindex():
    """Test that clearing the index forces full rescan."""
    print("\n── scan_with_index: Reindex ──")

    nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
    if not os.path.exists(nef_path):
        print(f"  SKIP: Sample NEF not found at {nef_path}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        test_nef = os.path.join(tmpdir, "test.NEF")
        shutil.copy2(nef_path, test_nef)

        idx_path = os.path.join(tmpdir, "index.json")
        index = ScanIndex(idx_path)

        # First scan
        scan_with_index(
            directories=[tmpdir],
            extensions={".nef"},
            index=index,
            workers=2,
        )
        index.save()
        check("Reindex: index populated", index.size == 1)

        # Clear (simulates --reindex)
        index2 = ScanIndex(idx_path)
        index2.clear()  # Don't load, just clear
        check("Reindex: cleared size=0", index2.size == 0)

        # Scan again — should do full EXIF read
        photos = scan_with_index(
            directories=[tmpdir],
            extensions={".nef"},
            index=index2,
            workers=2,
        )
        check("Reindex: found photo", len(photos) == 1)
        check("Reindex: index repopulated", index2.size == 1)


def test_scan_with_index_multithreaded():
    """Test that multithreaded scanning produces correct results."""
    print("\n── scan_with_index: Multithreaded ──")

    nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
    if not os.path.exists(nef_path):
        print(f"  SKIP: Sample NEF not found at {nef_path}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create multiple copies to actually exercise threading
        for i in range(6):
            dest = os.path.join(tmpdir, f"photo_{i}.NEF")
            shutil.copy2(nef_path, dest)

        # Scan with multiple workers
        photos = scan_with_index(
            directories=[tmpdir],
            extensions={".nef"},
            index=None,
            workers=4,
        )

        check("Threaded: found all 6", len(photos) == 6, f"got {len(photos)}")

        # All should have same metadata
        with_dt = [p for p in photos if p.datetime_original is not None]
        check("Threaded: all have datetime", len(with_dt) == 6, f"got {len(with_dt)}")

        makes = set(p.camera_make for p in photos if p.camera_make)
        check("Threaded: all NIKON", makes == {"NIKON CORPORATION"}, str(makes))

        errors = [p for p in photos if p.scan_error]
        check("Threaded: no errors", len(errors) == 0, f"got {len(errors)}")


def test_scan_with_index_full_pipeline():
    """Integration test: scan with index → match → verify round-trip."""
    print("\n── scan_with_index: Full Pipeline ──")

    nef_path = os.path.join(SAMPLES_DIR, "NIK_7953.NEF")
    if not os.path.exists(nef_path):
        print(f"  SKIP: Sample NEF not found at {nef_path}")
        return

    try:
        import importlib.util

        has_deps = importlib.util.find_spec("pyexiv2") is not None and importlib.util.find_spec("PIL") is not None
    except (ImportError, ModuleNotFoundError):
        has_deps = False

    if not has_deps:
        print("  SKIP: pyexiv2/PIL not available")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        # Set up: NEF (no GPS) + JPEG (with GPS, same day)
        test_nef = os.path.join(tmpdir, "NIK_7953.NEF")
        shutil.copy2(nef_path, test_nef)

        fake_mobile = os.path.join(tmpdir, "IMG_0001.jpg")
        _create_test_jpeg_with_exif(
            fake_mobile,
            dt_str="2017:09:23 22:30:00",
            gps=(55.7539, 37.6208),
        )

        idx_path = os.path.join(tmpdir, "index.json")
        index = ScanIndex(idx_path)

        # First scan with index
        photos = scan_with_index(
            directories=[tmpdir],
            extensions={".nef", ".jpg"},
            index=index,
            workers=2,
        )
        index.save()

        check("Pipeline: found 2 photos", len(photos) == 2, f"got {len(photos)}")

        sources = [p for p in photos if p.has_gps]
        targets = [p for p in photos if not p.has_gps]
        check("Pipeline: 1 source with GPS", len(sources) == 1)
        check("Pipeline: 1 target without GPS", len(targets) == 1)

        # Match
        from geosnag.matcher import match_photos

        matches, _, stats = match_photos(photos, max_time_delta=timedelta(hours=2))
        check("Pipeline: 1 match", stats.matched == 1, f"got {stats.matched}")

        # Second scan (from cache) — should give same results
        index2 = ScanIndex(idx_path)
        index2.load()
        photos2 = scan_with_index(
            directories=[tmpdir],
            extensions={".nef", ".jpg"},
            index=index2,
            workers=2,
        )
        matches2, _, stats2 = match_photos(photos2, max_time_delta=timedelta(hours=2))
        check("Pipeline: cached scan same match count", stats2.matched == stats.matched, f"got {stats2.matched}")


# ── Match cache tests ────────────────────────────────────────────────


def test_match_cache_write_read():
    """Test storing and retrieving match results, including persistence."""
    print("\n── Match Cache: Write & Read ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a dummy file so mtime/size work
        test_file = os.path.join(tmpdir, "photo.jpg")
        _create_test_jpeg(test_file)

        idx_path = os.path.join(tmpdir, "index.json")
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
        check("MatchCache: initially None", status is None and fp is None)

        # Store match result
        idx.update_match_result(test_file, "no_match", "abc123def456")
        status, fp = idx.get_match_result(test_file)
        check("MatchCache: status saved", status == "no_match")
        check("MatchCache: fingerprint saved", fp == "abc123def456")

        # Persist and reload
        idx.save()
        idx2 = ScanIndex(idx_path)
        idx2.load()

        status2, fp2 = idx2.get_match_result(test_file)
        check("MatchCache: persists across reload", status2 == "no_match")
        check("MatchCache: fingerprint persists", fp2 == "abc123def456")

        # Also store "matched" status
        idx2.update_match_result(test_file, "matched", "xyz789")
        status3, fp3 = idx2.get_match_result(test_file)
        check("MatchCache: matched status", status3 == "matched")

        # Non-existent file returns None
        status4, fp4 = idx2.get_match_result("/nonexistent/path.jpg")
        check("MatchCache: nonexistent returns None", status4 is None and fp4 is None)


def test_match_cache_threshold_invalidation():
    """Test that changing max_time_delta clears all match caches."""
    print("\n── Match Cache: Threshold Invalidation ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "photo.jpg")
        _create_test_jpeg(test_file)

        idx_path = os.path.join(tmpdir, "index.json")
        idx = ScanIndex(idx_path)

        meta = PhotoMeta(
            filepath=test_file,
            filename="photo.jpg",
            extension=".jpg",
        )
        idx.update(meta)
        idx.update_match_result(test_file, "no_match", "fingerprint_a")

        # Validate with same threshold — should keep cache
        valid = idx.validate_match_threshold(120)
        check("Threshold: None → 120 invalidates", not valid)

        # Re-add match result after invalidation
        idx.update_match_result(test_file, "no_match", "fingerprint_a")

        # Validate again with same threshold — should be valid now
        valid = idx.validate_match_threshold(120)
        check("Threshold: 120 → 120 is valid", valid)

        status, fp = idx.get_match_result(test_file)
        check("Threshold: cache preserved when valid", status == "no_match")

        # Change threshold — should clear all match caches
        valid = idx.validate_match_threshold(60)
        check("Threshold: 120 → 60 invalidates", not valid)

        status2, fp2 = idx.get_match_result(test_file)
        check("Threshold: cache cleared on change", status2 is None and fp2 is None)

        # Persist and verify threshold is stored
        idx.save()
        idx2 = ScanIndex(idx_path)
        idx2.load()
        valid2 = idx2.validate_match_threshold(60)
        check("Threshold: persists across reload", valid2)


def test_match_cache_cleared_on_rescan():
    """Test that update() (re-scan) clears match cache for that entry."""
    print("\n── Match Cache: Cleared on Re-scan ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "photo.jpg")
        _create_test_jpeg(test_file)

        idx_path = os.path.join(tmpdir, "index.json")
        idx = ScanIndex(idx_path)

        meta1 = PhotoMeta(
            filepath=test_file,
            filename="photo.jpg",
            extension=".jpg",
            datetime_original=datetime(2023, 6, 15, 10, 0, 0),
        )
        idx.update(meta1)
        idx.update_match_result(test_file, "no_match", "fp_original")

        status, fp = idx.get_match_result(test_file)
        check("Rescan: match cache set", status == "no_match")

        # Simulate re-scan (file changed, update() called again)
        meta2 = PhotoMeta(
            filepath=test_file,
            filename="photo.jpg",
            extension=".jpg",
            datetime_original=datetime(2023, 6, 16, 12, 0, 0),  # different date
        )
        idx.update(meta2)  # _photo_to_entry doesn't include match fields

        status2, fp2 = idx.get_match_result(test_file)
        check("Rescan: match cache cleared", status2 is None and fp2 is None)


def test_match_cache_threshold_persists():
    """Test that match_threshold_minutes persists in JSON."""
    print("\n── Match Cache: Threshold Persistence ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        idx_path = os.path.join(tmpdir, "index.json")
        idx = ScanIndex(idx_path)
        idx.validate_match_threshold(120)
        idx.save()

        # Read raw JSON to verify structure
        with open(idx_path, "r") as f:
            data = json.load(f)
        check("Persist: match_threshold_minutes in JSON", data.get("match_threshold_minutes") == 120)

        # Reload
        idx2 = ScanIndex(idx_path)
        idx2.load()
        valid = idx2.validate_match_threshold(120)
        check("Persist: threshold valid after reload", valid)

        # Clear resets threshold
        idx2.clear()
        check("Persist: clear resets threshold", idx2._match_threshold_minutes is None)


def test_match_cache_index_clear():
    """Test that clear() wipes match results and threshold."""
    print("\n── Match Cache: Index Clear ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = os.path.join(tmpdir, "photo.jpg")
        _create_test_jpeg(test_file)

        idx_path = os.path.join(tmpdir, "index.json")
        idx = ScanIndex(idx_path)

        # Set threshold first, then update entry and match result
        idx.validate_match_threshold(120)

        meta = PhotoMeta(
            filepath=test_file,
            filename="photo.jpg",
            extension=".jpg",
        )
        idx.update(meta)
        idx.update_match_result(test_file, "no_match", "fp1")

        # Verify set
        status, fp = idx.get_match_result(test_file)
        check("Clear: cache set before clear", status == "no_match")

        # Clear all (simulates --reindex)
        idx.clear()
        check("Clear: entries empty", idx.size == 0)
        check("Clear: threshold reset", idx._match_threshold_minutes is None)


if __name__ == "__main__":
    print("=" * 55)
    from geosnag import __version__

    print(f"  GeoSnag Index & Parallel Tests (v{__version__})")
    print("=" * 55)

    # ScanIndex unit tests
    test_index_empty()
    test_index_save_load()
    test_index_mtime_invalidation()
    test_index_size_invalidation()
    test_index_deleted_file()
    test_index_prune()
    test_index_clear()
    test_index_corrupt_file()
    test_index_version_mismatch()
    test_index_no_dirty_on_no_change()
    test_index_serialization_roundtrip()

    # _collect_file_paths
    test_collect_file_paths()

    # scan_with_index integration
    test_scan_with_index_no_cache()
    test_scan_with_index_cache_hit()
    test_scan_with_index_mixed()
    test_scan_with_index_prune_stale()
    test_scan_with_index_reindex()
    test_scan_with_index_multithreaded()
    test_scan_with_index_full_pipeline()

    # Match cache
    test_match_cache_write_read()
    test_match_cache_threshold_invalidation()
    test_match_cache_cleared_on_rescan()
    test_match_cache_threshold_persists()
    test_match_cache_index_clear()

    print()
    print("=" * 55)
    total = results["pass"] + results["fail"]
    print(f"  Results: {results['pass']}/{total} passed, {results['fail']} failed")
    print("=" * 55)

    sys.exit(0 if results["fail"] == 0 else 1)

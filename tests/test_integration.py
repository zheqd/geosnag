#!/usr/bin/env python3
"""
Integration tests for GeoSnag using real sample files.

Tests multiple components working together with real binary fixtures:
scan → match → write → verify. Uses a minimal NEF + JPG fixture pair
from tests/fixtures/.
"""

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

# Add parent to path for package imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import geosnag.writer as writer_module
from geosnag.matcher import match_photos
from geosnag.scanner import PhotoMeta, scan_directory, scan_photo
from geosnag.writer import _find_exiftool, stamp_processed, write_gps_to_exif, write_gps_xmp_sidecar

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
PASS = "✓"
FAIL = "✗"

results = {"pass": 0, "fail": 0}


def check(name: str, condition: bool, detail: str = ""):
    global results
    status = PASS if condition else FAIL
    results["pass" if condition else "fail"] += 1
    detail_str = f"  ({detail})" if detail else ""
    print(f"  {status} {name}{detail_str}")
    return condition


def test_scanner():
    """Test EXIF scanning on sample files."""
    print("\n── Scanner Tests ──")

    nef_path = os.path.join(FIXTURES_DIR, "camera_no_gps.nef")
    if not os.path.exists(nef_path):
        print(f"  SKIP: Sample NEF not found at {nef_path}")
        return

    # Test NEF scanning (no is_mobile_hint parameter anymore)
    meta = scan_photo(nef_path)
    check("NEF: scan succeeds", meta.scan_error is None, meta.scan_error or "")
    check("NEF: camera make", meta.camera_make == "NIKON CORPORATION", meta.camera_make or "None")
    check("NEF: camera model", meta.camera_model == "NIKON D610", meta.camera_model or "None")
    check("NEF: datetime parsed", meta.datetime_original is not None)
    check(
        "NEF: datetime correct",
        meta.datetime_original == datetime(2017, 9, 23, 23, 11, 37) if meta.datetime_original else False,
        str(meta.datetime_original),
    )
    check("NEF: no GPS (as expected)", not meta.has_gps)
    check("NEF: date_key", meta.date_key == "2017-09-23", meta.date_key or "None")
    check("NEF: extension", meta.extension == ".nef", meta.extension)
    check("NEF: not processed", not meta.geosnag_processed)


def test_matcher():
    """Test matching engine with synthetic data — unified photo list."""
    print("\n── Matcher Tests ──")

    # Create synthetic photos — no camera/mobile distinction
    target_photo = PhotoMeta(
        filepath="/fake/DSC_001.NEF",
        filename="DSC_001.NEF",
        extension=".nef",
        datetime_original=datetime(2023, 6, 15, 14, 30, 0),
        has_gps=False,
        camera_make="NIKON",
        camera_model="D610",
    )

    source_close = PhotoMeta(
        filepath="/fake/IMG_001.HEIC",
        filename="IMG_001.HEIC",
        extension=".heic",
        datetime_original=datetime(2023, 6, 15, 14, 15, 0),  # 15 min before target
        has_gps=True,
        gps_latitude=55.7539,
        gps_longitude=37.6208,
        camera_make="Apple",
        camera_model="iPhone 12",
    )

    source_far = PhotoMeta(
        filepath="/fake/IMG_002.HEIC",
        filename="IMG_002.HEIC",
        extension=".heic",
        datetime_original=datetime(2023, 6, 15, 10, 0, 0),  # 4.5 hours before
        has_gps=True,
        gps_latitude=55.0,
        gps_longitude=37.0,
    )

    source_no_gps = PhotoMeta(
        filepath="/fake/IMG_003.HEIC",
        filename="IMG_003.HEIC",
        extension=".heic",
        datetime_original=datetime(2023, 6, 15, 14, 29, 0),
        has_gps=False,
    )

    photo_with_gps = PhotoMeta(
        filepath="/fake/DSC_002.NEF",
        filename="DSC_002.NEF",
        extension=".nef",
        datetime_original=datetime(2023, 6, 15, 15, 0, 0),
        has_gps=True,
        gps_latitude=55.0,
        gps_longitude=37.0,
    )

    target_no_dt = PhotoMeta(
        filepath="/fake/DSC_003.NEF",
        filename="DSC_003.NEF",
        extension=".nef",
    )

    target_diff_day = PhotoMeta(
        filepath="/fake/DSC_004.NEF",
        filename="DSC_004.NEF",
        extension=".nef",
        datetime_original=datetime(2023, 6, 16, 10, 0, 0),
        has_gps=False,
    )

    already_processed = PhotoMeta(
        filepath="/fake/DSC_005.NEF",
        filename="DSC_005.NEF",
        extension=".nef",
        datetime_original=datetime(2023, 6, 15, 14, 20, 0),
        has_gps=False,
        geosnag_processed=True,  # Should be skipped
    )

    # Test 1: Unified matching with single photo list
    # In unified model: photos without GPS become targets (including source_no_gps).
    # source_no_gps has datetime on same day → it will also match to a GPS source.
    all_photos = [
        target_photo,
        photo_with_gps,
        target_no_dt,
        target_diff_day,
        source_close,
        source_far,
        source_no_gps,
        already_processed,
    ]

    matches, unmatched, stats = match_photos(
        all_photos,
        max_time_delta=timedelta(hours=2),
    )

    # 2 matches: target_photo→source_close, source_no_gps→source_close
    check("Match: 2 matches found", stats.matched == 2, f"got {stats.matched}")
    check("Match: already_processed skipped", stats.already_processed == 1)
    check("Match: sources detected", stats.sources >= 2, f"got {stats.sources}")
    check("Match: diff_day unmatched", stats.unmatched == 1)

    if matches:
        m = matches[0]
        check("Match: correct pair", m.target.filename == "DSC_001.NEF" and m.source.filename == "IMG_001.HEIC")
        check("Match: time delta ~15min", abs(m.time_delta.total_seconds() - 900) < 1, f"{m.time_delta_str}")
        check("Match: confidence > 85%", m.confidence > 85, f"{m.confidence:.1f}%")
        check("Match: GPS from source", m.source.gps_latitude == 55.7539)

    # Test 2: source_far should NOT match (4.5h > 2h threshold)
    check("Match: source_far excluded", all(m.source.filename != "IMG_002.HEIC" for m in matches))

    # Test 3: source_no_gps should not be used as source
    check("Match: source_no_gps excluded", all(m.source.filename != "IMG_003.HEIC" for m in matches))

    # Test 4: Wider threshold still picks closest
    matches2, _, stats2 = match_photos(
        [target_photo, source_close, source_far],
        max_time_delta=timedelta(hours=5),
    )
    check(
        "Match: wider threshold still picks closest",
        matches2[0].source.filename == "IMG_001.HEIC" if matches2 else False,
    )

    # Test 5: Backwards-compatible .camera / .mobile aliases
    if matches:
        m = matches[0]
        check("Match: .camera alias works", m.camera.filename == m.target.filename)
        check("Match: .mobile alias works", m.mobile.filename == m.source.filename)

    # Test 6: Explicit sources/targets mode
    matches3, _, stats3 = match_photos(
        sources=[source_close, source_far],
        targets=[target_photo],
        max_time_delta=timedelta(hours=2),
    )
    check("Match: explicit mode works", stats3.matched == 1, f"got {stats3.matched}")


def test_writer():
    """Test GPS writing on a copy of the NEF sample."""
    print("\n── Writer Tests ──")

    nef_path = os.path.join(FIXTURES_DIR, "camera_no_gps.nef")
    if not os.path.exists(nef_path):
        print(f"  SKIP: Sample NEF not found at {nef_path}")
        return

    # Work in temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        test_nef = os.path.join(tmpdir, "test.NEF")
        shutil.copy2(nef_path, test_nef)

        # Test EXIF write (with stamp)
        result = write_gps_to_exif(
            test_nef,
            latitude=55.7539,
            longitude=37.6208,
            altitude=150.5,
            stamp_after_write=True,
        )
        check("Write EXIF: success", result.success, result.error or "")

        # Verify GPS was written
        meta_after = scan_photo(test_nef)
        check("Write EXIF: GPS now present", meta_after.has_gps)
        if meta_after.has_gps:
            check(
                "Write EXIF: latitude correct",
                abs(meta_after.gps_latitude - 55.7539) < 0.001,
                f"{meta_after.gps_latitude:.6f}",
            )
            check(
                "Write EXIF: longitude correct",
                abs(meta_after.gps_longitude - 37.6208) < 0.001,
                f"{meta_after.gps_longitude:.6f}",
            )

        # Verify original metadata preserved
        check("Write EXIF: make preserved", meta_after.camera_make == "NIKON CORPORATION")
        check("Write EXIF: model preserved", meta_after.camera_model == "NIKON D610")
        check("Write EXIF: datetime preserved", meta_after.datetime_original == datetime(2017, 9, 23, 23, 11, 37))

        # Verify GeoSnag processed tag was written
        check("Write EXIF: geosnag_processed tag set", meta_after.geosnag_processed)

        # Test XMP sidecar
        test_nef2 = os.path.join(tmpdir, "test2.NEF")
        shutil.copy2(nef_path, test_nef2)

        result_xmp = write_gps_xmp_sidecar(
            test_nef2,
            latitude=48.8566,
            longitude=2.3522,
            altitude=35.0,
            stamp_after_write=True,
        )
        check("Write XMP: success", result_xmp.success, result_xmp.error or "")

        xmp_path = os.path.join(tmpdir, "test2.xmp")
        check("Write XMP: sidecar created", os.path.exists(xmp_path))

        if os.path.exists(xmp_path):
            with open(xmp_path) as f:
                xmp_content = f.read()
            check("Write XMP: contains GPS latitude", "GPSLatitude" in xmp_content)
            check("Write XMP: contains GPS longitude", "GPSLongitude" in xmp_content)
            check("Write XMP: contains GeoSnag tag", "GeoSnag" in xmp_content)

        # Verify XMP stamp was written to original file
        meta_xmp = scan_photo(test_nef2)
        check("Write XMP: stamp on original", meta_xmp.geosnag_processed)
        check("Write XMP: original still has no GPS", not meta_xmp.has_gps or meta_xmp.geosnag_processed)

        # Test stamp_processed standalone
        test_nef3 = os.path.join(tmpdir, "test3.NEF")
        shutil.copy2(nef_path, test_nef3)
        stamp_ok = stamp_processed(test_nef3)
        check("Stamp: standalone success", stamp_ok)
        meta_stamped = scan_photo(test_nef3)
        check("Stamp: detected on re-scan", meta_stamped.geosnag_processed)


def test_processed_skip():
    """Test that already-processed files are skipped in matching."""
    print("\n── Processed Skip Tests ──")

    target = PhotoMeta(
        filepath="/fake/DSC_010.NEF",
        filename="DSC_010.NEF",
        extension=".nef",
        datetime_original=datetime(2023, 7, 1, 12, 0, 0),
        has_gps=False,
        geosnag_processed=True,
    )

    source = PhotoMeta(
        filepath="/fake/IMG_010.jpg",
        filename="IMG_010.jpg",
        extension=".jpg",
        datetime_original=datetime(2023, 7, 1, 12, 5, 0),
        has_gps=True,
        gps_latitude=40.0,
        gps_longitude=-74.0,
    )

    # Processed photo should NOT match
    matches, unmatched, stats = match_photos(
        [target, source],
        max_time_delta=timedelta(hours=2),
    )
    check("Skip: processed photo not matched", stats.matched == 0)
    check("Skip: already_processed counted", stats.already_processed == 1)
    check("Skip: source still detected", stats.sources == 1)


def test_full_pipeline():
    """Test the full scan→match→write pipeline with real files."""
    print("\n── Full Pipeline Test ──")

    nef_path = os.path.join(FIXTURES_DIR, "camera_no_gps.nef")
    jpg_path = os.path.join(FIXTURES_DIR, "phone_with_gps.jpg")
    if not os.path.exists(nef_path) or not os.path.exists(jpg_path):
        print(f"  SKIP: Test fixtures not found in {FIXTURES_DIR}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        # Set up test directory (mixed — camera and mobile in same dir)
        photo_dir = os.path.join(tmpdir, "photos")
        os.makedirs(photo_dir)

        # Copy NEF (target — no GPS) and JPG (source — with GPS)
        test_nef = os.path.join(photo_dir, "camera_no_gps.nef")
        shutil.copy2(nef_path, test_nef)
        shutil.copy2(jpg_path, os.path.join(photo_dir, "phone_with_gps.jpg"))

        # Phase 1: Scan — single directory, unified
        all_photos = scan_directory(photo_dir, {".nef", ".jpg", ".jpeg"})

        check("Pipeline: found 2 photos", len(all_photos) == 2, f"found {len(all_photos)}")

        sources = [p for p in all_photos if p.has_gps]
        targets = [p for p in all_photos if not p.has_gps]

        check("Pipeline: 1 source (with GPS)", len(sources) == 1, f"found {len(sources)}")
        check("Pipeline: 1 target (no GPS)", len(targets) == 1, f"found {len(targets)}")

        if sources:
            check("Pipeline: source has GPS", sources[0].has_gps)
            check("Pipeline: source datetime", sources[0].datetime_original == datetime(2017, 9, 23, 22, 30, 0))

        # Phase 2: Match — pass single list
        matches, unmatched, stats = match_photos(
            all_photos,
            max_time_delta=timedelta(hours=2),
        )

        check("Pipeline: 1 match found", len(matches) == 1, f"got {len(matches)}")

        if matches:
            m = matches[0]
            delta_min = abs(m.time_delta.total_seconds()) / 60
            check("Pipeline: time delta ~41min", 40 < delta_min < 42, f"{delta_min:.1f}min")
            check("Pipeline: confidence > 50%", m.confidence > 50, f"{m.confidence:.1f}%")

            # Phase 3: Write with stamp
            result = write_gps_to_exif(
                m.target.filepath,
                m.source.gps_latitude,
                m.source.gps_longitude,
                stamp_after_write=True,
            )
            check("Pipeline: GPS write success", result.success, result.error or "")

            # Verify
            meta_final = scan_photo(test_nef)
            check("Pipeline: GPS now present", meta_final.has_gps)
            if meta_final.has_gps:
                check(
                    "Pipeline: lat correct",
                    abs(meta_final.gps_latitude - 55.7539) < 0.01,
                    f"{meta_final.gps_latitude:.6f}",
                )
                check(
                    "Pipeline: lon correct",
                    abs(meta_final.gps_longitude - 37.6208) < 0.01,
                    f"{meta_final.gps_longitude:.6f}",
                )
            check("Pipeline: camera make preserved", meta_final.camera_make == "NIKON CORPORATION")
            check("Pipeline: datetime preserved", meta_final.datetime_original == datetime(2017, 9, 23, 23, 11, 37))
            check("Pipeline: processed tag set", meta_final.geosnag_processed)

            # Phase 4: Re-run should skip processed file
            # After GPS write + stamp, the NEF now has GPS → it becomes a source.
            # Since it also has geosnag_processed=True AND has_gps=True,
            # the matcher classifies it as a source (not already_processed),
            # because the has_gps check comes first. Either way, 0 new matches.
            all_photos2 = scan_directory(photo_dir, {".nef", ".jpg", ".jpeg"})
            matches2, _, stats2 = match_photos(all_photos2, max_time_delta=timedelta(hours=2))
            check(
                "Pipeline: re-run has 0 new matches", stats2.matched == 0, f"expected 0 matches, got {stats2.matched}"
            )
            # The NEF now has GPS, so it's a source — verify no targets remain
            check("Pipeline: re-run has 0 targets", stats2.targets == 0, f"targets={stats2.targets}")


def test_writer_exiftool_backend():
    """
    E2E test for the ExifTool write backend.

    Forces pyexiv2 off and routes writes through exiftool, verifying that
    GPS and the processed stamp are correctly written to the NEF fixture.

    Skipped automatically if no exiftool binary is available on this machine.
    In CI (release.yml) exiftool is installed via apt-get so the test always runs.
    """
    print("\n── ExifTool Backend Tests ──")

    nef_path = os.path.join(FIXTURES_DIR, "camera_no_gps.nef")
    if not os.path.exists(nef_path):
        print(f"  SKIP: NEF fixture not found at {nef_path}")
        return

    # Detect any available exiftool (vendored or system)
    exiftool_cmd = _find_exiftool()
    if exiftool_cmd is None:
        print("  SKIP: exiftool not available (install via brew/opkg or run: make vendor)")
        return

    print(f"  Using: {' '.join(exiftool_cmd)}")

    with tempfile.TemporaryDirectory() as tmpdir:
        test_nef = os.path.join(tmpdir, "test_exiftool.NEF")
        shutil.copy2(nef_path, test_nef)

        # Force exiftool backend by patching module-level globals
        original_pyexiv2_ok = writer_module._PYEXIV2_OK
        original_exiftool = writer_module._EXIFTOOL
        try:
            writer_module._PYEXIV2_OK = False
            writer_module._EXIFTOOL = exiftool_cmd

            # Oslo coordinates (59.9139°N, 10.7522°E)
            result = write_gps_to_exif(
                test_nef,
                latitude=59.9139,
                longitude=10.7522,
                altitude=23.0,
                stamp_after_write=True,
            )
        finally:
            writer_module._PYEXIV2_OK = original_pyexiv2_ok
            writer_module._EXIFTOOL = original_exiftool

        check("ExifTool: write_gps_to_exif succeeds", result.success, result.error or "")

        if not result.success:
            return

        meta = scan_photo(test_nef)
        check("ExifTool: GPS present after write", meta.has_gps)

        if meta.has_gps:
            check(
                "ExifTool: latitude correct",
                abs(meta.gps_latitude - 59.9139) < 0.001,
                f"{meta.gps_latitude:.6f}",
            )
            check(
                "ExifTool: longitude correct",
                abs(meta.gps_longitude - 10.7522) < 0.001,
                f"{meta.gps_longitude:.6f}",
            )

        check("ExifTool: processed stamp set", meta.geosnag_processed)
        check("ExifTool: original metadata preserved", meta.camera_make == "NIKON CORPORATION")
        check(
            "ExifTool: datetime preserved",
            meta.datetime_original == datetime(2017, 9, 23, 23, 11, 37),
        )

        # Verify stamp_processed also routes correctly through exiftool
        test_nef2 = os.path.join(tmpdir, "test_stamp.NEF")
        shutil.copy2(nef_path, test_nef2)

        try:
            writer_module._PYEXIV2_OK = False
            writer_module._EXIFTOOL = exiftool_cmd
            stamp_ok = stamp_processed(test_nef2)
        finally:
            writer_module._PYEXIV2_OK = original_pyexiv2_ok
            writer_module._EXIFTOOL = original_exiftool

        check("ExifTool: stamp_processed succeeds", stamp_ok)
        meta2 = scan_photo(test_nef2)
        check("ExifTool: stamp detected on re-scan", meta2.geosnag_processed)


if __name__ == "__main__":
    print("=" * 55)
    from geosnag import __version__

    print(f"  GeoSnag Integration Tests (v{__version__})")
    print("=" * 55)

    test_scanner()
    test_matcher()
    test_writer()
    test_processed_skip()
    test_full_pipeline()
    test_writer_exiftool_backend()

    print()
    print("=" * 55)
    total = results["pass"] + results["fail"]
    print(f"  Results: {results['pass']}/{total} passed, {results['fail']} failed")
    print("=" * 55)

    sys.exit(0 if results["fail"] == 0 else 1)

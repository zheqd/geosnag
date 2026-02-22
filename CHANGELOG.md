# Changelog

All notable changes to GeoSnag will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.2.0] — 2026-02-22

### Added

- **ExifTool fallback backend** (`geosnag/writer.py`). When `pyexiv2`/`libexiv2` is
  unavailable (e.g. Synology DSM with glibc < 2.32), GPS writes automatically fall back
  to ExifTool. Probe order: vendored copy first, then system binary
  (`exiftool`, `/opt/bin/exiftool`, `/usr/bin/exiftool`).
- **Vendored ExifTool** (`geosnag/vendor/exiftool/`). A copy of ExifTool 13.50 can be
  bundled into the wheel via `python scripts/download_exiftool.py` (run by CI before
  `python -m build`). The vendor directory is `.gitignore`d but included in the
  sdist/wheel via `MANIFEST.in` and `[tool.setuptools.package-data]`.
- **`scripts/download_exiftool.py`** — downloads ExifTool from GitHub, extracts it to
  the vendor directory, and verifies the install with `perl exiftool -ver`.
- **Unit tests for the writer module** (`tests/test_writer.py`, 46 tests). Covers
  `_probe_cmd`, `_has_pyexiv2`, `_find_exiftool` (vendored-before-system probe order),
  `_write_gps_exiftool` (all argument combinations including altitude and stamp),
  `_stamp_exiftool`, and the pyexiv2/exiftool/neither routing in `write_gps_to_exif`
  and `stamp_processed`.
- **CLI end-to-end tests** (`tests/test_e2e.py`, 32 tests). Invokes
  `python -m geosnag.cli` as a subprocess and verifies exit codes, stdout content, and
  filesystem side effects (GPS written, XMP sidecar created, CSV report saved, stamp
  set, re-run skips processed files).
- **ExifTool integration test** in `tests/test_integration.py`. Patches `_PYEXIV2_OK=False`
  and runs a real ExifTool write against the NEF fixture, verifying GPS coordinates and
  the processed stamp are present after the write.
- `libimage-exiftool-perl` added to CI apt-get dependencies so the ExifTool integration
  test runs in GitHub Actions.

### Changed

- **Processed-file stamp field migrated from `Exif.Photo.UserComment` to
  `Exif.Image.Software`**. The new field has no charset-prefix ambiguity and is
  semantically more appropriate for a tool tag. All three scanner paths updated
  (exifread: `"Image Software"` key; pyexiv2: `PROJECT_TAG`; Pillow/HEIC: IFD0 tag
  `0x0131`). **Note**: files stamped by v0.1.x will not be recognised as processed and
  will be re-evaluated once, after which they receive the new stamp.
- `PROJECT_TAG` constant in `geosnag/__init__.py` changed from
  `Exif.Photo.UserComment` to `Exif.Image.Software`. All writer and scanner code derives
  the field name from this constant.
- ExifTool stamp argument changed from `-UserComment=` to `-Software=` to match the
  migrated field.
- `test_e2e.py` renamed to `test_integration.py` (Python-import–level tests that invoke
  real files). CLI subprocess tests added as a new `test_e2e.py`.
- `Makefile` `test` target updated to include `test_writer.py` and the new `test_e2e.py`.
- `release.yml` CI workflow: installs `libimage-exiftool-perl`, runs
  `download_exiftool.py` before `python -m build`, runs all four test suites.

### Fixed

- ExifTool was writing `UserComment` with a `charset=Ascii ` prefix that caused the
  `startswith("GeoSnag:")` check in the scanner to silently fail — processed files were
  being rescanned on every run. Eliminated entirely by migrating to `Exif.Image.Software`,
  which ExifTool writes without a charset prefix.

---

## [0.1.1] — 2025-02-21

### Changed

- Removed the automatic backup feature (`.geosnag.bak` files). `pyexiv2`/`libexiv2`
  prepares the full EXIF structure in memory before touching the file, making the extra
  copy unnecessary and cluttering the photo directory.

---

## [0.1.0] — 2025-02-21

Initial release. Core features:

- EXIF GPS coordinate reading and writing
- Timestamp-based matching algorithm with confidence scoring
- Unified `scan_dirs` configuration — auto-classifies photos as sources (with GPS) or
  targets (without GPS)
- Support for JPG, ARW, NEF, CR2, CR3, DNG, ORF, RAF, RW2, HEIC, HEIF, PNG
- Write modes: EXIF, XMP sidecar, or both
- Processed-file tagging via `Exif.Photo.UserComment` for safe re-runs
- Scan index cache — skip EXIF reads for unchanged files between runs
- Multithreaded scanning with configurable worker count
- Dry-run mode by default — no files modified unless `--apply` is passed
- CSV match report export (`--report`)
- Configurable time window, exclusion patterns
- Designed for Synology NAS (`@eaDir`, `#recycle` exclusions)
- Standard pip-installable Python package with CLI entry point

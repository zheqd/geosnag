# Changelog

All notable changes to GeoSnag will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.2.1] — 2026-02-22

### Fixed

- **PIL Image file descriptor leak** in HEIC scanner — `Image.open()` was not closed
  on exception. Now uses a context manager (`with Image.open(...) as img:`).
- **pyexiv2 Image leak** in scanner and writer — `img.close()` was skipped when
  `read_exif()` or `modify_exif()` raised. Now wrapped in `try/finally` (3 sites).
- **GPS coordinate validation** added to `write_gps_to_exif()` — rejects writes with
  out-of-range latitude/longitude before calling the backend.
- **CLI backend validation** — `geosnag --apply` now exits immediately with a clear
  install message when no EXIF write backend is available (instead of scanning 2889
  files and failing each one individually). Dry-run mode is unaffected.
- Outdated `UserComment` references in `stamp_processed()` docstring and
  `config.example.yaml` comment updated to `Exif.Image.Software`.
- Hardcoded version `v0.1.1` in `test_integration.py` replaced with dynamic
  `__version__` import.
- `matches_report.html` added to `.gitignore`.

### Changed

- **`pyexiv2` moved to optional dependency.** Core install (`pip install geosnag`)
  now requires `exifread`, `pillow-heif`, and `PyYAML`. Install pyexiv2 via
  `pip install geosnag[all]`. This unblocks installation on Synology NAS where
  `libexiv2` is unavailable.
- **`pillow-heif` promoted to core dependency.** HEIC/HEIF is the default iPhone
  format and too common to leave optional. The `[heic]` extra has been removed.
- **ExifTool vendoring removed.** The 20 MB vendored Perl distribution still required
  a system Perl interpreter, providing no benefit over `opkg install perl-image-exiftool`.
  Removed `scripts/download_exiftool.py`, `geosnag/vendor/`, related CI bundle step,
  `MANIFEST.in` vendor include, and `[tool.setuptools.package-data]`.
- `_find_exiftool()` simplified to probe system binaries only: `exiftool`,
  `/opt/bin/exiftool`, `/usr/bin/exiftool`.
- Explicit `encoding="utf-8"` added to exiftool `subprocess.run()` calls for
  robustness with non-ASCII file paths.
- Version string deduplicated — `__init__.py` now reads from `importlib.metadata`
  instead of duplicating the version from `pyproject.toml`.
- Redundant `Pillow` direct dependency removed (pulled transitively by `pillow-heif`).
- Release workflow split into `build` and `publish` jobs; `publish` uses
  `environment: pypi` for OIDC trusted publishing with optional approval gate.
- README quick start updated with `pip install geosnag[all]` as primary and
  Synology-specific minimal install path.
- `CODEOWNERS` and importable GitHub branch ruleset added.

---

## [0.2.0] — 2026-02-22

### Added

- **ExifTool fallback backend** (`geosnag/writer.py`). When `pyexiv2`/`libexiv2` is
  unavailable (e.g. Synology DSM with glibc < 2.32), GPS writes automatically fall back
  to ExifTool. Probed at: `exiftool`, `/opt/bin/exiftool`, `/usr/bin/exiftool`.
- **Unit tests for the writer module** (`tests/test_writer.py`). Covers `_probe_cmd`,
  `_has_pyexiv2`, `_find_exiftool`, `_write_gps_exiftool` (all argument combinations),
  `_stamp_exiftool`, and the pyexiv2/exiftool/neither routing.
- **CLI end-to-end tests** (`tests/test_e2e.py`, 32 tests). Invokes
  `python -m geosnag.cli` as a subprocess and verifies exit codes, stdout content, and
  filesystem side effects.
- **ExifTool integration test** in `tests/test_integration.py`. Patches `_PYEXIV2_OK=False`
  and runs a real ExifTool write against the NEF fixture.
- `libimage-exiftool-perl` added to CI apt-get dependencies.

### Changed

- **Processed-file stamp field migrated from `Exif.Photo.UserComment` to
  `Exif.Image.Software`**. The new field has no charset-prefix ambiguity and is
  semantically more appropriate for a tool tag. All three scanner paths updated.
  **Note**: files stamped by v0.1.x will be re-evaluated once, after which they
  receive the new stamp.
- `test_e2e.py` renamed to `test_integration.py`. CLI subprocess tests added as a
  new `test_e2e.py`.

### Fixed

- ExifTool `UserComment` charset-prefix bug that caused processed files to be
  rescanned on every run. Eliminated by migrating to `Exif.Image.Software`.

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

# Changelog

All notable changes to GeoSnag will be documented in this file.

## [0.1.0] — 2025-02-21

### Initial Release

- EXIF GPS coordinate reading and writing
- Timestamp-based matching algorithm with confidence scoring
- Unified `scan_dirs` configuration — auto-classifies photos as sources (with GPS) or targets (without GPS)
- Support for JPG, ARW, NEF, CR2, CR3, DNG, ORF, RAF, RW2, HEIC, HEIF, PNG
- Write modes: EXIF, XMP sidecar, or both
- Processed-file tagging via `Exif.Photo.UserComment` for safe re-runs
- Scan index cache — skip EXIF reads for unchanged files between runs
- Match cache — skip re-evaluation of unmatched targets when GPS sources haven't changed
- Multithreaded scanning with configurable worker count
- Dry-run mode by default — no files modified unless `--apply` is passed
- Backup file creation (`.geosnag.bak`) before every modification
- CSV match report export (`--report`)
- Configurable time window, confidence threshold, exclusion patterns
- Designed for Synology NAS (`@eaDir`, `#recycle` exclusions)
- Standard pip-installable Python package with CLI entry point

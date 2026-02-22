# GeoSnag

**Automatically enrich GPS-less camera photos using coordinates from phone photos taken on the same day.**

You shoot with a DSLR or mirrorless camera that has no GPS module. Your phone is always in your pocket, geotagging every shot. After a trip, your camera photos have no location data while your phone photos do.

GeoSnag fixes this. Point it at your photo directories, and it matches each GPS-less photo to the nearest geotagged photo by timestamp — then copies the coordinates over. No manual sorting, no separate camera/phone folder setup. It just figures it out.

Built for Synology NAS, but works anywhere Python runs.

## How It Works

GeoSnag runs a four-stage pipeline:

1. **Scan** — reads EXIF metadata from every photo in your configured directories, using multithreaded I/O and an on-disk index cache so repeat runs are fast.
2. **Classify** — photos that already carry GPS coordinates become *sources*. Photos without GPS become *targets*. This happens automatically based on metadata — no folder-based configuration needed.
3. **Match** — for each target, GeoSnag finds the closest source by timestamp within the same calendar day. Each match gets a confidence score based on how close in time the two shots were. You can set a maximum time window (default ±2 hours) and a minimum confidence threshold.
4. **Write** — copies GPS coordinates from the matched source into the target. Supports writing directly to EXIF, creating XMP sidecar files, or both. Every modification is preceded by a backup.

## Features

### Broad Format Support

GeoSnag reads and writes EXIF metadata for all major camera formats: JPEG, ARW (Sony), NEF (Nikon), CR2 and CR3 (Canon), DNG, ORF (Olympus), RAF (Fuji), RW2 (Panasonic), PNG, HEIC, and HEIF. HEIC/HEIF support is provided through pillow-heif, so Apple ProRAW and modern iPhone photos work out of the box.

### Intelligent Matching

Matching is timestamp-based with per-match confidence scoring. A phone photo taken 5 minutes before your camera shot scores higher than one taken 90 minutes later. The configurable time window (default ±2 hours, adjustable via `--max-delta`) and optional minimum confidence threshold let you tune the tradeoff between coverage and accuracy. All matching is day-scoped — GeoSnag won't accidentally pair a Monday photo with a Tuesday GPS source.

### Three Write Modes

Choose how GPS data is written to your files:

- **`exif`** (default) — writes GPS coordinates directly into the photo's EXIF metadata using pyexiv2. This is the most compatible option: photo apps, map views, and cloud services will see the location immediately.
- **`xmp_sidecar`** — creates a `.xmp` companion file next to the original. Non-destructive — the original file is never modified. Useful for RAW workflows where you want to preserve the original file bit-for-bit.
- **`both`** — writes EXIF and creates an XMP sidecar. Belt and suspenders.

### Performance at Scale

Photo libraries on a NAS can contain hundreds of thousands of files. GeoSnag is designed to handle this efficiently:

- **Scan index** — after the first run, EXIF metadata is cached to `.geosnag_index.json`. Subsequent runs only read metadata from new or modified files, detected by file size and modification time. A full rescan of 100k+ photos that takes minutes on first run completes in seconds on the next.
- **Match cache** — targets that had no match are remembered along with a fingerprint of the available GPS sources. If sources haven't changed, there's no point re-evaluating those targets. This is especially valuable for libraries where new photos are added incrementally.
- **Multithreaded scanning** — EXIF reads are parallelized across configurable worker threads (default: 4). On NAS hardware with slow disks but multiple cores, this makes a significant difference.

### Safe by Default

GeoSnag defaults to dry-run mode. Running `geosnag` without `--apply` scans everything, finds all matches, prints a summary — and touches nothing. You review the output, then run `geosnag --apply` when you're satisfied.

Additional safety measures:

- **No backup files needed** — pyexiv2 is built on libexiv2, a mature C++ library that has been the EXIF standard for 20+ years (used by Lightroom, digiKam, darktable, ExifTool). libexiv2 performs writes atomically: it parses and modifies the full EXIF structure in memory, then writes it back in a single operation. If anything fails — disk full, permission denied, corrupt header — it raises an exception before the file is touched. The original is never partially overwritten.
- **Processed-file tagging** — after writing GPS data, GeoSnag stamps the file's `Exif.Image.Software` tag with a `GeoSnag:` marker. On future runs, tagged files are automatically skipped. This prevents double-processing and makes re-runs safe.
- **GPS validation** — latitude and longitude values are range-checked before writing. Invalid coordinates from corrupted EXIF data won't propagate.
- **CSV reports** — use `--report matches.csv` to export the full match table for review before committing to `--apply`.

### NAS-Friendly

GeoSnag was designed for Synology NAS environments:

- Automatically excludes Synology thumbnail directories (`@eaDir`) and recycle bins (`#recycle`).
- Configurable glob-based exclusion patterns for anything else you want to skip.
- Runs on Python 3.9 (available via Synology Package Center) with minimal dependencies.
- Low memory footprint — metadata is processed in streaming fashion, not loaded entirely into RAM.

## Quick Start

```bash
pip install geosnag

curl -O https://raw.githubusercontent.com/zheqd/geosnag/main/config.example.yaml
cp config.example.yaml config.yaml
nano config.yaml    # set your photo directories

geosnag             # dry run — preview matches
geosnag --apply     # write GPS data
```

If PyPI is not available (e.g. air-gapped Synology NAS), install the latest wheel directly from GitHub:

```bash
pip install "$(curl -s https://api.github.com/repos/zheqd/geosnag/releases/latest \
  | grep browser_download_url | grep '\.whl' | cut -d'"' -f4)"
```

See [INSTALL.md](INSTALL.md) for detailed Synology NAS setup instructions.

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit it:

Always **single-quote paths** in `scan_dirs`. Unquoted paths with `!` cause a YAML parse error, and paths with ` #` are silently truncated at the `#` (treated as a comment). Single quotes are safe for all characters.

```yaml
scan_dirs:
  - '/volume1/photo/personal'
  - '/volume1/photo/shared'
  - '/volume1/homes/user/Photos/!PhotoLibrary/2016/08'  # ← quotes required
  - '/volume1/homes/user/photo library'                  # ← quotes required

recursive: true

extensions:
  - .jpg
  - .jpeg
  - .arw
  - .nef
  - .cr2
  - .cr3
  - .dng
  - .heic
  - .heif
  - .png

matching:
  max_time_delta_minutes: 120   # ±2 hours
  min_confidence: 0             # 0–100, reject matches below this

write_mode: exif          # exif | xmp_sidecar | both
create_backup: true
skip_processed: true
dry_run: true             # always start with dry run
workers: 4                # parallel scan threads
log_level: INFO

exclude_patterns:
  - "*/@eaDir/*"
  - "*/#recycle/*"
  - "*.geosnag.bak"
```

All directories in `scan_dirs` are scanned the same way. Photos with GPS become sources, photos without GPS become targets. There is no separate camera/mobile directory configuration.

## CLI Reference

```
geosnag [OPTIONS]

--apply                  Write GPS data (default is dry run)
--dry-run, -n            Force dry run even if config says otherwise
--config, -c PATH        Path to config file (default: config.yaml)
--report, -r PATH        Save match report to CSV
--write-mode, -w MODE    exif | xmp_sidecar | both
--max-delta, -d MINUTES  Max time difference for matching
--workers N              Number of parallel scan threads
--reindex                Force full rescan, ignore cached index
--rematch                Force re-evaluation of all targets, ignore match cache
--no-index               Disable scan index entirely
--no-skip-processed      Re-process files already tagged by GeoSnag
--preview-count N        Number of matches to preview (default: 20)
--verbose, -v            Enable debug logging
```

You can also run GeoSnag as a Python module: `python -m geosnag [OPTIONS]`.

## Project Structure

```
geosnag/
  __init__.py        Version and project-wide constants
  __main__.py        python -m geosnag entry point
  cli.py             CLI argument parsing and pipeline orchestration
  scanner.py         EXIF metadata reader (supports all listed formats)
  matcher.py         Timestamp-based GPS matching with confidence scoring
  writer.py          GPS coordinate writer (EXIF and XMP sidecar)
  index.py           Scan index cache for incremental runs
  parallel.py        Multithreaded scanning
tests/
  test_e2e.py        End-to-end pipeline tests (61 tests)
  test_index.py      Index and cache tests (107 tests)
pyproject.toml       Package metadata, dependencies, CLI entry point
config.example.yaml  Configuration template
```

## Requirements

- Python 3.9 or later
- **exifread** — EXIF metadata reading
- **pyexiv2** — EXIF/XMP metadata writing (requires libexiv2 system library)
- **pillow-heif** — HEIC/HEIF format support
- **Pillow** — image processing
- **PyYAML** — configuration file parsing

All Python dependencies are installed automatically via `pip install .`. On Synology NAS, you may need to install `libexiv2` separately — see [INSTALL.md](INSTALL.md).

## Testing

```bash
python tests/test_e2e.py      # 61 end-to-end tests
python tests/test_index.py    # 107 index/cache tests
```

Or with pytest:

```bash
pip install pytest
pytest tests/ -v
```

## License

Apache 2.0 — see [LICENSE](LICENSE).

# GeoSnag — Installation & Usage

## Prerequisites

- Synology NAS with SSH enabled (DSM → Control Panel → Terminal & SNMP → Enable SSH)
- Python 3.9+ (check with `python3 --version`)

If Python is missing, install via Synology Package Center (search "Python") or use `opkg install python3` if Entware is set up.

## Install

```bash
# SSH into your NAS
ssh your_user@your_nas_ip

# Create a working directory
mkdir -p /volume1/tools/geosnag
cd /volume1/tools/geosnag

# Clone from GitHub and install
git clone https://github.com/zheqd/geosnag.git .
pip3 install --break-system-packages .
```

If `pip3` is not available, try `python3 -m pip install ...` instead.

## Configure

Create your config from the template and edit it:

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` to point at your photo directories:

```yaml
scan_dirs:
  - /volume1/photo/personal
  - /volume1/photo/shared

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
  max_time_delta_minutes: 120
  min_confidence: 0

write_mode: exif
create_backup: true
skip_processed: true
dry_run: true
log_level: INFO
log_file: /volume1/tools/geosnag/geosnag.log

exclude_patterns:
  - "*/@eaDir/*"
  - "*/#recycle/*"
  - "*.geosnag.bak"
```

All directories are scanned the same way. Photos with GPS become sources, photos without GPS become targets. No mobile/camera distinction.

## Run — Dry Mode (preview, no changes)

```bash
geosnag
# or: python3 -m geosnag
```

Dry run is the default. It scans all directories, finds matches, and prints a report without modifying any files.

To save a CSV report:

```bash
geosnag --report matches.csv
```

## Run — Apply Mode (write GPS data)

```bash
geosnag --apply
```

This writes GPS coordinates into the target files and stamps each processed file with a GeoSnag tag so it will be skipped on future runs. Backup files (`.geosnag.bak`) are created before each modification.

## CLI Options

```
--apply              Write GPS data (default is dry run)
--dry-run, -n        Force dry run even if config says otherwise
--config, -c PATH    Use a different config file
--report, -r PATH    Save match report to CSV
--write-mode, -w     exif | xmp_sidecar | both
--max-delta, -d MIN  Override max time delta (minutes)
--no-skip-processed  Re-process files that were already geo-tagged
--workers N          Parallel scan threads (default: 4)
--reindex            Force full rescan (ignore cache)
--rematch            Force re-evaluation of all targets
--no-index           Disable scan index entirely
--verbose, -v        Show debug output
--preview-count N    Number of matches to preview (default: 20)
```

## Examples

Dry run with verbose output:

```bash
geosnag -v
```

Apply with stricter time matching (30 min max) and XMP sidecars:

```bash
geosnag --apply --max-delta 30 --write-mode xmp_sidecar
```

Re-run on already processed files:

```bash
geosnag --apply --no-skip-processed
```

## Re-runs

After applying, GeoSnag writes a marker tag to each enriched file. On the next run these files are detected during scanning and skipped automatically. This means you can safely re-run after adding new photos — only unprocessed files without GPS will be matched.

## Verify

Run the built-in test suite:

```bash
python3 tests/test_e2e.py
python3 tests/test_index.py
```

Expected: 61/61 E2E passed, 57/57 pytest passed.

## Development (macOS)

`pyexiv2` links against `libexiv2` which depends on `inih`. Install it via Homebrew before setting up the venv:

```bash
brew install inih
```

Then create a virtual environment and install with dev dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install '.[dev]'
```

Run tests:

```bash
python -m pytest tests/test_index.py tests/test_special_paths.py -v
python tests/test_e2e.py -v
```

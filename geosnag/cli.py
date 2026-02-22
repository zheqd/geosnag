#!/usr/bin/env python3
"""
GeoSnag — Photo Geo-Tagging Tool for Synology NAS

Enriches photos without GPS data using GPS coordinates from other photos
taken on the same day. Auto-detects sources and targets — no separate
camera/mobile directory configuration needed.

Usage:
    geosnag                                  # Dry run with config.yaml
    geosnag --config my_config.yaml
    geosnag --apply                          # Actually write GPS data
    geosnag --dry-run                        # Preview matches only
    geosnag --report report.csv              # Save match report to CSV
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import timedelta

import yaml

from . import INDEX_FILENAME, PROJECT_NAME
from . import __version__ as VERSION
from .index import ScanIndex
from .matcher import MatchStats, match_photos
from .parallel import scan_with_index
from .scanner import PHOTO_EXTS
from .writer import _EXIFTOOL, _PYEXIV2_OK, write_gps_to_exif, write_gps_xmp_sidecar


def _build_source_fingerprints(all_photos: list) -> dict:
    """Precompute per-date fingerprint of GPS source filepaths.

    Returns a dict mapping date_key (YYYY-MM-DD) to a truncated SHA-256 hex
    digest.  The fingerprint changes whenever a GPS source is added or removed
    for that date, invalidating any cached "no_match" results for targets on
    that day.
    """
    by_date: dict[str, list[str]] = defaultdict(list)
    for p in all_photos:
        if p.has_gps and p.datetime_original:
            by_date[p.date_key].append(p.filepath)
    return {date: hashlib.sha256("|".join(sorted(paths)).encode()).hexdigest()[:16] for date, paths in by_date.items()}


def setup_logging(level: str = "INFO", log_file: str = None):
    """Configure logging with console and optional file output."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def load_config(config_path: str) -> dict:
    """Load and validate configuration from YAML file."""
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    # Defaults
    config.setdefault("recursive", True)
    config.setdefault("write_mode", "exif")
    config.setdefault("dry_run", True)
    config.setdefault("skip_processed", True)
    config.setdefault("log_level", "INFO")
    config.setdefault("log_file", None)
    config.setdefault("exclude_patterns", [])
    config.setdefault("use_index", True)
    config.setdefault("workers", 4)

    matching = config.setdefault("matching", {})
    matching.setdefault("max_time_delta_minutes", 120)
    matching.setdefault("min_confidence", 0)

    # Unified scan_dirs — also support legacy camera_dirs/mobile_dirs
    scan_dirs = config.get("scan_dirs", [])
    if not scan_dirs:
        # Backwards compatibility: merge camera_dirs and mobile_dirs
        scan_dirs = list(config.get("camera_dirs", [])) + list(config.get("mobile_dirs", []))
    config["scan_dirs"] = scan_dirs

    # Unified extensions — also support legacy camera_extensions/mobile_extensions
    extensions = config.get("extensions", [])
    if not extensions:
        cam_exts = config.get("camera_extensions", [])
        mob_exts = config.get("mobile_extensions", [])
        extensions = list(set(cam_exts) | set(mob_exts))
    if not extensions:
        extensions = list(PHOTO_EXTS)
    config["extensions"] = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}

    return config


def print_banner():
    """Print startup banner."""
    title = f"{PROJECT_NAME} v{VERSION}"
    # Dynamic box width to accommodate any name
    inner_width = max(len(title) + 4, 41)
    print()
    print(f"  ┌{'─' * inner_width}┐")
    print(f"  │  {title:<{inner_width - 2}}│")
    print(f"  │  {'Photo Geo-Tagging for Synology NAS':<{inner_width - 2}}│")
    print(f"  └{'─' * inner_width}┘")
    print()


def print_scan_summary(all_photos: list, stats: MatchStats):
    """Print summary of scanned photos."""
    total = len(all_photos)
    with_gps = sum(1 for p in all_photos if p.has_gps)
    with_dt = sum(1 for p in all_photos if p.datetime_original)
    errors = sum(1 for p in all_photos if p.scan_error)
    processed = sum(1 for p in all_photos if p.geosnag_processed)
    no_gps = sum(1 for p in all_photos if not p.has_gps and not p.geosnag_processed)
    eligible = sum(1 for p in all_photos if not p.has_gps and not p.geosnag_processed and p.datetime_original)

    # Unique devices
    devices = set()
    for p in all_photos:
        if p.camera_make or p.camera_model:
            devices.add(f"{p.camera_make or ''} {p.camera_model or ''}".strip())

    print("  SCAN RESULTS")
    print("  ────────────")
    print(f"  Total photos:       {total:>6d}")
    print(f"    With GPS:         {with_gps:>6d}  (usable as GPS sources)")
    print(f"    Without GPS:      {no_gps:>6d}  (candidates for enrichment)")
    print(f"    Already processed:{processed:>6d}  ({PROJECT_NAME} tag found, skipped)")
    print(f"    With datetime:    {with_dt:>6d}")
    print(f"    Eligible targets: {eligible:>6d}  (no GPS + has datetime + not processed)")
    print(f"    Scan errors:      {errors:>6d}")
    if devices:
        print(f"    Devices:          {', '.join(sorted(devices))}")
    print()


def print_match_summary(
    matches: list,
    unmatched: list,
    stats: MatchStats,
    cached_unmatched_count: int = 0,
):
    """Print summary of matching results."""
    print("  MATCHING RESULTS")
    print("  ────────────────")
    print(f"  GPS sources:        {stats.sources:>6d}  across {stats.source_dates} dates")
    print(f"  Eligible targets:   {stats.targets:>6d}")
    print(f"  Already processed:  {stats.already_processed:>6d}")
    print(f"  Matched:            {stats.matched:>6d}  ({stats.matched / max(stats.targets, 1) * 100:.1f}%)")
    print(f"  Unmatched:          {stats.unmatched:>6d}")
    print(f"  No datetime:        {stats.without_datetime:>6d}")
    if cached_unmatched_count > 0:
        print(f"  Cache skipped:      {cached_unmatched_count:>6d}  (unchanged since last run)")

    if matches:
        print()
        print(f"  Avg confidence:     {stats.avg_confidence:>6.1f}%")
        print(f"  Avg time delta:     {stats.avg_time_delta_min:>6.1f} min")

        # Confidence distribution
        conf_buckets = {"90-100%": 0, "70-89%": 0, "50-69%": 0, "< 50%": 0}
        for m in matches:
            if m.confidence >= 90:
                conf_buckets["90-100%"] += 1
            elif m.confidence >= 70:
                conf_buckets["70-89%"] += 1
            elif m.confidence >= 50:
                conf_buckets["50-69%"] += 1
            else:
                conf_buckets["< 50%"] += 1

        print()
        print("  Confidence distribution:")
        for bucket, count in conf_buckets.items():
            bar = "█" * int(count / max(len(matches), 1) * 30)
            print(f"    {bucket:>8s}: {count:>4d}  {bar}")

    print()


def print_match_preview(matches: list, max_show: int = 20):
    """Print a preview of matched pairs."""
    if not matches:
        return

    print("  MATCH PREVIEW (first {} of {})".format(min(max_show, len(matches)), len(matches)))
    print("  ─" * 50)
    print(f"  {'Target File':<42s} {'Δ Time':>10s} {'Conf':>5s}  {'GPS Source':<35s}")
    print(f"  {'─' * 42} {'─' * 10} {'─' * 5}  {'─' * 35}")

    for m in matches[:max_show]:
        tgt_name = os.path.basename(m.target.filepath)
        if len(tgt_name) > 40:
            tgt_name = tgt_name[:37] + "..."
        src_name = os.path.basename(m.source.filepath)
        if len(src_name) > 33:
            src_name = src_name[:30] + "..."

        print(f"  {tgt_name:<42s} {m.time_delta_str:>10s} {m.confidence:>5.1f}  {src_name:<35s}")

    if len(matches) > max_show:
        print(f"  ... and {len(matches) - max_show} more")
    print()


def save_report(
    matches: list,
    unmatched: list,
    stats: MatchStats,
    report_path: str,
):
    """Save detailed match report to CSV."""
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # Header
        writer.writerow(
            [
                "Status",
                "Target File",
                "Target DateTime",
                "Target Make/Model",
                "Source File",
                "Source DateTime",
                "Latitude",
                "Longitude",
                "Time Delta (min)",
                "Confidence (%)",
            ]
        )

        for m in matches:
            writer.writerow(
                [
                    "MATCHED",
                    m.target.filepath,
                    m.target.datetime_original.isoformat() if m.target.datetime_original else "",
                    f"{m.target.camera_make or ''} {m.target.camera_model or ''}".strip(),
                    m.source.filepath,
                    m.source.datetime_original.isoformat() if m.source.datetime_original else "",
                    f"{m.source.gps_latitude:.6f}" if m.source.gps_latitude else "",
                    f"{m.source.gps_longitude:.6f}" if m.source.gps_longitude else "",
                    f"{m.time_delta_minutes:.1f}",
                    f"{m.confidence:.1f}",
                ]
            )

        for u in unmatched:
            writer.writerow(
                [
                    "UNMATCHED",
                    u.filepath,
                    u.datetime_original.isoformat() if u.datetime_original else "",
                    f"{u.camera_make or ''} {u.camera_model or ''}".strip(),
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )

    logging.getLogger(PROJECT_NAME.lower()).info(f"Report saved to {report_path}")


def apply_matches(
    matches: list,
    write_mode: str,
    min_confidence: float,
) -> tuple:
    """Apply GPS data from matches to target files. Returns (success, fail) counts."""
    success = 0
    fail = 0
    apply_logger = logging.getLogger(f"{PROJECT_NAME.lower()}.apply")

    for i, m in enumerate(matches, 1):
        if m.confidence < min_confidence:
            apply_logger.debug(
                f"Skipping {m.target.filename}: confidence {m.confidence:.1f}% < threshold {min_confidence:.1f}%"
            )
            continue

        lat = m.source.gps_latitude
        lon = m.source.gps_longitude
        alt = m.source.gps_altitude

        results = []

        if write_mode in ("exif", "both"):
            result = write_gps_to_exif(
                m.target.filepath,
                lat,
                lon,
                alt,
                stamp_after_write=True,
            )
            results.append(result)

        if write_mode in ("xmp_sidecar", "both"):
            result = write_gps_xmp_sidecar(
                m.target.filepath,
                lat,
                lon,
                alt,
                stamp_after_write=(write_mode == "xmp_sidecar"),
            )
            results.append(result)

        if all(r.success for r in results):
            success += 1
        else:
            fail += 1
            for r in results:
                if not r.success:
                    apply_logger.error(f"  Write failed ({r.method}): {r.error}")

        if i % 50 == 0:
            apply_logger.info(f"  Progress: {i}/{len(matches)} ({success} OK, {fail} failed)")

    return success, fail


def main():
    parser = argparse.ArgumentParser(
        description=f"{PROJECT_NAME} — Enrich photos with GPS from other photos taken nearby in time",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  geosnag                                    Dry run with config.yaml
  geosnag --apply                            Write GPS data to files
  geosnag --config my.yaml                   Use custom config
  geosnag --report matches.csv               Save report to CSV
  geosnag --write-mode xmp_sidecar           Use XMP sidecars only
  geosnag --workers 8                        Use 8 scan threads
  geosnag --reindex                          Force full rescan (ignore cache)
        """,
    )
    parser.add_argument(
        "--config",
        "-c",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml in current directory)",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        default=None,
        help="Preview matches without writing (overrides config)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write GPS data (overrides dry_run in config)",
    )
    parser.add_argument(
        "--report",
        "-r",
        default=None,
        help="Save match report to CSV file",
    )
    parser.add_argument(
        "--write-mode",
        "-w",
        choices=["exif", "xmp_sidecar", "both"],
        default=None,
        help="GPS write method (overrides config)",
    )
    parser.add_argument(
        "--max-delta",
        "-d",
        type=int,
        default=None,
        help="Max time difference in minutes (overrides config)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    parser.add_argument(
        "--preview-count",
        type=int,
        default=20,
        help="Number of matches to preview (default: 20)",
    )
    parser.add_argument(
        "--no-skip-processed",
        action="store_true",
        help=f"Don't skip photos already processed by {PROJECT_NAME}",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of parallel scan threads (default: 4, from config)",
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="Force full rescan, ignore cached index",
    )
    parser.add_argument(
        "--no-index",
        action="store_true",
        help="Disable scan index entirely (don't read or write cache)",
    )
    parser.add_argument(
        "--rematch",
        action="store_true",
        help="Force re-evaluation of all targets, ignore match cache",
    )

    args = parser.parse_args()

    # Resolve config path relative to current working directory
    config_path = args.config
    if not os.path.isabs(config_path):
        config_path = os.path.join(os.getcwd(), config_path)

    if not os.path.exists(config_path):
        print(f"Error: Config file not found: {config_path}")
        print("Create one by copying config.example.yaml: cp config.example.yaml config.yaml")
        sys.exit(1)

    config = load_config(config_path)

    # CLI overrides
    if args.verbose:
        config["log_level"] = "DEBUG"
    if args.dry_run is True:
        config["dry_run"] = True
    if args.apply:
        config["dry_run"] = False
    if args.write_mode:
        config["write_mode"] = args.write_mode
    if args.max_delta is not None:
        config["matching"]["max_time_delta_minutes"] = args.max_delta
    if args.no_skip_processed:
        config["skip_processed"] = False
    if args.workers is not None:
        config["workers"] = args.workers
    if args.no_index:
        config["use_index"] = False

    setup_logging(config["log_level"], config.get("log_file"))
    logger = logging.getLogger(PROJECT_NAME.lower())

    print_banner()

    # ── Backend validation ──
    write_mode = config.get("write_mode", "exif")
    is_dry_run = config["dry_run"]
    if not is_dry_run and write_mode in ("exif", "both") and not _PYEXIV2_OK and not _EXIFTOOL:
        print("  ✗  No EXIF write backend available.")
        print()
        print("     pyexiv2 could not be loaded (glibc version mismatch).")
        print("     ExifTool was not found at: exiftool, /opt/bin/exiftool, /usr/bin/exiftool")
        print()
        print("     On Synology DSM, install ExifTool via Entware:")
        print("       opkg install perl-image-exiftool")
        print()
        print("     Then re-run geosnag --apply")
        sys.exit(1)

    is_dry_run = config["dry_run"]
    if is_dry_run:
        print("  ⚠  DRY RUN MODE — no files will be modified")
        print("     Use --apply to write GPS data")
    else:
        print("  ⚡ LIVE MODE — files will be modified")
    print()

    # ── Phase 1: Scan ──
    print("  Phase 1: Scanning photos...")
    print("  ─" * 25)
    t0 = time.time()

    scan_dirs = config.get("scan_dirs", [])
    if not scan_dirs:
        print("  No directories configured. Set scan_dirs in config.yaml.")
        sys.exit(1)

    # Set up index
    index = None
    if config.get("use_index", True):
        index_path = os.path.join(
            os.path.dirname(os.path.abspath(config_path)),
            INDEX_FILENAME,
        )
        index = ScanIndex(index_path)
        if args.reindex:
            logger.info("--reindex: clearing cached index")
            index.clear()
        else:
            index.load()
        # Validate match threshold (clears match cache if config changed)
        index.validate_match_threshold(config["matching"]["max_time_delta_minutes"])

    # Scan with threading + index
    workers = max(1, config.get("workers", 4))
    all_photos = scan_with_index(
        directories=scan_dirs,
        extensions=config["extensions"],
        recursive=config["recursive"],
        exclude_patterns=config.get("exclude_patterns", []),
        index=index,
        workers=workers,
    )

    # Save index after scan
    if index is not None:
        index.save()

    scan_time = time.time() - t0
    logger.info(f"Scan completed in {scan_time:.1f}s")
    print()

    if not all_photos:
        print("  No photos found. Check scan_dirs and extensions in config.yaml.")
        sys.exit(0)

    # ── Phase 2: Match ──
    print("  Phase 2: Matching photos by timestamp...")
    print("  ─" * 25)
    t1 = time.time()

    max_delta = timedelta(minutes=config["matching"]["max_time_delta_minutes"])
    min_conf = config["matching"].get("min_confidence", 0)

    # Filter out already-processed photos if configured
    photos_to_match = all_photos
    if config.get("skip_processed", True):
        photos_to_match = [p for p in all_photos if not p.geosnag_processed]

    # ── Match cache: skip targets confirmed unmatched on previous run ──
    cached_unmatched = []
    if index is not None and not getattr(args, "rematch", False):
        source_fps = _build_source_fingerprints(all_photos)
        filtered = []
        for p in photos_to_match:
            if p.has_gps:
                filtered.append(p)  # sources always included
                continue
            status, fp = index.get_match_result(p.filepath)
            if status == "no_match" and fp == source_fps.get(p.date_key):
                cached_unmatched.append(p)  # skip — same sources, same result
            else:
                filtered.append(p)
        photos_to_match = filtered
        if cached_unmatched:
            logger.info(f"Match cache: {len(cached_unmatched)} targets skipped (unmatched on previous run)")
    else:
        source_fps = None

    matches, unmatched, stats = match_photos(photos_to_match, max_time_delta=max_delta)

    # ── Update match cache in index ──
    if index is not None:
        if source_fps is None:
            source_fps = _build_source_fingerprints(all_photos)
        for m in matches:
            index.update_match_result(
                m.target.filepath,
                "matched",
                source_fps.get(m.target.date_key, ""),
            )
        for u in unmatched:
            index.update_match_result(
                u.filepath,
                "no_match",
                source_fps.get(u.date_key, ""),
            )
        index.save()

    # Merge cached unmatched into stats for reporting
    unmatched.extend(cached_unmatched)
    stats.unmatched += len(cached_unmatched)

    match_time = time.time() - t1
    logger.info(f"Matching completed in {match_time:.1f}s")
    print()
    print_scan_summary(all_photos, stats)
    print_match_summary(matches, unmatched, stats, cached_unmatched_count=len(cached_unmatched))
    print_match_preview(matches, max_show=args.preview_count)

    # Save report
    if args.report:
        save_report(matches, unmatched, stats, args.report)
        print(f"  Report saved to: {args.report}")
        print()

    # ── Phase 3: Write GPS ──
    if not matches:
        print("  No matches found. Nothing to write.")
        sys.exit(0)

    if is_dry_run:
        print("  ─" * 25)
        print(f"  DRY RUN complete. {stats.matched} photos would be geo-tagged.")
        print("  Run with --apply to write GPS data.")
        if not args.report:
            print("  Use --report matches.csv to save detailed report.")
        print()
        sys.exit(0)

    # Live write
    print("  Phase 3: Writing GPS data...")
    print("  ─" * 25)
    t2 = time.time()

    success, fail = apply_matches(
        matches,
        write_mode=config["write_mode"],
        min_confidence=min_conf,
    )

    write_time = time.time() - t2
    total_time = time.time() - t0

    print()
    print("  WRITE RESULTS")
    print("  ─────────────")
    print(f"  Successful:  {success:>6d}")
    print(f"  Failed:      {fail:>6d}")
    print(f"  Write mode:  {config['write_mode']}")
    print()
    print(f"  Timing: scan={scan_time:.1f}s, match={match_time:.1f}s, write={write_time:.1f}s, total={total_time:.1f}s")
    print()

    if fail > 0:
        print(f"  ⚠  {fail} writes failed. Check log for details.")
        sys.exit(1)
    else:
        print(f"  ✓  All {success} photos geo-tagged successfully!")
        print("     Processed tag written — these files will be skipped on re-run.")
        sys.exit(0)


if __name__ == "__main__":
    main()

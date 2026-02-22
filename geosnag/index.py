"""
index.py — Scan index/cache for GeoSnag.

Persists scan results to a JSON file so repeat runs skip EXIF reads
for files that haven't changed (same mtime + size).

Also caches match results: targets confirmed as "no_match" are skipped
on subsequent runs if the set of GPS sources for that date hasn't changed.

Index structure:
  {
    "version": 5,
    "match_threshold_minutes": 120,
    "match_generation": 0,
    "entries": {
      "/abs/path/photo.nef": {
        "mtime": 1234567890.123,
        "size": 26400000,
        "datetime_original": "2017-09-23T23:11:37",
        "has_gps": false,
        "gps_latitude": null,
        "gps_longitude": null,
        "gps_altitude": null,
        "camera_make": "NIKON CORPORATION",
        "camera_model": "NIKON D610",
        "geosnag_processed": false,
        "match_status": null,
        "match_source_fp": null,
        "match_gen": 0
      }
    }
  }

Changes in v5: scan_error not stored — files with errors are not cached
at all, so they get a fresh scan attempt on the next run.
Match cache uses generation counter for O(1) invalidation.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from . import INDEX_FILENAME, PROJECT_NAME
from .scanner import PhotoMeta

logger = logging.getLogger(f"{PROJECT_NAME.lower()}.index")

INDEX_VERSION = 5


def _default_index_path(config_path: str) -> str:
    """Derive index file path from config file location."""
    config_dir = os.path.dirname(os.path.abspath(config_path))
    return os.path.join(config_dir, INDEX_FILENAME)


def _photo_to_entry(meta: PhotoMeta) -> dict:
    """Convert PhotoMeta to serializable index entry.

    Note: scan_error is intentionally omitted — transient errors are
    re-evaluated on the next scan and not needed for the matching pipeline.
    """
    mtime, size = _stat_file(meta.filepath)
    return {
        "mtime": mtime,
        "size": size,
        "datetime_original": meta.datetime_original.isoformat() if meta.datetime_original else None,
        "has_gps": meta.has_gps,
        "gps_latitude": meta.gps_latitude,
        "gps_longitude": meta.gps_longitude,
        "gps_altitude": meta.gps_altitude,
        "camera_make": meta.camera_make,
        "camera_model": meta.camera_model,
        "geosnag_processed": meta.geosnag_processed,
    }


def _entry_to_photo(filepath: str, entry: dict) -> PhotoMeta:
    """Convert index entry back to PhotoMeta."""
    dt_str = entry.get("datetime_original")
    dt = None
    if dt_str:
        try:
            dt = datetime.fromisoformat(dt_str)
        except (ValueError, TypeError):
            pass

    return PhotoMeta(
        filepath=filepath,
        filename=os.path.basename(filepath),
        extension=os.path.splitext(filepath)[1].lower(),
        datetime_original=dt,
        has_gps=entry.get("has_gps", False),
        gps_latitude=entry.get("gps_latitude"),
        gps_longitude=entry.get("gps_longitude"),
        gps_altitude=entry.get("gps_altitude"),
        camera_make=entry.get("camera_make"),
        camera_model=entry.get("camera_model"),
        geosnag_processed=entry.get("geosnag_processed", False),
        # scan_error not stored in index — transient, re-evaluated on rescan
    )


def _stat_file(filepath: str) -> tuple[Optional[float], Optional[int]]:
    """Return (mtime, size) from a single os.stat() call, or (None, None) on error."""
    try:
        st = os.stat(filepath)
        return st.st_mtime, st.st_size
    except OSError:
        return None, None


class ScanIndex:
    """
    Persistent scan index that caches EXIF scan results.

    Usage:
        idx = ScanIndex(index_path)
        idx.load()

        # Check before scanning
        cached = idx.lookup(filepath)
        if cached is not None:
            photo_meta = cached  # skip EXIF read
        else:
            photo_meta = scan_photo(filepath)
            idx.update(photo_meta)

        idx.save()
    """

    def __init__(self, index_path: str):
        self.index_path = index_path
        self.entries = {}  # type: dict[str, dict]
        self._dirty = False
        self._match_threshold_minutes = None  # cached max_time_delta config
        self._match_generation = 0  # bumped when match cache is invalidated

    def load(self) -> int:
        """Load index from disk. Returns number of cached entries."""
        if not os.path.exists(self.index_path):
            logger.info("No existing index found, starting fresh")
            return 0

        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if data.get("version") != INDEX_VERSION:
                logger.info(f"Index version mismatch (got {data.get('version')}, need {INDEX_VERSION}), rebuilding")
                self.entries = {}
                return 0

            self.entries = data.get("entries", {})
            self._match_threshold_minutes = data.get("match_threshold_minutes")
            self._match_generation = data.get("match_generation", 0)
            logger.info(f"Loaded index with {len(self.entries)} cached entries")
            return len(self.entries)

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Corrupt index file, starting fresh: {e}")
            self.entries = {}
            return 0

    def save(self) -> None:
        """Write index to disk."""
        if not self._dirty:
            logger.debug("Index unchanged, skipping save")
            return

        data = {
            "version": INDEX_VERSION,
            "match_threshold_minutes": self._match_threshold_minutes,
            "match_generation": self._match_generation,
            "entries": self.entries,
        }

        # Write to temp file first, then rename (atomic on POSIX)
        tmp_path = self.index_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))

            os.replace(tmp_path, self.index_path)
            logger.info(f"Index saved: {len(self.entries)} entries → {self.index_path}")
            self._dirty = False

        except OSError as e:
            logger.error(f"Failed to save index: {e}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError as cleanup_err:
                    logger.warning(f"Could not clean up temp index file {tmp_path}: {cleanup_err}")

    def lookup(self, filepath: str) -> Optional[PhotoMeta]:
        """
        Look up a file in the index. Returns PhotoMeta if cache is valid, None if miss.

        Cache hit requires: file exists in index AND mtime matches AND size matches.
        """
        entry = self.entries.get(filepath)
        if entry is None:
            return None

        current_mtime, current_size = _stat_file(filepath)
        if current_mtime is None:
            return None

        if entry.get("mtime") != current_mtime or entry.get("size") != current_size:
            return None

        return _entry_to_photo(filepath, entry)

    def update(self, meta: PhotoMeta) -> None:
        """Add or update a scan result in the index."""
        self.entries[meta.filepath] = _photo_to_entry(meta)
        self._dirty = True

    def prune(self, valid_paths: set) -> int:
        """Remove entries for files that no longer exist. Returns count removed."""
        stale = [p for p in self.entries if p not in valid_paths]
        for p in stale:
            del self.entries[p]
        if stale:
            self._dirty = True
            logger.info(f"Pruned {len(stale)} stale entries from index")
        return len(stale)

    def clear(self) -> None:
        """Clear all entries and match threshold."""
        self.entries = {}
        self._match_threshold_minutes = None
        self._match_generation = 0
        self._dirty = True

    # ── Match cache methods ──────────────────────────────────────────────

    def update_match_result(self, filepath: str, status: str, source_fingerprint: str = None) -> None:
        """Set match cache fields on an existing index entry.

        Args:
            filepath: Absolute path of the target photo.
            status: "matched" or "no_match".
            source_fingerprint: Hash of GPS sources available for this target's date.
        """
        if filepath not in self.entries:
            return
        self.entries[filepath]["match_status"] = status
        self.entries[filepath]["match_source_fp"] = source_fingerprint
        self.entries[filepath]["match_gen"] = self._match_generation
        self._dirty = True

    def get_match_result(self, filepath: str) -> tuple:
        """Retrieve cached match result for a photo.

        Returns:
            (match_status, source_fingerprint) or (None, None) if not cached
            or if the entry's generation is stale.
        """
        entry = self.entries.get(filepath)
        if entry is None:
            return None, None
        # Stale generation → treat as uncached (O(1) instead of O(n) wipe)
        if entry.get("match_gen", 0) != self._match_generation:
            return None, None
        return entry.get("match_status"), entry.get("match_source_fp")

    def validate_match_threshold(self, current_minutes: int) -> bool:
        """Check if cached match results are still valid for the current threshold.

        If stored max_time_delta_minutes differs from current config, all match
        caches are invalidated by bumping the generation counter (O(1) instead
        of iterating every entry).

        Returns True if threshold is unchanged, False if match caches were cleared.
        """
        if self._match_threshold_minutes == current_minutes:
            return True
        old = self._match_threshold_minutes or "not set"
        logger.info(f"Match threshold changed ({old} → {current_minutes}), clearing match cache")
        self._match_generation += 1  # stale entries detected on read via match_gen
        self._match_threshold_minutes = current_minutes
        self._dirty = True
        return False

    @property
    def size(self) -> int:
        return len(self.entries)

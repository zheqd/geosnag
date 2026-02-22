"""
parallel.py — Multithreaded photo scanning with index integration.

Combines the scan index (cache) with concurrent.futures ThreadPoolExecutor
to maximize scan throughput:
  1. Walk directories, collect file paths
  2. Check index for cache hits (skip EXIF read)
  3. Fan out cache misses to thread pool for parallel EXIF reads
  4. Merge results and update index

Thread safety:
  - scan_photo() is thread-safe (reads file, returns new object)
  - Index updates happen on main thread after pool completes
  - Progress counter uses threading.Lock for accurate reporting
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from . import PROJECT_NAME
from .index import ScanIndex
from .scanner import PHOTO_EXTS, PhotoMeta, collect_photo_paths, scan_photo

logger = logging.getLogger(f"{PROJECT_NAME.lower()}.parallel")


def _collect_file_paths(
    directories: list,
    extensions: set,
    recursive: bool = True,
    exclude_patterns: list = None,
) -> list:
    """Walk directories and collect all photo file paths (no EXIF reading).

    Delegates to scanner.collect_photo_paths — kept as a thin wrapper for
    backward compatibility with tests that import this name.
    """
    return collect_photo_paths(directories, extensions, recursive, exclude_patterns)


class ScanProgress:
    """Thread-safe progress tracker."""

    def __init__(self, total: int, report_every: int = 500):
        self._lock = threading.Lock()
        self._scanned = 0
        self._errors = 0
        self._cache_hits = 0
        self.total = total
        self.report_every = report_every

    def tick(self, error: bool = False) -> None:
        with self._lock:
            self._scanned += 1
            if error:
                self._errors += 1
            if self._scanned % self.report_every == 0:
                logger.info(
                    f"  Progress: {self._scanned}/{self.total} scanned "
                    f"({self._cache_hits} cached, {self._errors} errors)"
                )

    def tick_cached(self) -> None:
        with self._lock:
            self._scanned += 1
            self._cache_hits += 1
            if self._scanned % self.report_every == 0:
                logger.info(
                    f"  Progress: {self._scanned}/{self.total} scanned "
                    f"({self._cache_hits} cached, {self._errors} errors)"
                )

    @property
    def scanned(self) -> int:
        return self._scanned

    @property
    def errors(self) -> int:
        return self._errors

    @property
    def cache_hits(self) -> int:
        return self._cache_hits


def scan_with_index(
    directories: list,
    extensions: set = None,
    recursive: bool = True,
    exclude_patterns: list = None,
    index: Optional[ScanIndex] = None,
    workers: int = 4,
) -> list:
    """
    Scan directories for photos using index cache and thread pool.

    Args:
        directories: List of directory paths to scan
        extensions: File extensions to include (default: PHOTO_EXTS)
        recursive: Scan subdirectories
        exclude_patterns: Glob patterns to skip
        index: ScanIndex instance for caching (None = no cache)
        workers: Number of parallel scan threads

    Returns:
        List of PhotoMeta for all found photos
    """
    if extensions is None:
        extensions = PHOTO_EXTS

    t0 = time.time()

    # Step 1: Collect all file paths (fast — just filesystem walk)
    logger.info(f"Collecting file paths from {len(directories)} directories...")
    all_paths = _collect_file_paths(directories, extensions, recursive, exclude_patterns)
    logger.info(f"Found {len(all_paths)} photo files in {time.time() - t0:.1f}s")

    if not all_paths:
        return []

    # Step 2: Split into cache hits and misses
    results = []  # type: list[PhotoMeta]
    to_scan = []  # type: list[str]
    progress = ScanProgress(len(all_paths))

    if index is not None:
        for filepath in all_paths:
            cached = index.lookup(filepath)
            if cached is not None:
                results.append(cached)
                progress.tick_cached()
            else:
                to_scan.append(filepath)

        logger.info(f"Index: {progress.cache_hits} cache hits, {len(to_scan)} need scanning")
    else:
        to_scan = all_paths

    # Step 3: Parallel scan of cache misses
    if to_scan:
        actual_workers = min(workers, len(to_scan))
        logger.info(f"Scanning {len(to_scan)} files with {actual_workers} threads...")

        scanned_results = []  # type: list[PhotoMeta]

        with ThreadPoolExecutor(max_workers=actual_workers) as pool:
            futures = {pool.submit(scan_photo, fp): fp for fp in to_scan}

            for future in as_completed(futures):
                filepath = futures[future]
                try:
                    meta = future.result()
                    scanned_results.append(meta)
                    progress.tick(error=bool(meta.scan_error))

                    # Update index on main thread (futures resolve here)
                    if index is not None:
                        index.update(meta)

                except Exception as e:
                    logger.error(f"Thread scan failed for {filepath}: {e}")
                    meta = PhotoMeta(
                        filepath=filepath,
                        filename=os.path.basename(filepath),
                        extension=os.path.splitext(filepath)[1].lower(),
                        scan_error=str(e),
                    )
                    scanned_results.append(meta)
                    progress.tick(error=True)

        results.extend(scanned_results)

    # Step 4: Prune stale index entries
    if index is not None:
        valid_paths = set(all_paths)
        index.prune(valid_paths)

    scan_time = time.time() - t0
    logger.info(
        f"Scan complete: {len(results)} photos in {scan_time:.1f}s "
        f"({progress.cache_hits} cached, {progress.errors} errors)"
    )

    return results

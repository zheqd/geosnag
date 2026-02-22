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
  - tqdm progress bar is updated from the main thread (as_completed loop)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from tqdm import tqdm

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


def _is_tty() -> bool:
    """Check if stdout is a terminal (not piped/redirected)."""
    try:
        return sys.stdout.isatty()
    except AttributeError:
        return False


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
    use_bar = _is_tty()

    # Step 1: Collect all file paths (fast — just filesystem walk)
    logger.info(f"Collecting file paths from {len(directories)} directories...")
    all_paths = _collect_file_paths(directories, extensions, recursive, exclude_patterns)
    logger.info(f"Found {len(all_paths)} photo files in {time.time() - t0:.1f}s")

    if not all_paths:
        return []

    # Step 2: Split into cache hits and misses
    results = []  # type: list[PhotoMeta]
    to_scan = []  # type: list[str]
    cache_hits = 0
    error_count = 0

    if index is not None:
        bar_desc = "  Checking index"
        with tqdm(
            total=len(all_paths),
            desc=bar_desc,
            unit=" files",
            disable=not use_bar,
            bar_format="  {desc}: {percentage:3.0f}%|{bar:30}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
        ) as pbar:
            for filepath in all_paths:
                cached = index.lookup(filepath)
                if cached is not None:
                    results.append(cached)
                    cache_hits += 1
                else:
                    to_scan.append(filepath)
                pbar.update(1)

        logger.info(f"Index: {cache_hits} cache hits, {len(to_scan)} need scanning")
    else:
        to_scan = all_paths

    # Step 3: Parallel scan of cache misses
    if to_scan:
        actual_workers = min(workers, len(to_scan))
        logger.info(f"Scanning {len(to_scan)} files with {actual_workers} threads...")

        scanned_results = []  # type: list[PhotoMeta]

        with tqdm(
            total=len(to_scan),
            desc="  Scanning EXIF",
            unit=" files",
            disable=not use_bar,
            bar_format="  {desc}: {percentage:3.0f}%|{bar:30}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
        ) as pbar:
            with ThreadPoolExecutor(max_workers=actual_workers) as pool:
                futures = {pool.submit(scan_photo, fp): fp for fp in to_scan}

                for future in as_completed(futures):
                    filepath = futures[future]
                    try:
                        meta = future.result()
                        scanned_results.append(meta)
                        if meta.scan_error:
                            error_count += 1

                        # Update index on main thread (futures resolve here).
                        # Skip caching files with scan errors so they get
                        # rescanned on the next run (transient errors like
                        # NFS hiccups or locked files get a fresh attempt).
                        if index is not None and not meta.scan_error:
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
                        error_count += 1

                    pbar.update(1)

        results.extend(scanned_results)

    # Step 4: Prune stale index entries
    if index is not None:
        valid_paths = set(all_paths)
        index.prune(valid_paths)

    scan_time = time.time() - t0
    logger.info(f"Scan complete: {len(results)} photos in {scan_time:.1f}s ({cache_hits} cached, {error_count} errors)")

    return results

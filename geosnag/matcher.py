"""
matcher.py — Matching engine for GPS photo enrichment.

Takes a single list of photos, auto-splits them into:
  - GPS sources: photos that have GPS coordinates
  - GPS targets: photos that lack GPS coordinates (and haven't been processed yet)

Algorithm:
  1. Group GPS-source photos by calendar date (YYYY-MM-DD)
  2. For each GPS-target photo:
     a. Find GPS-source photos from the same date
     b. Select the one with the closest timestamp
     c. If within the configured threshold → match
  3. Compute confidence score: 100 * (1 - time_delta / threshold)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from . import PROJECT_NAME
from .scanner import PhotoMeta

logger = logging.getLogger(f"{PROJECT_NAME.lower()}.matcher")


@dataclass
class MatchResult:
    """A match between a target photo and a GPS-source photo."""

    target: PhotoMeta  # photo receiving GPS
    source: PhotoMeta  # photo providing GPS
    time_delta: timedelta
    confidence: float  # 0-100, higher = closer in time

    # Backwards-compatible aliases
    @property
    def camera(self) -> PhotoMeta:
        return self.target

    @property
    def mobile(self) -> PhotoMeta:
        return self.source

    @property
    def time_delta_minutes(self) -> float:
        return self.time_delta.total_seconds() / 60.0

    @property
    def time_delta_str(self) -> str:
        total_sec = int(self.time_delta.total_seconds())
        hours, remainder = divmod(abs(total_sec), 3600)
        minutes, seconds = divmod(remainder, 60)
        sign = "-" if total_sec < 0 else "+"
        if hours > 0:
            return f"{sign}{hours}h{minutes:02d}m{seconds:02d}s"
        elif minutes > 0:
            return f"{sign}{minutes}m{seconds:02d}s"
        else:
            return f"{sign}{seconds}s"


@dataclass
class MatchStats:
    """Statistics about the matching process."""

    total_photos: int = 0
    sources: int = 0  # photos with GPS (usable as sources)
    targets: int = 0  # photos without GPS + not yet processed
    already_processed: int = 0  # already stamped by GeoSnag
    without_datetime: int = 0  # no datetime, can't match
    source_dates: int = 0  # unique dates with GPS sources
    matched: int = 0
    unmatched: int = 0
    avg_confidence: float = 0.0
    avg_time_delta_min: float = 0.0

    # Backwards-compatible aliases
    @property
    def camera_eligible(self) -> int:
        return self.targets

    @property
    def camera_with_gps(self) -> int:
        return self.sources

    @property
    def camera_without_datetime(self) -> int:
        return self.without_datetime

    @property
    def mobile_with_gps(self) -> int:
        return self.sources

    @property
    def mobile_dates(self) -> int:
        return self.source_dates


def match_photos(
    photos: list[PhotoMeta] = None,
    *,
    sources: list[PhotoMeta] = None,
    targets: list[PhotoMeta] = None,
    max_time_delta: timedelta = timedelta(hours=2),
) -> tuple[list[MatchResult], list[PhotoMeta], MatchStats]:
    """
    Match GPS-target photos to GPS-source photos by same-day closest timestamp.

    Can be called two ways:
      1. match_photos(all_photos)  — auto-splits by has_gps
      2. match_photos(sources=..., targets=...) — explicit lists

    Args:
        photos: Single list of all photos (auto-split by has_gps)
        sources: Explicit list of GPS-source photos
        targets: Explicit list of GPS-target photos
        max_time_delta: Maximum allowed time difference for matching

    Returns:
        Tuple of (matches, unmatched_target_photos, statistics)
    """
    stats = MatchStats()

    # Auto-split or use explicit lists
    if photos is not None:
        stats.total_photos = len(photos)
        if sources is None:
            sources = []
        if targets is None:
            targets = []

        for p in photos:
            if p.has_gps and p.datetime_original:
                sources.append(p)
            elif p.geosnag_processed:
                stats.already_processed += 1
            elif not p.has_gps:
                targets.append(p)
            # Photos with GPS but no datetime are just counted
    else:
        if sources is None:
            sources = []
        if targets is None:
            targets = []
        stats.total_photos = len(sources) + len(targets)

    stats.sources = len(sources)

    # Step 1: Index GPS-source photos by date
    source_by_date: dict[str, list[PhotoMeta]] = defaultdict(list)
    for sp in sources:
        if sp.has_gps and sp.datetime_original:
            date_key = sp.date_key
            if date_key:
                source_by_date[date_key].append(sp)

    stats.source_dates = len(source_by_date)

    # Sort source photos within each date by timestamp
    for date_key in source_by_date:
        source_by_date[date_key].sort(key=lambda p: p.datetime_original)

    logger.info(f"GPS source index: {stats.sources} photos across {stats.source_dates} dates")

    # Step 2: Match each target photo
    matches: list[MatchResult] = []
    unmatched: list[PhotoMeta] = []

    for tp in targets:
        # Skip photos without datetime
        if not tp.datetime_original:
            stats.without_datetime += 1
            unmatched.append(tp)
            continue

        stats.targets += 1
        date_key = tp.date_key

        # Find source photos from the same date
        if date_key not in source_by_date:
            stats.unmatched += 1
            unmatched.append(tp)
            continue

        # Find closest source photo by timestamp
        best_match: Optional[PhotoMeta] = None
        best_delta: Optional[timedelta] = None

        for sp in source_by_date[date_key]:
            delta = abs(tp.datetime_original - sp.datetime_original)
            if delta <= max_time_delta:
                if best_delta is None or delta < best_delta:
                    best_match = sp
                    best_delta = delta

        if best_match and best_delta is not None:
            max_seconds = max_time_delta.total_seconds()
            confidence = 100.0 if max_seconds == 0 else 100.0 * (1.0 - best_delta.total_seconds() / max_seconds)
            confidence = max(0.0, min(100.0, confidence))

            # Signed delta: positive = target after source, negative = target before
            signed_delta = tp.datetime_original - best_match.datetime_original

            match = MatchResult(
                target=tp,
                source=best_match,
                time_delta=signed_delta,
                confidence=confidence,
            )
            matches.append(match)
            stats.matched += 1
        else:
            stats.unmatched += 1
            unmatched.append(tp)

    # Compute averages
    if matches:
        stats.avg_confidence = sum(m.confidence for m in matches) / len(matches)
        stats.avg_time_delta_min = sum(abs(m.time_delta.total_seconds()) / 60 for m in matches) / len(matches)

    logger.info(
        f"Matching complete: {stats.matched} matched, {stats.unmatched} unmatched "
        f"(avg confidence: {stats.avg_confidence:.1f}%, avg delta: {stats.avg_time_delta_min:.1f}min)"
    )

    return matches, unmatched, stats

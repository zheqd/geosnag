"""
scanner.py — EXIF metadata scanner for photo files.

Scans all supported photo formats, extracts DateTimeOriginal, GPS coordinates,
and the processed marker tag.

Auto-classifies photos as GPS sources (has GPS) or GPS targets (no GPS).
No camera/mobile distinction needed — directories can be mixed.

Supported formats:
  JPG, JPEG, ARW, NEF, CR2, CR3, DNG, ORF, RAF, RW2, HEIC, HEIF, PNG
"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import exifread

from . import MARKER_PREFIX, PROJECT_NAME, PROJECT_TAG

logger = logging.getLogger(f"{PROJECT_NAME.lower()}.scanner")

# Lazy PIL/pillow-heif imports — only loaded on first HEIC file encounter.
# This shaves ~200 ms off startup when scanning non-HEIC libraries.
_Image = None
_ExifBase = None
_heif_registered = False


def _ensure_heif():
    """Lazy-import PIL + pillow-heif on first use."""
    global _Image, _ExifBase, _heif_registered
    if _heif_registered:
        return
    from PIL import Image as _PILImage
    from PIL.ExifTags import Base as _PILExifBase
    from pillow_heif import register_heif_opener

    register_heif_opener()
    _Image = _PILImage
    _ExifBase = _PILExifBase
    _heif_registered = True


# Directories always excluded from recursive walks (Synology system dirs, etc.)
EXCLUDED_DIRS = {"@eaDir", "#recycle", ".git", "__pycache__", ".geosnag"}

# All supported photo extensions (unified — no camera/mobile split)
PHOTO_EXTS = {
    ".jpg",
    ".jpeg",
    ".arw",
    ".nef",
    ".cr2",
    ".cr3",
    ".dng",
    ".orf",
    ".raf",
    ".rw2",  # RAW
    ".heic",
    ".heif",
    ".png",
}

GEOSNAG_TAG = PROJECT_TAG
GEOSNAG_MARKER_PREFIX = MARKER_PREFIX


@dataclass
class PhotoMeta:
    """Metadata extracted from a photo file."""

    filepath: str
    filename: str
    extension: str
    datetime_original: Optional[datetime] = None
    has_gps: bool = False
    gps_latitude: Optional[float] = None
    gps_longitude: Optional[float] = None
    gps_altitude: Optional[float] = None
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    geosnag_processed: bool = False  # True if already processed by GeoSnag
    scan_error: Optional[str] = None
    format_mismatch: Optional[str] = None  # Real format if ext doesn't match content (e.g. "JPEG")

    @property
    def date_key(self) -> Optional[str]:
        """Calendar date string for matching (YYYY-MM-DD)."""
        if self.datetime_original:
            return self.datetime_original.strftime("%Y-%m-%d")
        return None


def _gps_dms_to_decimal(dms_value, ref) -> Optional[float]:
    """Convert EXIF GPS DMS (degrees/minutes/seconds) to decimal degrees."""
    try:
        values = dms_value.values
        d = float(values[0].num) / float(values[0].den)
        m = float(values[1].num) / float(values[1].den)
        s = float(values[2].num) / float(values[2].den)
        decimal = d + m / 60.0 + s / 3600.0
        if ref in ("S", "W"):
            decimal = -decimal
        return decimal
    except (AttributeError, IndexError, ZeroDivisionError, TypeError) as e:
        logger.debug(f"GPS conversion error: {e}")
        return None


def _parse_exif_datetime(dt_string: str) -> Optional[datetime]:
    """Parse EXIF datetime string to Python datetime."""
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(str(dt_string).strip(), fmt)
        except ValueError:
            continue
    return None


def _check_geosnag_tag_exifread(tags: dict) -> bool:
    """Check if GeoSnag processed tag is present via exifread tags."""
    # Exif.Image.Software is read by exifread as "Image Software"
    val = tags.get("Image Software")
    if val:
        return str(val).startswith(GEOSNAG_MARKER_PREFIX)
    return False


def _check_geosnag_tag_pyexiv2(filepath: str) -> bool:
    """Check if GeoSnag processed tag is present via pyexiv2 (more reliable)."""
    try:
        import pyexiv2

        img = pyexiv2.Image(filepath)
        try:
            exif = img.read_exif()
        finally:
            img.close()
        val = exif.get(GEOSNAG_TAG, "")
        return val.startswith(GEOSNAG_MARKER_PREFIX)
    except Exception:
        pass
    return False


def _scan_with_exifread(filepath: str) -> dict:
    """Read EXIF using exifread library (works for JPG, NEF, ARW, CR2, DNG, etc.)."""
    result = {
        "datetime_original": None,
        "has_gps": False,
        "gps_latitude": None,
        "gps_longitude": None,
        "gps_altitude": None,
        "camera_make": None,
        "camera_model": None,
        "geosnag_processed": False,
        "scan_error": None,
    }

    with open(filepath, "rb") as f:
        tags = exifread.process_file(f, details=False)

    if not tags:
        return result

    # Camera info
    if "Image Make" in tags:
        result["camera_make"] = str(tags["Image Make"]).strip()
    if "Image Model" in tags:
        result["camera_model"] = str(tags["Image Model"]).strip()

    # DateTime — try multiple tags in priority order
    for dt_tag in [
        "EXIF DateTimeOriginal",
        "EXIF DateTimeDigitized",
        "Image DateTime",
        "Image DateTimeOriginal",
    ]:
        if dt_tag in tags:
            result["datetime_original"] = _parse_exif_datetime(str(tags[dt_tag]))
            if result["datetime_original"]:
                break

    # GPS
    if "GPS GPSLatitude" in tags and "GPS GPSLongitude" in tags:
        lat_ref = str(tags.get("GPS GPSLatitudeRef", "N"))
        lon_ref = str(tags.get("GPS GPSLongitudeRef", "E"))
        lat = _gps_dms_to_decimal(tags["GPS GPSLatitude"], lat_ref)
        lon = _gps_dms_to_decimal(tags["GPS GPSLongitude"], lon_ref)
        if lat is not None and lon is not None:
            # Sanity check: valid coordinate range
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                result["has_gps"] = True
                result["gps_latitude"] = lat
                result["gps_longitude"] = lon

        # Altitude
        if "GPS GPSAltitude" in tags:
            try:
                alt_val = tags["GPS GPSAltitude"].values[0]
                alt = float(alt_val.num) / float(alt_val.den)
                alt_ref = str(tags.get("GPS GPSAltitudeRef", "0"))
                if alt_ref == "1":
                    alt = -alt
                result["gps_altitude"] = alt
            except (AttributeError, IndexError, ZeroDivisionError):
                pass

    # GeoSnag processed check — try exifread first, fallback to pyexiv2
    result["geosnag_processed"] = _check_geosnag_tag_exifread(tags)
    if not result["geosnag_processed"]:
        result["geosnag_processed"] = _check_geosnag_tag_pyexiv2(filepath)

    return result


def _scan_heic(filepath: str) -> dict:
    """Read EXIF from HEIC/HEIF using pillow-heif."""
    result = {
        "datetime_original": None,
        "has_gps": False,
        "gps_latitude": None,
        "gps_longitude": None,
        "gps_altitude": None,
        "camera_make": None,
        "camera_model": None,
        "geosnag_processed": False,
        "scan_error": None,
    }

    _ensure_heif()

    try:
        with _Image.open(filepath) as img:
            exif = img.getexif()

            if not exif:
                return result

            # Camera info
            if _ExifBase.Make in exif:
                result["camera_make"] = str(exif[_ExifBase.Make]).strip()
            if _ExifBase.Model in exif:
                result["camera_model"] = str(exif[_ExifBase.Model]).strip()

            # DateTime
            for tag_id in [_ExifBase.DateTimeOriginal, _ExifBase.DateTimeDigitized, _ExifBase.DateTime]:
                if tag_id in exif:
                    result["datetime_original"] = _parse_exif_datetime(str(exif[tag_id]))
                    if result["datetime_original"]:
                        break
                # Also check IFD sub-dictionaries
                ifd = exif.get_ifd(0x8769)  # Exif IFD
                if ifd and tag_id in ifd:
                    result["datetime_original"] = _parse_exif_datetime(str(ifd[tag_id]))
                    if result["datetime_original"]:
                        break

            # GPS IFD
            gps_ifd = exif.get_ifd(0x8825)  # GPS IFD
            if gps_ifd:
                lat_data = gps_ifd.get(2)  # GPSLatitude
                lat_ref = gps_ifd.get(1, "N")  # GPSLatitudeRef
                lon_data = gps_ifd.get(4)  # GPSLongitude
                lon_ref = gps_ifd.get(3, "E")  # GPSLongitudeRef

                if lat_data and lon_data:
                    try:
                        lat = float(lat_data[0]) + float(lat_data[1]) / 60 + float(lat_data[2]) / 3600
                        lon = float(lon_data[0]) + float(lon_data[1]) / 60 + float(lon_data[2]) / 3600
                        if lat_ref == "S":
                            lat = -lat
                        if lon_ref == "W":
                            lon = -lon
                        if -90 <= lat <= 90 and -180 <= lon <= 180:
                            result["has_gps"] = True
                            result["gps_latitude"] = lat
                            result["gps_longitude"] = lon
                    except (TypeError, ValueError, IndexError):
                        pass

                # Altitude
                alt_data = gps_ifd.get(6)  # GPSAltitude
                if alt_data is not None:
                    try:
                        alt = float(alt_data)
                        alt_ref = gps_ifd.get(5, 0)
                        if alt_ref == 1:
                            alt = -alt
                        result["gps_altitude"] = alt
                    except (TypeError, ValueError) as e:
                        logger.debug(f"HEIC altitude conversion error for {filepath}: {e}")

            # Software tag (IFD0, 0x0131) for GeoSnag processed marker
            software = exif.get(0x0131, "")  # 0x0131 = Software
            if str(software).startswith(GEOSNAG_MARKER_PREFIX):
                result["geosnag_processed"] = True

    except Exception as e:
        logger.debug(f"HEIC scan error for {filepath}: {e}")
        result["scan_error"] = str(e)

    return result


def _detect_format_mismatch(filepath: str, ext: str) -> Optional[str]:
    """Check if file content doesn't match its extension by sniffing magic bytes.

    Returns the real format string (e.g. "JPEG") if mismatched, None if OK.
    Common with Google Takeout exports that save JPEGs as .heic.
    """
    _EXT_FORMAT = {
        ".jpg": "JPEG", ".jpeg": "JPEG",
        ".png": "PNG",
        ".heic": "HEIC", ".heif": "HEIC",
    }
    expected = _EXT_FORMAT.get(ext)
    if expected is None:
        return None  # RAW formats — no magic-byte check

    try:
        with open(filepath, "rb") as f:
            header = f.read(12)
    except OSError:
        return None

    if header[:3] == b"\xff\xd8\xff":
        real = "JPEG"
    elif header[:4] == b"\x89PNG":
        real = "PNG"
    elif header[4:8] == b"ftyp":
        real = "HEIC"
    else:
        return None

    return real if real != expected else None


def scan_photo(filepath: str) -> PhotoMeta:
    """Scan a single photo file and return its metadata."""
    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1].lower()

    meta = PhotoMeta(
        filepath=filepath,
        filename=filename,
        extension=ext,
    )

    # Detect extension/content mismatch (e.g. Google Takeout JPEG saved as .heic)
    meta.format_mismatch = _detect_format_mismatch(filepath, ext)

    try:
        if ext in {".heic", ".heif"}:
            data = _scan_heic(filepath)
        else:
            data = _scan_with_exifread(filepath)

        meta.datetime_original = data.get("datetime_original")
        meta.has_gps = data.get("has_gps", False)
        meta.gps_latitude = data.get("gps_latitude")
        meta.gps_longitude = data.get("gps_longitude")
        meta.gps_altitude = data.get("gps_altitude")
        meta.camera_make = data.get("camera_make")
        meta.camera_model = data.get("camera_model")
        meta.geosnag_processed = data.get("geosnag_processed", False)
        if data.get("scan_error"):
            meta.scan_error = data["scan_error"]

    except Exception as e:
        meta.scan_error = str(e)
        logger.warning(f"Failed to scan {filepath}: {e}")

    return meta


def collect_photo_paths(
    directories: list,
    extensions: set = None,
    recursive: bool = True,
    exclude_patterns: list = None,
) -> list[str]:
    """Walk directories and collect all matching photo file paths.

    This is the single implementation of directory traversal used by both
    scan_directory() and the parallel scanner.

    Args:
        directories: List of directory paths to scan
        extensions: File extensions to include (default: PHOTO_EXTS)
        recursive: Scan subdirectories
        exclude_patterns: Glob patterns for files to skip

    Returns:
        List of file paths (sorted within each directory)
    """
    if extensions is None:
        extensions = PHOTO_EXTS

    paths = []
    dirs_walked = 0
    for directory in directories:
        if not os.path.isdir(directory):
            logger.warning(f"Directory not found, skipping: {directory}")
            continue

        walker = os.walk(directory) if recursive else [(directory, [], os.listdir(directory))]
        for root, dirs, files in walker:
            # Prune excluded directories in-place to prevent os.walk from descending
            if recursive:
                dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            else:
                files = [f for f in files if os.path.isfile(os.path.join(root, f))]

            dirs_walked += 1
            if dirs_walked % 200 == 0:
                logger.info(f"  Walking directories... {dirs_walked} visited, {len(paths)} photos found")

            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                if ext not in extensions:
                    continue

                filepath = os.path.join(root, fname)

                if exclude_patterns:
                    rel_path = os.path.relpath(filepath, directory)
                    excluded = False
                    for pattern in exclude_patterns:
                        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(filepath, pattern):
                            excluded = True
                            break
                    if excluded:
                        continue

                paths.append(filepath)

    if dirs_walked >= 200:
        logger.info(f"  Walk complete: {dirs_walked} directories, {len(paths)} photos")

    return paths


def scan_directory(
    directory: str,
    extensions: set = None,
    recursive: bool = True,
    exclude_patterns: list = None,
) -> list[PhotoMeta]:
    """
    Scan a directory for photos and return metadata list.

    Args:
        directory: Path to scan
        extensions: Set of extensions to include (default: PHOTO_EXTS)
        recursive: Scan subdirectories
        exclude_patterns: Glob patterns for files to skip

    Returns:
        List of PhotoMeta for all found photos
    """
    paths = collect_photo_paths([directory], extensions, recursive, exclude_patterns)

    results = []
    error_count = 0

    for i, filepath in enumerate(paths, 1):
        if i % 100 == 0:
            logger.info(f"  Scanned {i} files...")

        meta = scan_photo(filepath)
        if meta.scan_error:
            error_count += 1
        results.append(meta)

    logger.info(f"Scanned {len(paths)} files in {directory} ({error_count} errors)")
    return results

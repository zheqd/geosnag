"""
writer.py — GPS EXIF writer for photo files.

Writes GPS coordinates into photo EXIF data using pyexiv2 (primary) or
ExifTool (fallback). Also supports writing XMP sidecar files.
After writing GPS, stamps a processed marker tag so the file is skipped on re-runs.

Backend selection
-----------------
1. pyexiv2  — preferred. Requires libexiv2 ≥ glibc 2.32. Works on modern
              Linux and macOS. May not be available on older Synology DSM.
2. exiftool — fallback. Probed at: exiftool, /opt/bin/exiftool, /usr/bin/exiftool.
              Install on Synology via Entware: opkg install perl-image-exiftool

Safety guarantee
----------------
Both backends write atomically. pyexiv2/libexiv2 prepares the full EXIF
structure in memory before touching the file. ExifTool uses a temp-file
rename strategy. A failed write leaves the original intact in both cases.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from . import MARKER_PREFIX, PROJECT_NAME, PROJECT_TAG, __version__
from .scanner import _EXT_FORMAT

logger = logging.getLogger(f"{PROJECT_NAME.lower()}.writer")

# Re-export for backward compat
GEOSNAG_TAG = PROJECT_TAG
GEOSNAG_MARKER_PREFIX = MARKER_PREFIX


# ---------------------------------------------------------------------------
# Backend detection (cached at module level)
# ---------------------------------------------------------------------------


def _has_pyexiv2() -> bool:
    """Return True if pyexiv2 can be imported successfully (not just installed)."""
    if importlib.util.find_spec("pyexiv2") is None:
        return False
    try:
        import pyexiv2  # noqa: F401  — test import only

        return True
    except OSError:
        # libexiv2.so failed to load (e.g. glibc version mismatch on Synology)
        return False
    except Exception:
        return False


def _probe_cmd(cmd: List[str]) -> bool:
    """Return True if running cmd + ['-ver'] exits 0."""
    try:
        return subprocess.run(cmd + ["-ver"], capture_output=True, timeout=5).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _find_exiftool() -> Optional[List[str]]:
    """Return the exiftool invocation command, or None if not installed."""
    for candidate in ("exiftool", "/opt/bin/exiftool", "/usr/bin/exiftool"):
        if _probe_cmd([candidate]):
            return [candidate]
    return None


# Evaluate once at import time so every write call doesn't re-probe.
_PYEXIV2_OK: bool = _has_pyexiv2()
# Always probe ExifTool — even when pyexiv2 is primary, ExifTool is needed
# as fallback for format-mismatched files (e.g. Google Takeout JPEGs with .heic
# extension) where pyexiv2/libexiv2 refuses to write but ExifTool -m succeeds.
_EXIFTOOL: Optional[List[str]] = _find_exiftool()

if _PYEXIV2_OK:
    logger.debug("GPS writer: using pyexiv2" + (" (exiftool available as fallback)" if _EXIFTOOL else ""))
elif _EXIFTOOL:
    logger.debug(f"GPS writer: pyexiv2 unavailable, using exiftool ({' '.join(_EXIFTOOL)})")
else:
    logger.warning(
        "GPS writer: neither pyexiv2 nor exiftool is available. "
        "GPS writes will fail. Install exiftool via: opkg install perl-image-exiftool"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class WriteResult:
    """Result of a GPS write operation."""

    filepath: str
    success: bool
    method: str  # "exif" or "xmp_sidecar"
    error: Optional[str] = None


def _decimal_to_dms_rational(decimal: float) -> str:
    """Convert decimal degrees to EXIF DMS rational string (for pyexiv2)."""
    d = int(abs(decimal))
    m_full = (abs(decimal) - d) * 60
    m = int(m_full)
    s = round((m_full - m) * 60 * 10000)
    return f"{d}/1 {m}/1 {s}/10000"


def _make_stamp() -> str:
    """Generate the processed marker string."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{MARKER_PREFIX}v{__version__}:{now}"


def _build_gps_exif_dict(
    latitude: float,
    longitude: float,
    altitude: Optional[float],
    stamp: Optional[str],
) -> dict:
    """Build the GPS EXIF tag dict shared by all pyexiv2 write paths."""
    lat_ref = "N" if latitude >= 0 else "S"
    lon_ref = "E" if longitude >= 0 else "W"

    gps_data = {
        "Exif.GPSInfo.GPSVersionID": "2 3 0 0",
        "Exif.GPSInfo.GPSLatitudeRef": lat_ref,
        "Exif.GPSInfo.GPSLatitude": _decimal_to_dms_rational(latitude),
        "Exif.GPSInfo.GPSLongitudeRef": lon_ref,
        "Exif.GPSInfo.GPSLongitude": _decimal_to_dms_rational(longitude),
        "Exif.GPSInfo.GPSMapDatum": "WGS-84",
    }

    if altitude is not None:
        alt_ref = "0" if altitude >= 0 else "1"
        gps_data["Exif.GPSInfo.GPSAltitudeRef"] = alt_ref
        gps_data["Exif.GPSInfo.GPSAltitude"] = f"{int(abs(altitude) * 100)}/100"

    if stamp:
        gps_data[GEOSNAG_TAG] = stamp

    return gps_data


# ---------------------------------------------------------------------------
# pyexiv2 backend
# ---------------------------------------------------------------------------


def _write_gps_pyexiv2(
    filepath: str,
    latitude: float,
    longitude: float,
    altitude: Optional[float],
    stamp: Optional[str],
) -> None:
    """Write GPS (and optional stamp) via pyexiv2. Raises on failure."""
    import pyexiv2

    gps_data = _build_gps_exif_dict(latitude, longitude, altitude, stamp)

    img = pyexiv2.Image(filepath)
    try:
        img.modify_exif(gps_data)
    finally:
        img.close()


def _write_gps_pyexiv2_imagedata(
    filepath: str,
    latitude: float,
    longitude: float,
    altitude: Optional[float],
    stamp: Optional[str],
) -> None:
    """Write GPS via pyexiv2 ImageData (bytes-based, format-agnostic).

    Used for format-mismatched files (e.g. Google Takeout JPEG saved as .heic)
    where the normal pyexiv2.Image(filepath) path fails because libexiv2 checks
    the file extension. ImageData works on raw bytes, detecting format from
    magic bytes instead.

    Uses temp-file + atomic rename to match libexiv2's own safety strategy.
    Raises on failure.
    """
    import pyexiv2

    gps_data = _build_gps_exif_dict(latitude, longitude, altitude, stamp)

    # Guard against symlinks — refuse to operate on symlinked files
    if os.path.islink(filepath):
        raise RuntimeError(f"Refusing to ImageData-write through symlink: {filepath}")

    # Capture original permissions *before* any file operations (minimise TOCTOU)
    orig_mode = os.stat(filepath).st_mode

    # Read file into memory — ImageData detects format from content, not extension
    with open(filepath, "rb") as f:
        raw = f.read()

    with pyexiv2.ImageData(raw) as img:
        img.modify_exif(gps_data)
        modified = img.get_bytes()

    # Atomic write: mkstemp for unpredictable name, created with original permissions
    dir_name = os.path.dirname(filepath)
    fd, tmp_path = tempfile.mkstemp(suffix=".geosnag_tmp", dir=dir_name)
    fd_closed = False
    try:
        os.fchmod(fd, orig_mode)
        os.write(fd, modified)
        os.close(fd)
        fd_closed = True
        os.replace(tmp_path, filepath)  # atomic on POSIX (same dir)
    except BaseException:
        # Clean up temp file on any failure (including KeyboardInterrupt)
        if not fd_closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError as cleanup_err:
            logger.debug(f"Failed to clean up temp file {tmp_path}: {cleanup_err}")
        raise


def _stamp_pyexiv2(filepath: str, stamp: str) -> None:
    """Write stamp tag only via pyexiv2. Raises on failure."""
    import pyexiv2

    img = pyexiv2.Image(filepath)
    try:
        img.modify_exif({GEOSNAG_TAG: stamp})
    finally:
        img.close()


# ---------------------------------------------------------------------------
# ExifTool backend
# ---------------------------------------------------------------------------


def _write_gps_exiftool(
    filepath: str,
    latitude: float,
    longitude: float,
    altitude: Optional[float],
    stamp: Optional[str],
    exiftool: List[str],
) -> None:
    """Write GPS (and optional stamp) via exiftool subprocess. Raises on failure."""
    lat_ref = "N" if latitude >= 0 else "S"
    lon_ref = "E" if longitude >= 0 else "W"

    args = [
        *exiftool,
        "-overwrite_original",
    ]
    args += [
        f"-GPSLatitude={abs(latitude)}",
        f"-GPSLatitudeRef={lat_ref}",
        f"-GPSLongitude={abs(longitude)}",
        f"-GPSLongitudeRef={lon_ref}",
        "-GPSMapDatum=WGS-84",
    ]

    if altitude is not None:
        args += [
            f"-GPSAltitude={abs(altitude)}",
            f"-GPSAltitudeRef={'0' if altitude >= 0 else '1'}",
        ]

    if stamp:
        args += [f"-Software={stamp}"]

    args.append(filepath)

    result = subprocess.run(args, capture_output=True, timeout=30, encoding="utf-8")
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:500]
        raise RuntimeError(stderr or "exiftool returned non-zero exit code")


# Derive format → canonical extension from scanner's _EXT_FORMAT (single source of truth).
# Sort by (length, name) descending so shorter / alphabetically-earlier exts win last
# (e.g. ".jpg" over ".jpeg", ".heic" over ".heif").
_FORMAT_EXT = {fmt: ext for ext, fmt in sorted(_EXT_FORMAT.items(), key=lambda x: (len(x[0]), x[0]), reverse=True)}


def _write_gps_exiftool_rename(
    filepath: str,
    latitude: float,
    longitude: float,
    altitude: Optional[float],
    stamp: Optional[str],
    exiftool: List[str],
    real_format: str,
) -> None:
    """Write GPS via ExifTool by temporarily renaming to the correct extension.

    ExifTool's "Not a valid HEIC (looks more like a JPEG)" is a hard error
    that ``-m`` does not suppress. The workaround: copy the file to a temp
    path whose extension matches the real content, run ExifTool on the copy
    (which now passes validation), then atomic-replace the original.

    Raises on failure. Cleans up temp files even on crash.
    """
    correct_ext = _FORMAT_EXT.get(real_format)
    if not correct_ext:
        raise RuntimeError(f"Unknown format {real_format!r}, cannot determine extension")

    # Guard against symlinks — refuse to operate on symlinked files
    if os.path.islink(filepath):
        raise RuntimeError(f"Refusing to rename-write through symlink: {filepath}")

    dir_name = os.path.dirname(filepath)

    fd, tmp_path = tempfile.mkstemp(suffix=correct_ext, prefix=".geosnag_", dir=dir_name)
    try:
        os.close(fd)  # close immediately; copy2 overwrites content and permissions
        shutil.copy2(filepath, tmp_path, follow_symlinks=False)
        _write_gps_exiftool(tmp_path, latitude, longitude, altitude, stamp, exiftool)
        os.replace(tmp_path, filepath)  # atomic on POSIX (same dir)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError as cleanup_err:
            logger.debug(f"Failed to clean up temp file {tmp_path}: {cleanup_err}")
        raise


def _stamp_exiftool(filepath: str, stamp: str, exiftool: List[str]) -> None:
    """Write stamp tag only via exiftool. Raises on failure."""
    result = subprocess.run(
        [*exiftool, "-overwrite_original", f"-Software={stamp}", filepath],
        capture_output=True,
        timeout=30,
        encoding="utf-8",
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:500]
        raise RuntimeError(stderr or "exiftool stamp failed")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def stamp_processed(filepath: str) -> bool:
    """
    Write the GeoSnag processed marker to a file's Exif.Image.Software tag.

    Tries pyexiv2 first, falls back to exiftool. Returns True on success.
    """
    stamp = _make_stamp()
    try:
        if _PYEXIV2_OK:
            _stamp_pyexiv2(filepath, stamp)
        elif _EXIFTOOL:
            _stamp_exiftool(filepath, stamp, _EXIFTOOL)
        else:
            logger.warning(f"No write backend available — cannot stamp {filepath}")
            return False
        logger.debug(f"Stamped processed tag: {filepath}")
        return True
    except Exception as e:
        logger.warning(f"Could not stamp processed tag on {filepath}: {e}")
        return False


def write_gps_to_exif(
    filepath: str,
    latitude: float,
    longitude: float,
    altitude: Optional[float] = None,
    stamp_after_write: bool = True,
    format_mismatch: Optional[str] = None,
) -> WriteResult:
    """
    Write GPS coordinates into a photo's EXIF data.

    Uses pyexiv2 if available, otherwise falls back to exiftool.
    Works with JPG, NEF, ARW, CR2, DNG, and all other formats
    supported by exiv2 / ExifTool.

    Args:
        filepath: Path to the photo file
        latitude: GPS latitude in decimal degrees (positive=N, negative=S)
        longitude: GPS longitude in decimal degrees (positive=E, negative=W)
        altitude: Optional GPS altitude in meters (positive=above sea level)
        stamp_after_write: If True, write GeoSnag processed tag after GPS
        format_mismatch: If set (e.g. "JPEG"), the file's extension doesn't
            match its real content — use ExifTool -m (lenient) mode.
            Detected during scan, passed through to avoid re-reading the file.

    Returns:
        WriteResult with success/failure info
    """
    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        return WriteResult(
            filepath=filepath,
            success=False,
            method="exif",
            error=f"Invalid coordinates: ({latitude}, {longitude})",
        )

    if not _PYEXIV2_OK and not _EXIFTOOL:
        return WriteResult(
            filepath=filepath,
            success=False,
            method="exif",
            error=("No write backend available. Install exiftool via: opkg install perl-image-exiftool"),
        )

    stamp = _make_stamp() if stamp_after_write else None

    # Format-mismatched files (e.g. Google Takeout JPEG saved as .heic):
    # The normal pyexiv2.Image(path) and ExifTool both reject these because
    # the extension doesn't match the content. Fallback strategies:
    #   1. pyexiv2.ImageData — reads raw bytes, detects format from content
    #   2. ExifTool temp-rename — copy to temp with correct ext, write, replace
    # Note: ExifTool -m does NOT suppress this error ("Not a valid HEIC …"
    # is a hard error, not a minor warning), so we use the rename strategy.
    if format_mismatch:
        basename = os.path.basename(filepath)
        # Strategy 1: pyexiv2 ImageData (atomic temp-file + rename)
        if _PYEXIV2_OK:
            logger.info(f"Format mismatch: {basename} is actually {format_mismatch}, using pyexiv2 ImageData")
            try:
                _write_gps_pyexiv2_imagedata(filepath, latitude, longitude, altitude, stamp)
                logger.debug(f"GPS written (ImageData): {filepath}")
                return WriteResult(filepath=filepath, success=True, method="exif")
            except Exception as e:
                logger.debug(f"ImageData write failed for {filepath}: {e}, trying ExifTool rename")
                # Fall through to ExifTool rename

        # Strategy 2: ExifTool with temp-rename to correct extension
        if _EXIFTOOL:
            logger.info(
                f"Format mismatch: {basename} is actually {format_mismatch}, "
                f"using ExifTool with temp rename to {_FORMAT_EXT.get(format_mismatch, '?')}"
            )
            try:
                _write_gps_exiftool_rename(filepath, latitude, longitude, altitude, stamp, _EXIFTOOL, format_mismatch)
                logger.debug(f"GPS written (rename): {filepath}")
                return WriteResult(filepath=filepath, success=True, method="exif")
            except Exception as e:
                logger.error(f"GPS write failed for {filepath}: {e}")
                return WriteResult(filepath=filepath, success=False, method="exif", error=str(e))

        # Neither backend can handle it
        logger.warning(
            f"Format mismatch: {basename} is actually {format_mismatch}, but neither pyexiv2 nor ExifTool is available"
        )
        return WriteResult(
            filepath=filepath,
            success=False,
            method="exif",
            error=f"Format mismatch ({format_mismatch}) — no write backend available",
        )

    try:
        if _PYEXIV2_OK:
            _write_gps_pyexiv2(filepath, latitude, longitude, altitude, stamp)
        else:
            _write_gps_exiftool(filepath, latitude, longitude, altitude, stamp, _EXIFTOOL)

        logger.debug(f"GPS written: {filepath}")
        return WriteResult(filepath=filepath, success=True, method="exif")

    except Exception as e:
        logger.error(f"GPS write failed for {filepath}: {e}")
        return WriteResult(filepath=filepath, success=False, method="exif", error=str(e))


def write_gps_xmp_sidecar(
    filepath: str,
    latitude: float,
    longitude: float,
    altitude: Optional[float] = None,
    stamp_after_write: bool = True,
) -> WriteResult:
    """
    Write GPS coordinates to an XMP sidecar file (non-destructive).

    Creates a .xmp file next to the original photo with GPS metadata.
    Supported by Lightroom, Darktable, digiKam, and other photo managers.

    Args:
        filepath: Path to the original photo file
        latitude: GPS latitude in decimal degrees
        longitude: GPS longitude in decimal degrees
        altitude: Optional GPS altitude in meters
        stamp_after_write: If True, write GeoSnag tag to original file EXIF
    """
    try:
        lat_ref = "N" if latitude >= 0 else "S"
        lon_ref = "E" if longitude >= 0 else "W"

        lat_abs = abs(latitude)
        lon_abs = abs(longitude)
        lat_deg = int(lat_abs)
        lat_min = (lat_abs - lat_deg) * 60
        lon_deg = int(lon_abs)
        lon_min = (lon_abs - lon_deg) * 60

        lat_xmp = f"{lat_deg},{lat_min:.6f}{lat_ref}"
        lon_xmp = f"{lon_deg},{lon_min:.6f}{lon_ref}"

        alt_xml = ""
        if altitude is not None:
            alt_xml = f"\n      <exif:GPSAltitude>{abs(altitude):.2f}</exif:GPSAltitude>"
            alt_xml += f"\n      <exif:GPSAltitudeRef>{'0' if altitude >= 0 else '1'}</exif:GPSAltitudeRef>"

        xmp_content = f"""<?xpacket begin='\xef\xbb\xbf' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
  <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
    <rdf:Description rdf:about=''
      xmlns:exif='http://ns.adobe.com/exif/1.0/'
      xmlns:xmp='http://ns.adobe.com/xap/1.0/'>
      <exif:GPSVersionID>2.3.0.0</exif:GPSVersionID>
      <exif:GPSLatitude>{lat_xmp}</exif:GPSLatitude>
      <exif:GPSLongitude>{lon_xmp}</exif:GPSLongitude>
      <exif:GPSMapDatum>WGS-84</exif:GPSMapDatum>{alt_xml}
      <xmp:CreatorTool>{PROJECT_NAME} v{__version__}</xmp:CreatorTool>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>"""

        base, _ = os.path.splitext(filepath)
        xmp_path = base + ".xmp"

        if os.path.exists(xmp_path):
            logger.warning(f"XMP sidecar already exists, overwriting: {xmp_path}")

        with open(xmp_path, "w", encoding="utf-8") as f:
            f.write(xmp_content)

        logger.debug(f"XMP sidecar written: {xmp_path}")

        if stamp_after_write:
            stamp_processed(filepath)

        return WriteResult(filepath=filepath, success=True, method="xmp_sidecar")

    except Exception as e:
        logger.error(f"XMP sidecar write failed for {filepath}: {e}")
        return WriteResult(filepath=filepath, success=False, method="xmp_sidecar", error=str(e))

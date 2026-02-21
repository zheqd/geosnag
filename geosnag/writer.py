"""
writer.py — GPS EXIF writer for photo files.

Writes GPS coordinates into photo EXIF data using pyexiv2.
Also supports writing XMP sidecar files as a non-destructive alternative.
After writing GPS, stamps a processed marker tag so the file is skipped on re-runs.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import BACKUP_EXT, MARKER_PREFIX, PROJECT_NAME, PROJECT_TAG, __version__

logger = logging.getLogger(f"{PROJECT_NAME.lower()}.writer")

# Re-export for backward compat
GEOSNAG_TAG = PROJECT_TAG
GEOSNAG_MARKER_PREFIX = MARKER_PREFIX


@dataclass
class WriteResult:
    """Result of a GPS write operation."""

    filepath: str
    success: bool
    method: str  # "exif" or "xmp_sidecar"
    error: Optional[str] = None
    backup_path: Optional[str] = None


def _decimal_to_dms_rational(decimal: float) -> str:
    """Convert decimal degrees to EXIF DMS rational string (for pyexiv2)."""
    d = int(abs(decimal))
    m_full = (abs(decimal) - d) * 60
    m = int(m_full)
    s = round((m_full - m) * 60 * 10000)  # ten-thousandths of seconds for precision
    return f"{d}/1 {m}/1 {s}/10000"


def _make_stamp() -> str:
    """Generate the processed marker string."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return f"{MARKER_PREFIX}v{__version__}:{now}"


def stamp_processed(filepath: str) -> bool:
    """
    Write the GeoSnag processed marker to a file's EXIF UserComment.

    Returns True on success, False on failure.
    """
    try:
        import pyexiv2

        stamp = _make_stamp()
        img = pyexiv2.Image(filepath)
        img.modify_exif({GEOSNAG_TAG: stamp})
        img.close()
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
    create_backup: bool = True,
    stamp_after_write: bool = True,
) -> WriteResult:
    """
    Write GPS coordinates into a photo's EXIF data using pyexiv2.

    Works with JPG, NEF, ARW, CR2, DNG, and other formats supported by exiv2.

    Args:
        filepath: Path to the photo file
        latitude: GPS latitude in decimal degrees (positive=N, negative=S)
        longitude: GPS longitude in decimal degrees (positive=E, negative=W)
        altitude: Optional GPS altitude in meters (positive=above sea level)
        create_backup: If True, create .bak backup before modifying
        stamp_after_write: If True, write GeoSnag processed tag after GPS

    Returns:
        WriteResult with success/failure info
    """
    try:
        import pyexiv2
    except ImportError:
        return WriteResult(
            filepath=filepath,
            success=False,
            method="exif",
            error="pyexiv2 not installed",
        )

    backup_path = None

    try:
        # Create backup
        if create_backup:
            backup_path = filepath + BACKUP_EXT
            if not os.path.exists(backup_path):
                shutil.copy2(filepath, backup_path)
                logger.debug(f"Backup created: {backup_path}")

        # Prepare GPS EXIF data
        lat_ref = "N" if latitude >= 0 else "S"
        lon_ref = "E" if longitude >= 0 else "W"
        lat_dms = _decimal_to_dms_rational(latitude)
        lon_dms = _decimal_to_dms_rational(longitude)

        gps_data = {
            "Exif.GPSInfo.GPSVersionID": "2 3 0 0",
            "Exif.GPSInfo.GPSLatitudeRef": lat_ref,
            "Exif.GPSInfo.GPSLatitude": lat_dms,
            "Exif.GPSInfo.GPSLongitudeRef": lon_ref,
            "Exif.GPSInfo.GPSLongitude": lon_dms,
            "Exif.GPSInfo.GPSMapDatum": "WGS-84",
        }

        if altitude is not None:
            alt_ref = "0" if altitude >= 0 else "1"  # 0=above sea level, 1=below
            alt_rational = f"{int(abs(altitude) * 100)}/100"
            gps_data["Exif.GPSInfo.GPSAltitudeRef"] = alt_ref
            gps_data["Exif.GPSInfo.GPSAltitude"] = alt_rational

        # Also stamp the processed tag in the same write if requested
        if stamp_after_write:
            gps_data[GEOSNAG_TAG] = _make_stamp()

        # Write to file
        img = pyexiv2.Image(filepath)
        img.modify_exif(gps_data)
        img.close()

        logger.debug(f"GPS written: {filepath} → ({latitude:.6f}, {longitude:.6f})")

        return WriteResult(
            filepath=filepath,
            success=True,
            method="exif",
            backup_path=backup_path,
        )

    except Exception as e:
        logger.error(f"GPS write failed for {filepath}: {e}")

        # Restore from backup on failure
        if backup_path and os.path.exists(backup_path):
            try:
                shutil.copy2(backup_path, filepath)
                logger.info(f"Restored from backup: {filepath}")
            except Exception as restore_err:
                logger.error(f"Backup restore also failed: {restore_err}")

        return WriteResult(
            filepath=filepath,
            success=False,
            method="exif",
            error=str(e),
            backup_path=backup_path,
        )


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
        # Format for XMP: DD,MM.MMM{N|S}
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

        # Write sidecar — same base name, .xmp extension
        base, _ = os.path.splitext(filepath)
        xmp_path = base + ".xmp"

        # If XMP already exists, merge (don't overwrite) — for MVP, just warn
        if os.path.exists(xmp_path):
            logger.warning(f"XMP sidecar already exists, overwriting: {xmp_path}")

        with open(xmp_path, "w", encoding="utf-8") as f:
            f.write(xmp_content)

        logger.debug(f"XMP sidecar written: {xmp_path}")

        # Stamp the original file so it's skipped on re-runs
        if stamp_after_write:
            stamp_processed(filepath)

        return WriteResult(
            filepath=filepath,
            success=True,
            method="xmp_sidecar",
        )

    except Exception as e:
        logger.error(f"XMP sidecar write failed for {filepath}: {e}")
        return WriteResult(
            filepath=filepath,
            success=False,
            method="xmp_sidecar",
            error=str(e),
        )


def remove_backups(directory: str, recursive: bool = True) -> int:
    """Remove backup files. Returns count of removed files."""
    removed = 0
    walker = os.walk(directory) if recursive else [(directory, [], os.listdir(directory))]
    for root, dirs, files in walker:
        for fname in files:
            if fname.endswith(BACKUP_EXT):
                fpath = os.path.join(root, fname)
                try:
                    os.remove(fpath)
                    removed += 1
                except OSError as e:
                    logger.warning(f"Could not remove backup {fpath}: {e}")
    return removed

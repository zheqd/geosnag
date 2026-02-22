# GeoSnag - Photo Geo-Tagging Tool for Synology NAS
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("geosnag")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

# ── Centralized project identity ──
# Change ONLY these constants to rename the entire project.
# All modules import from here — no scattered name references.
PROJECT_NAME = "GeoSnag"
PROJECT_TAG = "Exif.Image.Software"
MARKER_PREFIX = "GeoSnag:"
INDEX_FILENAME = ".geosnag_index.json"

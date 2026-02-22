#!/usr/bin/env python3
"""Download and vendor ExifTool into geosnag/vendor/exiftool/.

Run once from the repo root before building the wheel:

    python scripts/download_exiftool.py

Requires Python 3.9+ and no extra dependencies (uses stdlib only).
"""

import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

EXIFTOOL_VERSION = "13.50"
DOWNLOAD_URL = (
    f"https://github.com/exiftool/exiftool/archive/refs/tags/{EXIFTOOL_VERSION}.tar.gz"
)

REPO_ROOT = Path(__file__).parent.parent
VENDOR_DIR = REPO_ROOT / "geosnag" / "vendor" / "exiftool"


def download(url: str, dest: Path) -> None:
    print(f"  Downloading {url} …")
    urllib.request.urlretrieve(url, dest)
    size_mb = dest.stat().st_size / 1_048_576
    print(f"  Saved {dest.name} ({size_mb:.1f} MB)")


def extract(archive: Path, target_dir: Path) -> None:
    """Extract exiftool script + lib/ from the tarball into target_dir."""
    prefix = f"exiftool-{EXIFTOOL_VERSION}/"

    # Clean existing vendor files, keep directory itself
    if target_dir.exists():
        for item in target_dir.iterdir():
            if item.name == ".gitkeep":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

    target_dir.mkdir(parents=True, exist_ok=True)

    print(f"  Extracting to {target_dir} …")
    with tarfile.open(archive, "r:gz") as tf:
        members_to_extract = []
        for member in tf.getmembers():
            name = member.name
            # Keep: top-level exiftool script and lib/ tree
            if name == f"{prefix}exiftool" or name.startswith(f"{prefix}lib/"):
                # Strip the version prefix from the path
                member.name = name[len(prefix):]
                members_to_extract.append(member)

        tf.extractall(path=target_dir, members=members_to_extract)

    script = target_dir / "exiftool"
    script.chmod(0o755)

    pm_count = len(list((target_dir / "lib").rglob("*.pm")))
    print(f"  Extracted exiftool script + {pm_count} .pm library files")


def verify(target_dir: Path) -> None:
    """Quick sanity-check that perl can run the vendored exiftool."""
    import subprocess

    script = target_dir / "exiftool"
    perl = shutil.which("perl")
    if not perl:
        print("  WARNING: perl not found on PATH — skipping verification")
        return

    result = subprocess.run([perl, str(script), "-ver"], capture_output=True, text=True, timeout=10)
    if result.returncode == 0:
        print(f"  Verified: ExifTool {result.stdout.strip()} runs OK with {perl}")
    else:
        print(f"  WARNING: verification failed:\n{result.stderr}")


def main() -> None:
    print(f"Vendoring ExifTool {EXIFTOOL_VERSION} into geosnag/vendor/exiftool/")

    archive = Path(f"/tmp/exiftool-{EXIFTOOL_VERSION}.tar.gz")
    if archive.exists():
        print(f"  Using cached {archive}")
    else:
        download(DOWNLOAD_URL, archive)

    extract(archive, VENDOR_DIR)
    verify(VENDOR_DIR)

    print("\nDone. Commit the vendor directory:")
    print("  git add geosnag/vendor/")
    print('  git commit -m "vendor: bundle ExifTool 13.50 for Synology compatibility"')


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)

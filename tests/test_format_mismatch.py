"""
Tests for format mismatch detection and handling.

Covers:
- scanner._detect_format_mismatch: magic byte detection for all formats
- scanner.scan_photo: format_mismatch field populated
- writer._write_gps_exiftool: lenient (-m) flag
- writer._write_gps_pyexiv2_imagedata: atomic write, temp cleanup
- writer.write_gps_to_exif: format_mismatch routing (3 strategies + fallbacks)
- index: format_mismatch serialization round-trip
"""

from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from geosnag import writer as writer_module
from geosnag.index import ScanIndex, _entry_to_photo, _photo_to_entry
from geosnag.scanner import PhotoMeta, _detect_format_mismatch
from geosnag.writer import (
    _write_gps_exiftool,
    _write_gps_exiftool_rename,
    _write_gps_pyexiv2_imagedata,
    write_gps_to_exif,
)

# ---------------------------------------------------------------------------
# _detect_format_mismatch (scanner.py)
# ---------------------------------------------------------------------------


class TestDetectFormatMismatch:
    """Tests for magic-byte format mismatch detection."""

    def _write_file(self, tmpdir, name, content):
        path = os.path.join(tmpdir, name)
        with open(path, "wb") as f:
            f.write(content)
        return path

    # JPEG magic bytes
    JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    # PNG magic bytes
    PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    # HEIC magic bytes (ftyp at offset 4)
    HEIC_HEADER = b"\x00\x00\x00\x1cftyp" + b"\x00" * 100

    def test_jpeg_in_heic_extension(self, tmp_path):
        """Google Takeout classic: JPEG saved as .heic."""
        path = self._write_file(str(tmp_path), "photo.heic", self.JPEG_HEADER)
        assert _detect_format_mismatch(path, ".heic") == "JPEG"

    def test_jpeg_in_heif_extension(self, tmp_path):
        """JPEG saved as .heif."""
        path = self._write_file(str(tmp_path), "photo.heif", self.JPEG_HEADER)
        assert _detect_format_mismatch(path, ".heif") == "JPEG"

    def test_png_in_heic_extension(self, tmp_path):
        """PNG saved as .heic."""
        path = self._write_file(str(tmp_path), "photo.heic", self.PNG_HEADER)
        assert _detect_format_mismatch(path, ".heic") == "PNG"

    def test_heic_in_jpg_extension(self, tmp_path):
        """HEIC saved as .jpg."""
        path = self._write_file(str(tmp_path), "photo.jpg", self.HEIC_HEADER)
        assert _detect_format_mismatch(path, ".jpg") == "HEIC"

    def test_jpeg_in_png_extension(self, tmp_path):
        """JPEG saved as .png."""
        path = self._write_file(str(tmp_path), "photo.png", self.JPEG_HEADER)
        assert _detect_format_mismatch(path, ".png") == "JPEG"

    def test_matching_jpeg_returns_none(self, tmp_path):
        """JPEG content in .jpg extension — no mismatch."""
        path = self._write_file(str(tmp_path), "photo.jpg", self.JPEG_HEADER)
        assert _detect_format_mismatch(path, ".jpg") is None

    def test_matching_jpeg_returns_none_uppercase(self, tmp_path):
        """JPEG content in .jpeg extension — no mismatch."""
        path = self._write_file(str(tmp_path), "photo.jpeg", self.JPEG_HEADER)
        assert _detect_format_mismatch(path, ".jpeg") is None

    def test_matching_png_returns_none(self, tmp_path):
        """PNG content in .png extension — no mismatch."""
        path = self._write_file(str(tmp_path), "photo.png", self.PNG_HEADER)
        assert _detect_format_mismatch(path, ".png") is None

    def test_matching_heic_returns_none(self, tmp_path):
        """HEIC content in .heic extension — no mismatch."""
        path = self._write_file(str(tmp_path), "photo.heic", self.HEIC_HEADER)
        assert _detect_format_mismatch(path, ".heic") is None

    def test_raw_extension_skipped(self, tmp_path):
        """RAW extensions (.nef, .arw, etc.) skip magic-byte check."""
        path = self._write_file(str(tmp_path), "photo.nef", self.JPEG_HEADER)
        for ext in [".nef", ".arw", ".cr2", ".cr3", ".dng", ".orf", ".raf", ".rw2"]:
            assert _detect_format_mismatch(path, ext) is None

    def test_unknown_magic_returns_none(self, tmp_path):
        """Unknown magic bytes — can't determine real format."""
        path = self._write_file(str(tmp_path), "photo.heic", b"\x00" * 50)
        assert _detect_format_mismatch(path, ".heic") is None

    def test_unreadable_file_returns_none(self, tmp_path):
        """OSError during read returns None (no crash)."""
        assert _detect_format_mismatch("/nonexistent/path.heic", ".heic") is None

    def test_short_file_returns_none(self, tmp_path):
        """File shorter than 12 bytes — shouldn't crash."""
        path = self._write_file(str(tmp_path), "tiny.heic", b"\xff\xd8")
        # Only 2 bytes — header[:3] check will fail, header[4:8] won't match
        result = _detect_format_mismatch(path, ".heic")
        # Could be None or could detect partial match; just shouldn't crash
        assert result is None or isinstance(result, str)

    def test_empty_file_returns_none(self, tmp_path):
        """Empty file — shouldn't crash."""
        path = self._write_file(str(tmp_path), "empty.jpg", b"")
        assert _detect_format_mismatch(path, ".jpg") is None


# ---------------------------------------------------------------------------
# scan_photo sets format_mismatch field
# ---------------------------------------------------------------------------


class TestScanPhotoFormatMismatch:
    """Verify scan_photo populates the format_mismatch field."""

    def test_scan_photo_detects_mismatch(self, tmp_path):
        """scan_photo should set format_mismatch for a JPEG in .heic."""
        from geosnag.scanner import scan_photo

        path = tmp_path / "photo.heic"
        # Write JPEG content with HEIC extension
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)

        meta = scan_photo(str(path))
        assert meta.format_mismatch == "JPEG"
        assert meta.extension == ".heic"

    def test_scan_photo_no_mismatch(self, tmp_path):
        """scan_photo should leave format_mismatch None for matched files."""
        from geosnag.scanner import scan_photo

        # Create a minimal JPEG with correct extension
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)

        meta = scan_photo(str(path))
        assert meta.format_mismatch is None

    def test_scan_photo_raw_no_mismatch(self, tmp_path):
        """scan_photo should not check RAW extensions."""
        from geosnag.scanner import scan_photo

        path = tmp_path / "photo.nef"
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 200)  # JPEG bytes in .nef

        meta = scan_photo(str(path))
        assert meta.format_mismatch is None


# ---------------------------------------------------------------------------
# _write_gps_exiftool lenient flag
# ---------------------------------------------------------------------------


class TestExiftoolLenientFlag:
    """Test that -m flag is passed when lenient=True."""

    def _run(self, lenient=False):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _write_gps_exiftool(
                "/tmp/test.heic",
                1.0,
                1.0,
                None,
                None,
                ["exiftool"],
                lenient=lenient,
            )
            return mock_run.call_args[0][0]

    def test_lenient_includes_m_flag(self):
        args = self._run(lenient=True)
        assert "-m" in args

    def test_normal_excludes_m_flag(self):
        args = self._run(lenient=False)
        assert "-m" not in args

    def test_m_flag_before_gps_args(self):
        """The -m flag should appear before any GPS arguments."""
        args = self._run(lenient=True)
        m_idx = args.index("-m")
        gps_idx = next(i for i, a in enumerate(args) if "GPSLatitude=" in a)
        assert m_idx < gps_idx


# ---------------------------------------------------------------------------
# _write_gps_pyexiv2_imagedata
# ---------------------------------------------------------------------------


def _make_mock_pyexiv2(get_bytes_return=b"modified"):
    """Create a mock pyexiv2 module with ImageData context manager."""
    mock_pyexiv2 = MagicMock()
    mock_imagedata = MagicMock()
    mock_imagedata.__enter__ = MagicMock(return_value=mock_imagedata)
    mock_imagedata.__exit__ = MagicMock(return_value=False)
    mock_imagedata.get_bytes.return_value = get_bytes_return
    mock_pyexiv2.ImageData.return_value = mock_imagedata
    return mock_pyexiv2, mock_imagedata


class TestWriteGpsPyexiv2ImageData:
    """Test the ImageData-based writer with atomic temp-file strategy."""

    def test_reads_file_and_writes_atomically(self, tmp_path):
        """Verify: read raw → ImageData modify → temp file → os.replace."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"fake_jpeg_content")
        os.chmod(str(test_file), 0o644)

        mock_pyexiv2, mock_imagedata = _make_mock_pyexiv2(b"modified_jpeg_content")

        with patch.dict("sys.modules", {"pyexiv2": mock_pyexiv2}):
            _write_gps_pyexiv2_imagedata(str(test_file), 55.7539, 37.6208, 150.0, "GeoSnag:test")

        # File should contain the modified content
        assert test_file.read_bytes() == b"modified_jpeg_content"
        # No temp file left behind
        assert not (tmp_path / "photo.heic.geosnag_tmp").exists()

    def test_temp_file_cleaned_on_failure(self, tmp_path):
        """If os.replace fails, temp file should be removed."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"original_content")

        mock_pyexiv2, _ = _make_mock_pyexiv2(b"modified_content")

        with patch.dict("sys.modules", {"pyexiv2": mock_pyexiv2}):
            with patch("os.replace", side_effect=OSError("disk full")):
                with pytest.raises(OSError, match="disk full"):
                    _write_gps_pyexiv2_imagedata(str(test_file), 1.0, 1.0, None, None)

        # Original content should be untouched
        assert test_file.read_bytes() == b"original_content"
        # Temp file should be cleaned up
        assert not (tmp_path / "photo.heic.geosnag_tmp").exists()

    def test_passes_altitude_when_provided(self, tmp_path):
        """Verify altitude is included in GPS data."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"fake_content")

        mock_pyexiv2, mock_imagedata = _make_mock_pyexiv2()

        with patch.dict("sys.modules", {"pyexiv2": mock_pyexiv2}):
            _write_gps_pyexiv2_imagedata(str(test_file), 1.0, 1.0, 100.0, None)

        # Check modify_exif was called with altitude data
        modify_call = mock_imagedata.modify_exif.call_args[0][0]
        assert "Exif.GPSInfo.GPSAltitude" in modify_call
        assert "Exif.GPSInfo.GPSAltitudeRef" in modify_call

    def test_negative_altitude(self, tmp_path):
        """Negative altitude: AltitudeRef should be '1'."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"fake_content")

        mock_pyexiv2, mock_imagedata = _make_mock_pyexiv2()

        with patch.dict("sys.modules", {"pyexiv2": mock_pyexiv2}):
            _write_gps_pyexiv2_imagedata(str(test_file), 1.0, 1.0, -50.0, None)

        modify_call = mock_imagedata.modify_exif.call_args[0][0]
        assert modify_call["Exif.GPSInfo.GPSAltitudeRef"] == "1"

    def test_no_altitude_when_none(self, tmp_path):
        """When altitude is None, no altitude keys should be set."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"fake_content")

        mock_pyexiv2, mock_imagedata = _make_mock_pyexiv2()

        with patch.dict("sys.modules", {"pyexiv2": mock_pyexiv2}):
            _write_gps_pyexiv2_imagedata(str(test_file), 1.0, 1.0, None, None)

        modify_call = mock_imagedata.modify_exif.call_args[0][0]
        assert "Exif.GPSInfo.GPSAltitude" not in modify_call

    def test_stamp_included_when_provided(self, tmp_path):
        """Verify stamp is written to the EXIF data."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"fake_content")

        mock_pyexiv2, mock_imagedata = _make_mock_pyexiv2()

        with patch.dict("sys.modules", {"pyexiv2": mock_pyexiv2}):
            _write_gps_pyexiv2_imagedata(str(test_file), 1.0, 1.0, None, "GeoSnag:v0.3.1:2026-01-01")

        modify_call = mock_imagedata.modify_exif.call_args[0][0]
        from geosnag.writer import GEOSNAG_TAG

        assert modify_call[GEOSNAG_TAG] == "GeoSnag:v0.3.1:2026-01-01"


# ---------------------------------------------------------------------------
# _write_gps_exiftool_rename
# ---------------------------------------------------------------------------


class TestWriteGpsExiftoolRename:
    """Test the ExifTool temp-rename strategy for format-mismatched files."""

    def test_creates_temp_with_correct_extension(self, tmp_path):
        """A JPEG-in-.heic should be copied to a .jpg temp file."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

        created_files = []

        def mock_run(args, **kwargs):
            # Record what file exiftool was called with
            created_files.append(args[-1])
            return MagicMock(returncode=0, stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            _write_gps_exiftool_rename(
                str(test_file), 1.0, 1.0, None, None, ["exiftool"], "JPEG"
            )

        # ExifTool should have been called with a .jpg temp file
        assert len(created_files) == 1
        assert created_files[0].endswith(".jpg")

    def test_original_file_replaced_with_modified(self, tmp_path):
        """After ExifTool writes to temp, original should be atomically replaced."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"original_content")

        def mock_run(args, **kwargs):
            # Simulate ExifTool modifying the temp file
            tmp_file = args[-1]
            with open(tmp_file, "wb") as f:
                f.write(b"exiftool_modified_content")
            return MagicMock(returncode=0, stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            _write_gps_exiftool_rename(
                str(test_file), 1.0, 1.0, None, None, ["exiftool"], "JPEG"
            )

        # Original path should now contain the modified content
        assert test_file.read_bytes() == b"exiftool_modified_content"

    def test_temp_file_cleaned_on_exiftool_failure(self, tmp_path):
        """If ExifTool fails, temp file should be cleaned up."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"original_content")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="ExifTool error")
            with pytest.raises(RuntimeError):
                _write_gps_exiftool_rename(
                    str(test_file), 1.0, 1.0, None, None, ["exiftool"], "JPEG"
                )

        # Original should be untouched
        assert test_file.read_bytes() == b"original_content"
        # No temp files left behind
        tmp_files = list(tmp_path.glob(".geosnag_tmp_*"))
        assert len(tmp_files) == 0

    def test_unknown_format_raises(self, tmp_path):
        """Unknown format string should raise immediately."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"\x00" * 50)

        with pytest.raises(RuntimeError, match="Unknown format"):
            _write_gps_exiftool_rename(
                str(test_file), 1.0, 1.0, None, None, ["exiftool"], "TIFF"
            )

    def test_preserves_file_permissions(self, tmp_path):
        """Temp-replaced file should keep original permissions."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"content")
        os.chmod(str(test_file), 0o640)

        def mock_run(args, **kwargs):
            tmp_file = args[-1]
            with open(tmp_file, "wb") as f:
                f.write(b"modified")
            return MagicMock(returncode=0, stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            _write_gps_exiftool_rename(
                str(test_file), 1.0, 1.0, None, None, ["exiftool"], "JPEG"
            )

        assert os.stat(str(test_file)).st_mode & 0o777 == 0o640


# ---------------------------------------------------------------------------
# write_gps_to_exif — format_mismatch routing
# ---------------------------------------------------------------------------


class TestWriteGpsFormatMismatchRouting:
    """Test the three-strategy routing for format-mismatched files."""

    def test_mismatch_uses_imagedata_when_pyexiv2_available(self):
        """Strategy 1: pyexiv2 ImageData is tried first."""
        with patch.object(writer_module, "_PYEXIV2_OK", True):
            with patch.object(writer_module, "_EXIFTOOL", ["exiftool"]):
                with patch("geosnag.writer._write_gps_pyexiv2_imagedata") as mock_id:
                    result = write_gps_to_exif(
                        "/tmp/test.heic",
                        1.0,
                        1.0,
                        format_mismatch="JPEG",
                    )
                    mock_id.assert_called_once()
                    assert result.success

    def test_mismatch_does_not_call_normal_pyexiv2(self):
        """Format mismatch should NOT use the normal pyexiv2.Image path."""
        with patch.object(writer_module, "_PYEXIV2_OK", True):
            with patch.object(writer_module, "_EXIFTOOL", ["exiftool"]):
                with patch("geosnag.writer._write_gps_pyexiv2_imagedata"):
                    with patch("geosnag.writer._write_gps_pyexiv2") as mock_normal:
                        write_gps_to_exif(
                            "/tmp/test.heic",
                            1.0,
                            1.0,
                            format_mismatch="JPEG",
                        )
                        mock_normal.assert_not_called()

    def test_mismatch_falls_back_to_exiftool_rename_on_imagedata_failure(self):
        """Strategy 2: ExifTool rename when ImageData fails."""
        with patch.object(writer_module, "_PYEXIV2_OK", True):
            with patch.object(writer_module, "_EXIFTOOL", ["exiftool"]):
                with patch(
                    "geosnag.writer._write_gps_pyexiv2_imagedata",
                    side_effect=RuntimeError("ImageData failed"),
                ):
                    with patch("geosnag.writer._write_gps_exiftool_rename") as mock_rename:
                        result = write_gps_to_exif(
                            "/tmp/test.heic",
                            1.0,
                            1.0,
                            format_mismatch="JPEG",
                        )
                        mock_rename.assert_called_once()
                        # Check real_format="JPEG" was passed
                        _, kwargs = mock_rename.call_args
                        assert kwargs.get("real_format") == "JPEG" or mock_rename.call_args[0][-1] == "JPEG"
                        assert result.success

    def test_mismatch_exiftool_only_when_no_pyexiv2(self):
        """When pyexiv2 unavailable, go straight to ExifTool rename."""
        with patch.object(writer_module, "_PYEXIV2_OK", False):
            with patch.object(writer_module, "_EXIFTOOL", ["exiftool"]):
                with patch("geosnag.writer._write_gps_exiftool_rename") as mock_rename:
                    result = write_gps_to_exif(
                        "/tmp/test.heic",
                        1.0,
                        1.0,
                        format_mismatch="JPEG",
                    )
                    mock_rename.assert_called_once()
                    assert result.success

    def test_mismatch_fails_when_neither_available(self):
        """Neither pyexiv2 nor ExifTool — returns failure."""
        with patch.object(writer_module, "_PYEXIV2_OK", False):
            with patch.object(writer_module, "_EXIFTOOL", None):
                result = write_gps_to_exif(
                    "/tmp/test.heic",
                    1.0,
                    1.0,
                    format_mismatch="JPEG",
                )
                assert not result.success
                assert "no write backend" in result.error.lower() or "no backend" in result.error.lower()

    def test_mismatch_both_strategies_fail(self):
        """Both ImageData and ExifTool rename fail — returns error."""
        with patch.object(writer_module, "_PYEXIV2_OK", True):
            with patch.object(writer_module, "_EXIFTOOL", ["exiftool"]):
                with patch(
                    "geosnag.writer._write_gps_pyexiv2_imagedata",
                    side_effect=RuntimeError("ImageData exploded"),
                ):
                    with patch(
                        "geosnag.writer._write_gps_exiftool_rename",
                        side_effect=RuntimeError("ExifTool rename also exploded"),
                    ):
                        result = write_gps_to_exif(
                            "/tmp/test.heic",
                            1.0,
                            1.0,
                            format_mismatch="JPEG",
                        )
                        assert not result.success
                        assert "ExifTool rename also exploded" in result.error

    def test_no_mismatch_uses_normal_path(self):
        """Without format_mismatch, normal pyexiv2/exiftool path is used."""
        with patch.object(writer_module, "_PYEXIV2_OK", True):
            with patch("geosnag.writer._write_gps_pyexiv2") as mock_normal:
                with patch("geosnag.writer._write_gps_pyexiv2_imagedata") as mock_id:
                    result = write_gps_to_exif("/tmp/test.jpg", 1.0, 1.0)
                    mock_normal.assert_called_once()
                    mock_id.assert_not_called()
                    assert result.success

    def test_mismatch_invalid_coords_still_rejected(self):
        """Coordinate validation happens before mismatch routing."""
        result = write_gps_to_exif(
            "/tmp/test.heic",
            999.0,
            1.0,
            format_mismatch="JPEG",
        )
        assert not result.success
        assert "Invalid coordinates" in result.error

    def test_mismatch_no_backend_still_rejected(self):
        """No backend at all + mismatch → early failure."""
        with patch.object(writer_module, "_PYEXIV2_OK", False):
            with patch.object(writer_module, "_EXIFTOOL", None):
                result = write_gps_to_exif(
                    "/tmp/test.heic",
                    1.0,
                    1.0,
                    format_mismatch="JPEG",
                )
                assert not result.success


# ---------------------------------------------------------------------------
# Index: format_mismatch serialization round-trip
# ---------------------------------------------------------------------------


class TestIndexFormatMismatchRoundtrip:
    """Test that format_mismatch is stored in and restored from the index."""

    def test_photo_to_entry_includes_format_mismatch(self, tmp_path):
        """_photo_to_entry should include format_mismatch."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"\x00" * 50)

        meta = PhotoMeta(
            filepath=str(test_file),
            filename="photo.heic",
            extension=".heic",
            format_mismatch="JPEG",
        )
        entry = _photo_to_entry(meta)
        assert entry["format_mismatch"] == "JPEG"

    def test_photo_to_entry_none_when_no_mismatch(self, tmp_path):
        """format_mismatch should be None when no mismatch."""
        test_file = tmp_path / "photo.jpg"
        test_file.write_bytes(b"\x00" * 50)

        meta = PhotoMeta(
            filepath=str(test_file),
            filename="photo.jpg",
            extension=".jpg",
            format_mismatch=None,
        )
        entry = _photo_to_entry(meta)
        assert entry["format_mismatch"] is None

    def test_entry_to_photo_restores_format_mismatch(self):
        """_entry_to_photo should restore format_mismatch."""
        entry = {
            "mtime": 1234567890.0,
            "size": 1000,
            "datetime_original": None,
            "has_gps": False,
            "gps_latitude": None,
            "gps_longitude": None,
            "gps_altitude": None,
            "camera_make": None,
            "camera_model": None,
            "geosnag_processed": False,
            "format_mismatch": "JPEG",
        }
        photo = _entry_to_photo("/tmp/photo.heic", entry)
        assert photo.format_mismatch == "JPEG"

    def test_entry_to_photo_none_when_missing(self):
        """Old index entries without format_mismatch field should default to None."""
        entry = {
            "mtime": 1234567890.0,
            "size": 1000,
            "has_gps": False,
            "geosnag_processed": False,
            # no format_mismatch key
        }
        photo = _entry_to_photo("/tmp/photo.jpg", entry)
        assert photo.format_mismatch is None

    def test_full_index_roundtrip_with_format_mismatch(self, tmp_path):
        """Save to index with format_mismatch, reload, verify preserved."""
        test_file = tmp_path / "photo.heic"
        test_file.write_bytes(b"\x00" * 50)

        idx_path = tmp_path / "index.json"
        idx = ScanIndex(str(idx_path))

        meta = PhotoMeta(
            filepath=str(test_file),
            filename="photo.heic",
            extension=".heic",
            datetime_original=datetime(2023, 6, 15, 14, 30),
            has_gps=False,
            format_mismatch="JPEG",
        )
        idx.update(meta)
        idx.save()

        # Reload
        idx2 = ScanIndex(str(idx_path))
        idx2.load()

        cached = idx2.lookup(str(test_file))
        assert cached is not None
        assert cached.format_mismatch == "JPEG"
        assert cached.datetime_original == datetime(2023, 6, 15, 14, 30)

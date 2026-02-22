"""
Unit tests for geosnag/writer.py.

Covers:
- _probe_cmd: all subprocess failure modes
- _has_pyexiv2: module missing, glibc OSError, other exception
- _find_exiftool: system binary probe, each candidate path
- _write_gps_exiftool: correct subprocess args for every combination
- _stamp_exiftool: correct subprocess args, non-zero exit raises
- write_gps_to_exif: routing to pyexiv2 / exiftool / neither
- stamp_processed: routing to pyexiv2 / exiftool / neither
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

import geosnag.writer as writer_module
from geosnag.writer import (
    _find_exiftool,
    _has_pyexiv2,
    _probe_cmd,
    _stamp_exiftool,
    _write_gps_exiftool,
    stamp_processed,
    write_gps_to_exif,
)


# ---------------------------------------------------------------------------
# _probe_cmd
# ---------------------------------------------------------------------------


class TestProbeCmd:
    def test_returns_true_on_zero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert _probe_cmd(["exiftool"]) is True

    def test_appends_ver_flag(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _probe_cmd(["exiftool"])
            mock_run.assert_called_once_with(["exiftool", "-ver"], capture_output=True, timeout=5)

    def test_ver_flag_appended_to_multi_word_cmd(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _probe_cmd(["perl", "/path/to/exiftool"])
            mock_run.assert_called_once_with(["perl", "/path/to/exiftool", "-ver"], capture_output=True, timeout=5)

    def test_returns_false_on_nonzero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert _probe_cmd(["exiftool"]) is False

    def test_returns_false_on_file_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _probe_cmd(["notexist"]) is False

    def test_returns_false_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("exiftool", 5)):
            assert _probe_cmd(["exiftool"]) is False

    def test_returns_false_on_oserror(self):
        with patch("subprocess.run", side_effect=OSError("exec failed")):
            assert _probe_cmd(["exiftool"]) is False


# ---------------------------------------------------------------------------
# _has_pyexiv2
# ---------------------------------------------------------------------------


class TestHasPyexiv2:
    def test_returns_false_when_spec_not_found(self):
        with patch("importlib.util.find_spec", return_value=None):
            assert _has_pyexiv2() is False

    def test_returns_false_on_oserror(self):
        """Simulates glibc version mismatch on Synology."""
        with patch("importlib.util.find_spec", return_value=MagicMock()):
            with patch("builtins.__import__", side_effect=OSError("GLIBC_2.32 not found")):
                assert _has_pyexiv2() is False

    def test_returns_false_on_other_exception(self):
        with patch("importlib.util.find_spec", return_value=MagicMock()):
            with patch("builtins.__import__", side_effect=RuntimeError("unexpected")):
                assert _has_pyexiv2() is False


# ---------------------------------------------------------------------------
# _find_exiftool
# ---------------------------------------------------------------------------


class TestFindExiftool:
    def test_returns_none_when_nothing_available(self):
        with patch("geosnag.writer._probe_cmd", return_value=False):
            assert _find_exiftool() is None

    def test_returns_system_binary_when_available(self):
        def probe(cmd):
            return cmd == ["exiftool"]

        with patch("geosnag.writer._probe_cmd", side_effect=probe):
            result = _find_exiftool()
            assert result == ["exiftool"]

    def test_returns_opt_bin_when_system_missing(self):
        def probe(cmd):
            return cmd == ["/opt/bin/exiftool"]

        with patch("geosnag.writer._probe_cmd", side_effect=probe):
            result = _find_exiftool()
            assert result == ["/opt/bin/exiftool"]

    def test_system_binary_probed_before_opt_bin(self):
        probed = []

        def probe(cmd):
            probed.append(cmd[0])
            return False

        with patch("geosnag.writer._probe_cmd", side_effect=probe):
            _find_exiftool()

        assert probed.index("exiftool") < probed.index("/opt/bin/exiftool")

    def test_return_type_is_list(self):
        with patch("geosnag.writer._probe_cmd", return_value=True):
            result = _find_exiftool()
            assert result is None or isinstance(result, list)


# ---------------------------------------------------------------------------
# _write_gps_exiftool
# ---------------------------------------------------------------------------


class TestWriteGpsExiftool:
    def _run(self, **kwargs):
        defaults = dict(
            filepath="/tmp/test.NEF",
            latitude=59.9139,
            longitude=10.7522,
            altitude=None,
            stamp=None,
            exiftool=["exiftool"],
        )
        defaults.update(kwargs)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _write_gps_exiftool(**defaults)
            return mock_run.call_args[0][0]  # the args list

    def test_north_latitude_ref(self):
        args = self._run(latitude=59.9)
        assert "-GPSLatitudeRef=N" in args

    def test_south_latitude_ref(self):
        args = self._run(latitude=-33.8)
        assert "-GPSLatitudeRef=S" in args

    def test_east_longitude_ref(self):
        args = self._run(longitude=10.7)
        assert "-GPSLongitudeRef=E" in args

    def test_west_longitude_ref(self):
        args = self._run(longitude=-73.9)
        assert "-GPSLongitudeRef=W" in args

    def test_absolute_lat_lon_value(self):
        args = self._run(latitude=-33.8688, longitude=-70.6693)
        assert "-GPSLatitude=33.8688" in args
        assert "-GPSLongitude=70.6693" in args

    def test_wgs84_datum(self):
        args = self._run()
        assert "-GPSMapDatum=WGS-84" in args

    def test_overwrite_original_flag(self):
        args = self._run()
        assert "-overwrite_original" in args

    def test_altitude_above_sea_level(self):
        args = self._run(altitude=150.5)
        assert "-GPSAltitude=150.5" in args
        assert "-GPSAltitudeRef=0" in args

    def test_altitude_below_sea_level(self):
        args = self._run(altitude=-10.0)
        assert "-GPSAltitude=10.0" in args
        assert "-GPSAltitudeRef=1" in args

    def test_no_altitude_args_when_none(self):
        args = self._run(altitude=None)
        assert not any("GPSAltitude" in a for a in args)

    def test_stamp_included(self):
        args = self._run(stamp="GeoSnag:v0.1.1:2026-01-01T00:00:00")
        assert any("Software" in a for a in args)

    def test_no_stamp_when_none(self):
        args = self._run(stamp=None)
        assert not any("Software" in a for a in args)

    def test_filepath_is_last_arg(self):
        args = self._run(filepath="/photos/test.NEF")
        assert args[-1] == "/photos/test.NEF"

    def test_multi_word_cmd_spread(self):
        """['perl', '/path/exiftool'] is spread correctly into args."""
        args = self._run(exiftool=["perl", "/path/to/exiftool"])
        assert args[0] == "perl"
        assert args[1] == "/path/to/exiftool"

    def test_raises_on_nonzero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="Error: file not found")
            with pytest.raises(RuntimeError, match="Error: file not found"):
                _write_gps_exiftool("/tmp/x.NEF", 1.0, 1.0, None, None, ["exiftool"])

    def test_raises_generic_message_on_empty_stderr(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="")
            with pytest.raises(RuntimeError, match="non-zero exit code"):
                _write_gps_exiftool("/tmp/x.NEF", 1.0, 1.0, None, None, ["exiftool"])


# ---------------------------------------------------------------------------
# _stamp_exiftool
# ---------------------------------------------------------------------------


class TestStampExiftool:
    def test_correct_args(self):
        stamp = "GeoSnag:v0.1.1:2026-01-01T00:00:00"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _stamp_exiftool("/tmp/test.NEF", stamp, ["exiftool"])
            args = mock_run.call_args[0][0]

        assert args[0] == "exiftool"
        assert "-overwrite_original" in args
        assert f"-Software={stamp}" in args
        assert args[-1] == "/tmp/test.NEF"

    def test_multi_word_cmd_spread(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _stamp_exiftool("/tmp/test.NEF", "stamp", ["perl", "/path/exiftool"])
            args = mock_run.call_args[0][0]

        assert args[0] == "perl"
        assert args[1] == "/path/exiftool"

    def test_raises_on_nonzero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="stamp failed")
            with pytest.raises(RuntimeError, match="stamp failed"):
                _stamp_exiftool("/tmp/test.NEF", "stamp", ["exiftool"])


# ---------------------------------------------------------------------------
# write_gps_to_exif — backend routing
# ---------------------------------------------------------------------------


class TestWriteGpsToExifRouting:
    def test_uses_pyexiv2_when_available(self):
        with patch.object(writer_module, "_PYEXIV2_OK", True):
            with patch("geosnag.writer._write_gps_pyexiv2") as mock_pyexiv2:
                write_gps_to_exif("/tmp/test.NEF", 1.0, 1.0)
                mock_pyexiv2.assert_called_once()

    def test_does_not_call_exiftool_when_pyexiv2_ok(self):
        with patch.object(writer_module, "_PYEXIV2_OK", True):
            with patch("geosnag.writer._write_gps_pyexiv2"):
                with patch("geosnag.writer._write_gps_exiftool") as mock_exiftool:
                    write_gps_to_exif("/tmp/test.NEF", 1.0, 1.0)
                    mock_exiftool.assert_not_called()

    def test_uses_exiftool_when_pyexiv2_unavailable(self):
        with patch.object(writer_module, "_PYEXIV2_OK", False):
            with patch.object(writer_module, "_EXIFTOOL", ["exiftool"]):
                with patch("geosnag.writer._write_gps_exiftool") as mock_exiftool:
                    result = write_gps_to_exif("/tmp/test.NEF", 1.0, 1.0)
                    mock_exiftool.assert_called_once()
                    assert result.success

    def test_returns_failure_when_neither_available(self):
        with patch.object(writer_module, "_PYEXIV2_OK", False):
            with patch.object(writer_module, "_EXIFTOOL", None):
                result = write_gps_to_exif("/tmp/test.NEF", 1.0, 1.0)
                assert not result.success
                assert result.error is not None

    def test_returns_failure_result_on_pyexiv2_exception(self):
        with patch.object(writer_module, "_PYEXIV2_OK", True):
            with patch("geosnag.writer._write_gps_pyexiv2", side_effect=RuntimeError("write failed")):
                result = write_gps_to_exif("/tmp/test.NEF", 1.0, 1.0)
                assert not result.success
                assert "write failed" in result.error

    def test_returns_failure_result_on_exiftool_exception(self):
        with patch.object(writer_module, "_PYEXIV2_OK", False):
            with patch.object(writer_module, "_EXIFTOOL", ["exiftool"]):
                with patch(
                    "geosnag.writer._write_gps_exiftool",
                    side_effect=RuntimeError("exiftool failed"),
                ):
                    result = write_gps_to_exif("/tmp/test.NEF", 1.0, 1.0)
                    assert not result.success
                    assert "exiftool failed" in result.error

    def test_passes_exiftool_cmd_to_backend(self):
        cmd = ["exiftool"]
        with patch.object(writer_module, "_PYEXIV2_OK", False):
            with patch.object(writer_module, "_EXIFTOOL", cmd):
                with patch("geosnag.writer._write_gps_exiftool") as mock_et:
                    mock_et.return_value = None
                    write_gps_to_exif("/tmp/test.NEF", 1.0, 1.0)
                    assert mock_et.call_args[0][-1] == cmd  # last positional = exiftool


# ---------------------------------------------------------------------------
# stamp_processed — backend routing
# ---------------------------------------------------------------------------


class TestStampProcessedRouting:
    def test_uses_pyexiv2_when_available(self):
        with patch.object(writer_module, "_PYEXIV2_OK", True):
            with patch("geosnag.writer._stamp_pyexiv2") as mock_stamp:
                stamp_processed("/tmp/test.NEF")
                mock_stamp.assert_called_once()

    def test_uses_exiftool_when_pyexiv2_unavailable(self):
        with patch.object(writer_module, "_PYEXIV2_OK", False):
            with patch.object(writer_module, "_EXIFTOOL", ["exiftool"]):
                with patch("geosnag.writer._stamp_exiftool") as mock_stamp:
                    result = stamp_processed("/tmp/test.NEF")
                    mock_stamp.assert_called_once()
                    assert result is True

    def test_returns_false_when_neither_available(self):
        with patch.object(writer_module, "_PYEXIV2_OK", False):
            with patch.object(writer_module, "_EXIFTOOL", None):
                assert stamp_processed("/tmp/test.NEF") is False

    def test_returns_false_on_exception(self):
        with patch.object(writer_module, "_PYEXIV2_OK", True):
            with patch("geosnag.writer._stamp_pyexiv2", side_effect=RuntimeError("disk full")):
                assert stamp_processed("/tmp/test.NEF") is False

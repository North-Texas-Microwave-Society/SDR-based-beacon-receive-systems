import sys

import pytest

import beacon_reporter


def test_read_new_rows_waits_for_complete_row(tmp_path):
    path = tmp_path / "observations.csv"
    header = "timestamp_utc,center_freq_hz\n"
    path.write_text(header + "first,1\npartial,", encoding="utf-8")

    rows, offset = beacon_reporter.read_new_rows(str(path), 0)

    assert rows == [{"timestamp_utc": "first", "center_freq_hz": "1"}]
    assert offset == len((header + "first,1\n").encode())

    with path.open("a", encoding="utf-8") as output:
        output.write("2\n")
    rows, _ = beacon_reporter.read_new_rows(str(path), offset)
    assert rows == [{"timestamp_utc": "partial", "center_freq_hz": "2"}]


def test_read_new_rows_recovers_after_file_truncation(tmp_path):
    path = tmp_path / "observations.csv"
    path.write_text("timestamp_utc,center_freq_hz\nold,1\n", encoding="utf-8")
    _, old_offset = beacon_reporter.read_new_rows(str(path), 0)
    path.write_text("timestamp_utc,center_freq_hz\nnew,2\n", encoding="utf-8")

    rows, _ = beacon_reporter.read_new_rows(str(path), old_offset + 100)

    assert rows == [{"timestamp_utc": "new", "center_freq_hz": "2"}]


def test_reporter_converts_configured_passband_khz_to_hz(tmp_path, monkeypatch):
    config = tmp_path / "beacon_config.py"
    config.write_text("PASSBAND_KHZ = 5\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "beacon_reporter.py",
            "--config",
            str(config),
            "--monitor-token",
            "token",
            "--beacon-id",
            "id",
        ],
    )

    assert beacon_reporter.parse_args().passband_hz == pytest.approx(5000)

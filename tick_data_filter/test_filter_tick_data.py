import os
import re
import tempfile
from datetime import date, datetime, timedelta

import pytest

from filter_tick_data import (
    _format_progress_bar,
    _read_last_line,
    build_intervals,
    default_output_path,
    filter_ticks,
    parse_forex_factory_events,
    parse_tick_timestamp,
)


# ---------------------------------------------------------------------------
# parse_tick_timestamp
# ---------------------------------------------------------------------------

class TestParseTickTimestamp:
    def test_basic(self):
        result = parse_tick_timestamp("2023.02.15 06:48:01.656")
        assert result == datetime(2023, 2, 15, 6, 48, 1, 656000)

    def test_midnight(self):
        result = parse_tick_timestamp("2023.01.01 00:00:00.000")
        assert result == datetime(2023, 1, 1, 0, 0, 0, 0)

    def test_end_of_day(self):
        result = parse_tick_timestamp("2023.12.31 23:59:59.999")
        assert result == datetime(2023, 12, 31, 23, 59, 59, 999000)


# ---------------------------------------------------------------------------
# parse_forex_factory_events
# ---------------------------------------------------------------------------

def _write_ff_file(tmp_dir, lines):
    path = os.path.join(tmp_dir, "ff.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        for line in lines:
            f.write(line + "\n")
    return path


FF_LINES = [
    "2023-02-15 07:00:00+00:00,GBP,High Impact Expected,CPI y/y,3.0%,2.8%,2.7%",
    "2023-02-15 09:30:00+00:00,GBP,Medium Impact Expected,Final Manufacturing PMI,51.9,53.0,52.5",
    "2023-02-15 13:30:00+00:00,USD,Low Impact Expected,Unemployment Claims,329K,318K,317K",
    "2023-02-15 22:59:59+00:00,USD,Non-Economic,Bank Holiday,,,",
]


class TestParseForexFactoryEvents:
    def test_high_only(self, tmp_path):
        path = _write_ff_file(str(tmp_path), FF_LINES)
        events = parse_forex_factory_events(path, include_high=True, include_med=False, include_low=False)
        assert events == [datetime(2023, 2, 15, 7, 0, 0)]

    def test_med_only(self, tmp_path):
        path = _write_ff_file(str(tmp_path), FF_LINES)
        events = parse_forex_factory_events(path, include_high=False, include_med=True, include_low=False)
        assert events == [datetime(2023, 2, 15, 9, 30, 0)]

    def test_low_only(self, tmp_path):
        path = _write_ff_file(str(tmp_path), FF_LINES)
        events = parse_forex_factory_events(path, include_high=False, include_med=False, include_low=True)
        assert events == [datetime(2023, 2, 15, 13, 30, 0)]

    def test_all_impacts(self, tmp_path):
        path = _write_ff_file(str(tmp_path), FF_LINES)
        events = parse_forex_factory_events(path, include_high=True, include_med=True, include_low=True)
        assert len(events) == 3
        # Non-Economic should never be included
        assert datetime(2023, 2, 15, 22, 59, 59) not in events

    def test_none_selected(self, tmp_path):
        path = _write_ff_file(str(tmp_path), FF_LINES)
        events = parse_forex_factory_events(path, include_high=False, include_med=False, include_low=False)
        assert events == []

    def test_unsorted_input_returns_sorted(self, tmp_path):
        lines = [
            "2023-02-15 13:30:00+00:00,USD,High Impact Expected,NFP,167K,115K,132K",
            "2023-02-15 07:00:00+00:00,GBP,High Impact Expected,CPI y/y,3.0%,2.8%,2.7%",
            "2023-02-15 09:30:00+00:00,GBP,High Impact Expected,GDP q/q,0.8%,0.7%,0.7%",
        ]
        path = _write_ff_file(str(tmp_path), lines)
        events = parse_forex_factory_events(path, include_high=True, include_med=False, include_low=False)
        assert events == sorted(events)


# ---------------------------------------------------------------------------
# build_intervals
# ---------------------------------------------------------------------------

class TestBuildIntervals:
    def test_empty(self):
        assert build_intervals([], 60, 60) == []

    def test_single_event(self):
        ev = datetime(2023, 2, 15, 7, 0, 0)
        result = build_intervals([ev], 60, 60)
        assert result == [(ev - timedelta(seconds=60), ev + timedelta(seconds=60))]

    def test_non_overlapping(self):
        ev1 = datetime(2023, 2, 15, 7, 0, 0)
        ev2 = datetime(2023, 2, 15, 9, 0, 0)
        result = build_intervals([ev1, ev2], 60, 60)
        assert len(result) == 2

    def test_overlapping_merge(self):
        ev1 = datetime(2023, 2, 15, 7, 0, 0)
        ev2 = datetime(2023, 2, 15, 7, 0, 30)  # 30s apart, with 60s window they overlap
        result = build_intervals([ev1, ev2], 60, 60)
        assert len(result) == 1
        assert result[0] == (ev1 - timedelta(seconds=60), ev2 + timedelta(seconds=60))

    def test_adjacent_merge(self):
        ev1 = datetime(2023, 2, 15, 7, 0, 0)
        ev2 = datetime(2023, 2, 15, 7, 2, 0)  # exactly 120s apart, windows touch
        result = build_intervals([ev1, ev2], 60, 60)
        assert len(result) == 1

    def test_unsorted_events(self):
        ev1 = datetime(2023, 2, 15, 9, 0, 0)
        ev2 = datetime(2023, 2, 15, 7, 0, 0)
        result = build_intervals([ev2, ev1], 60, 60)
        assert result[0][0] < result[-1][1]
        assert len(result) == 2

    def test_three_events_partial_merge(self):
        ev1 = datetime(2023, 2, 15, 7, 0, 0)
        ev2 = datetime(2023, 2, 15, 7, 0, 30)  # overlaps with ev1
        ev3 = datetime(2023, 2, 15, 9, 0, 0)   # separate
        result = build_intervals([ev1, ev2, ev3], 60, 60)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# filter_ticks
# ---------------------------------------------------------------------------

def _write_tick_file(tmp_dir, lines):
    path = os.path.join(tmp_dir, "ticks.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        for line in lines:
            f.write(line + "\n")
    return path


def _read_output(path):
    with open(path, "r", encoding="utf-8") as f:
        return [line.rstrip("\n\r") for line in f if line.strip()]


class TestFilterTicks:
    def test_basic_filtering(self, tmp_path):
        tick_lines = [
            "2023.02.15 06:58:30.000;1.20550;1.20569",  # 90s before event -> outside
            "2023.02.15 06:59:00.000;1.20551;1.20570",  # 60s before -> inside (boundary)
            "2023.02.15 06:59:30.000;1.20552;1.20571",  # 30s before -> inside
            "2023.02.15 07:00:00.000;1.20553;1.20572",  # at event -> inside
            "2023.02.15 07:00:30.000;1.20554;1.20573",  # 30s after -> inside
            "2023.02.15 07:01:00.000;1.20555;1.20574",  # 60s after -> inside (boundary)
            "2023.02.15 07:01:30.000;1.20556;1.20575",  # 90s after -> outside
        ]
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        ev = datetime(2023, 2, 15, 7, 0, 0)
        intervals = build_intervals([ev], 60, 60)

        filter_ticks(tick_file, output_file, intervals, show_progress=False)
        result = _read_output(output_file)
        assert len(result) == 5
        assert result[0].startswith("2023.02.15 06:59:00.000")
        assert result[-1].startswith("2023.02.15 07:01:00.000")

    def test_no_matching_ticks(self, tmp_path):
        tick_lines = [
            "2023.02.15 08:00:00.000;1.20550;1.20569",
            "2023.02.15 08:00:01.000;1.20551;1.20570",
        ]
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        ev = datetime(2023, 2, 15, 7, 0, 0)
        intervals = build_intervals([ev], 60, 60)

        filter_ticks(tick_file, output_file, intervals, show_progress=False)
        result = _read_output(output_file)
        assert result == []

    def test_all_ticks_match(self, tmp_path):
        tick_lines = [
            "2023.02.15 06:59:30.000;1.20550;1.20569",
            "2023.02.15 07:00:00.000;1.20551;1.20570",
            "2023.02.15 07:00:30.000;1.20552;1.20571",
        ]
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        ev = datetime(2023, 2, 15, 7, 0, 0)
        intervals = build_intervals([ev], 60, 60)

        filter_ticks(tick_file, output_file, intervals, show_progress=False)
        result = _read_output(output_file)
        assert len(result) == 3

    def test_empty_intervals(self, tmp_path):
        tick_lines = [
            "2023.02.15 07:00:00.000;1.20550;1.20569",
        ]
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        filter_ticks(tick_file, output_file, [], show_progress=False)
        result = _read_output(output_file)
        assert result == []

    def test_multiple_intervals(self, tmp_path):
        tick_lines = [
            "2023.02.15 06:59:30.000;1.20550;1.20569",  # in interval 1
            "2023.02.15 07:00:30.000;1.20551;1.20570",  # in interval 1
            "2023.02.15 08:00:00.000;1.20552;1.20571",  # between intervals
            "2023.02.15 08:59:30.000;1.20553;1.20572",  # in interval 2
            "2023.02.15 09:00:30.000;1.20554;1.20573",  # in interval 2
            "2023.02.15 10:00:00.000;1.20555;1.20574",  # after all intervals
        ]
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        ev1 = datetime(2023, 2, 15, 7, 0, 0)
        ev2 = datetime(2023, 2, 15, 9, 0, 0)
        intervals = build_intervals([ev1, ev2], 60, 60)

        filter_ticks(tick_file, output_file, intervals, show_progress=False)
        result = _read_output(output_file)
        assert len(result) == 4

    def test_unsorted_tick_data_raises(self, tmp_path):
        tick_lines = [
            "2023.02.15 07:00:01.000;1.20550;1.20569",
            "2023.02.15 07:00:00.000;1.20551;1.20570",  # out of order!
        ]
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        ev = datetime(2023, 2, 15, 7, 0, 0)
        intervals = build_intervals([ev], 60, 60)

        with pytest.raises(ValueError, match="not sorted"):
            filter_ticks(tick_file, output_file, intervals, show_progress=False)

    def test_ticks_after_all_intervals_early_exit(self, tmp_path):
        tick_lines = [
            "2023.02.15 06:59:30.000;1.20550;1.20569",  # in interval
            "2023.02.15 07:02:00.000;1.20551;1.20570",  # past interval
            "2023.02.15 07:03:00.000;1.20552;1.20571",  # past interval
            "2023.02.15 07:04:00.000;1.20553;1.20572",  # past interval
        ]
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        ev = datetime(2023, 2, 15, 7, 0, 0)
        intervals = build_intervals([ev], 60, 60)

        filter_ticks(tick_file, output_file, intervals, show_progress=False)
        result = _read_output(output_file)
        assert len(result) == 1

    def test_min_date(self, tmp_path):
        tick_lines = [
            "2023.02.14 06:59:30.000;1.20550;1.20569",  # day before min_date
            "2023.02.15 06:59:30.000;1.20551;1.20570",  # in interval, on min_date
            "2023.02.15 07:00:00.000;1.20552;1.20571",  # in interval, on min_date
        ]
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        ev1 = datetime(2023, 2, 14, 7, 0, 0)
        ev2 = datetime(2023, 2, 15, 7, 0, 0)
        intervals = build_intervals([ev1, ev2], 60, 60)

        filter_ticks(tick_file, output_file, intervals, show_progress=False, min_date=date(2023, 2, 15))
        result = _read_output(output_file)
        assert len(result) == 2
        assert result[0].startswith("2023.02.15")

    def test_max_date(self, tmp_path):
        tick_lines = [
            "2023.02.15 06:59:30.000;1.20550;1.20569",  # in interval
            "2023.02.15 07:00:00.000;1.20551;1.20570",  # in interval
            "2023.02.16 06:59:30.000;1.20552;1.20571",  # day after max_date
        ]
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        ev1 = datetime(2023, 2, 15, 7, 0, 0)
        ev2 = datetime(2023, 2, 16, 7, 0, 0)
        intervals = build_intervals([ev1, ev2], 60, 60)

        filter_ticks(tick_file, output_file, intervals, show_progress=False, max_date=date(2023, 2, 15))
        result = _read_output(output_file)
        assert len(result) == 2
        assert all(line.startswith("2023.02.15") for line in result)

    def test_min_and_max_date(self, tmp_path):
        tick_lines = [
            "2023.02.14 07:00:00.000;1.20550;1.20569",  # before min
            "2023.02.15 06:59:30.000;1.20551;1.20570",  # in range, in interval
            "2023.02.16 06:59:30.000;1.20552;1.20571",  # in range, in interval
            "2023.02.17 06:59:30.000;1.20553;1.20572",  # after max
        ]
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        evs = [datetime(2023, 2, d, 7, 0, 0) for d in range(14, 18)]
        intervals = build_intervals(evs, 60, 60)

        filter_ticks(tick_file, output_file, intervals, show_progress=False,
                     min_date=date(2023, 2, 15), max_date=date(2023, 2, 16))
        result = _read_output(output_file)
        assert len(result) == 2
        assert result[0].startswith("2023.02.15")
        assert result[1].startswith("2023.02.16")

    def test_tick_file_with_header(self, tmp_path):
        tick_lines = [
            "DateTime;Bid;Ask",  # header line
            "2023.02.15 06:59:00.000;1.20551;1.20570",
            "2023.02.15 07:00:00.000;1.20552;1.20571",
            "2023.02.15 07:01:00.000;1.20553;1.20572",
        ]
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        intervals = build_intervals([datetime(2023, 2, 15, 7, 0, 0)], 60, 60)
        filter_ticks(tick_file, output_file, intervals, show_progress=False)
        result = _read_output(output_file)
        assert len(result) == 3
        assert "DateTime" not in result[0]


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

class TestProgressHelpers:
    @staticmethod
    def _strip_ansi(text):
        return re.sub(r"\033\[[0-9;]*m", "", text)

    def test_format_progress_bar_zero(self):
        bar = _format_progress_bar(0.0, "2023-02-15", 1, 365, bar_width=40)
        plain = self._strip_ansi(bar)
        assert "0.0%" in plain
        assert "2023-02-15" in plain
        assert "day 1/365" in plain
        assert bar.startswith("\r[")

    def test_format_progress_bar_fifty(self):
        bar = _format_progress_bar(50.0, "2023-08-15", 180, 365, bar_width=40)
        plain = self._strip_ansi(bar)
        assert "50.0%" in plain
        assert "=" in plain

    def test_format_progress_bar_hundred(self):
        bar = _format_progress_bar(100.0, "2024-02-14", 365, 365, bar_width=40)
        plain = self._strip_ansi(bar)
        assert "100.0%" in plain
        assert "day 365/365" in plain

    def test_format_progress_bar_has_green(self):
        bar = _format_progress_bar(50.0, "2023-08-15", 180, 365, bar_width=40)
        assert "\033[32m" in bar
        assert "\033[0m" in bar

    def test_read_last_line(self, tmp_path):
        tick_file = os.path.join(str(tmp_path), "ticks.csv")
        with open(tick_file, "w", encoding="utf-8") as f:
            f.write("2023.02.15 06:48:01.656;1.20550;1.20569\n")
            f.write("2023.02.15 06:48:02.000;1.20551;1.20570\n")
        last = _read_last_line(tick_file)
        assert last == "2023.02.15 06:48:02.000;1.20551;1.20570"

    def test_read_last_line_empty(self, tmp_path):
        tick_file = os.path.join(str(tmp_path), "empty.csv")
        with open(tick_file, "w", encoding="utf-8") as f:
            pass
        assert _read_last_line(tick_file) is None


# ---------------------------------------------------------------------------
# default_output_path
# ---------------------------------------------------------------------------

class TestDefaultOutputPath:
    def test_csv_with_dates(self):
        assert default_output_path("data/ticks.csv", date(2020, 1, 1), date(2020, 12, 31)) == "data/ticks_filtered_2020-01-01-2020-12-31.csv"

    def test_no_extension_with_dates(self):
        assert default_output_path("data/ticks", date(2020, 1, 1), date(2020, 12, 31)) == "data/ticks_filtered_2020-01-01-2020-12-31"

    def test_complex_path_with_dates(self):
        result = default_output_path("/some/path/tick_data_GBPUSD.csv", date(2023, 6, 15), date(2024, 3, 1))
        assert result == "/some/path/tick_data_GBPUSD_filtered_2023-06-15-2024-03-01.csv"

    def test_only_min_date(self, tmp_path):
        tick_file = tmp_path / "ticks.csv"
        tick_file.write_text("2024.01.10 08:00:00.000;1.1000;1.1001\n2024.03.20 16:00:00.000;1.1050;1.1051\n")
        result = default_output_path(str(tick_file), min_date=date(2024, 2, 1))
        assert result == str(tmp_path / "ticks_filtered_2024-02-01-2024-03-20.csv")

    def test_only_max_date(self, tmp_path):
        tick_file = tmp_path / "ticks.csv"
        tick_file.write_text("2024.01.10 08:00:00.000;1.1000;1.1001\n2024.03.20 16:00:00.000;1.1050;1.1051\n")
        result = default_output_path(str(tick_file), max_date=date(2024, 2, 28))
        assert result == str(tmp_path / "ticks_filtered_2024-01-10-2024-02-28.csv")

    def test_no_dates_reads_from_file(self, tmp_path):
        tick_file = tmp_path / "ticks.csv"
        tick_file.write_text("2024.01.10 08:00:00.000;1.1000;1.1001\n2024.03.20 16:00:00.000;1.1050;1.1051\n")
        result = default_output_path(str(tick_file))
        assert result == str(tmp_path / "ticks_filtered_2024-01-10-2024-03-20.csv")

    def test_no_dates_reads_from_file_with_header(self, tmp_path):
        tick_file = tmp_path / "ticks.csv"
        tick_file.write_text("DateTime;Bid;Ask\n2024.01.10 08:00:00.000;1.1000;1.1001\n2024.03.20 16:00:00.000;1.1050;1.1051\n")
        result = default_output_path(str(tick_file))
        assert result == str(tmp_path / "ticks_filtered_2024-01-10-2024-03-20.csv")


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_full_pipeline(self, tmp_path):
        ff_lines = [
            "2023-02-15 07:00:00+00:00,GBP,High Impact Expected,CPI y/y,3.0%,2.8%,2.7%",
            "2023-02-15 09:30:00+00:00,GBP,Medium Impact Expected,Final Manufacturing PMI,51.9,53.0,52.5",
            "2023-02-15 13:30:00+00:00,USD,Low Impact Expected,Unemployment Claims,329K,318K,317K",
        ]
        tick_lines = [
            "2023.02.15 06:58:00.000;1.20550;1.20569",  # before high event window
            "2023.02.15 06:59:00.000;1.20551;1.20570",  # in high event window
            "2023.02.15 07:00:00.000;1.20552;1.20571",  # at high event
            "2023.02.15 07:01:00.000;1.20553;1.20572",  # in high event window
            "2023.02.15 07:02:00.000;1.20554;1.20573",  # after high event window
            "2023.02.15 09:29:30.000;1.20555;1.20574",  # in med event window
            "2023.02.15 09:30:00.000;1.20556;1.20575",  # at med event
            "2023.02.15 09:31:00.000;1.20557;1.20576",  # in med event window
            "2023.02.15 13:30:00.000;1.20558;1.20577",  # at low event
        ]

        ff_file = _write_ff_file(str(tmp_path), ff_lines)
        tick_file = _write_tick_file(str(tmp_path), tick_lines)
        output_file = os.path.join(str(tmp_path), "out.csv")

        # High only
        events = parse_forex_factory_events(ff_file, include_high=True, include_med=False, include_low=False)
        intervals = build_intervals(events, 60, 60)
        filter_ticks(tick_file, output_file, intervals, show_progress=False)
        result = _read_output(output_file)
        assert len(result) == 3  # 06:59, 07:00, 07:01

        # High + Medium
        events = parse_forex_factory_events(ff_file, include_high=True, include_med=True, include_low=False)
        intervals = build_intervals(events, 60, 60)
        filter_ticks(tick_file, output_file, intervals, show_progress=False)
        result = _read_output(output_file)
        assert len(result) == 6  # 3 high + 3 med

        # All impacts
        events = parse_forex_factory_events(ff_file, include_high=True, include_med=True, include_low=True)
        intervals = build_intervals(events, 60, 60)
        filter_ticks(tick_file, output_file, intervals, show_progress=False)
        result = _read_output(output_file)
        assert len(result) == 7  # 3 high + 3 med + 1 low (at event)

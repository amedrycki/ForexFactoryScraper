import argparse
import csv
import os
import sys
import time
from datetime import date, datetime, timedelta


def parse_tick_timestamp(ts_str):
    return datetime.strptime(ts_str, "%Y.%m.%d %H:%M:%S.%f")


def _parse_tick_ts_fast(ts_str):
    # Fixed-position slicing for format: YYYY.MM.DD HH:MM:SS.mmm
    return (
        int(ts_str[0:4]),   # year
        int(ts_str[5:7]),   # month
        int(ts_str[8:10]),  # day
        int(ts_str[11:13]), # hour
        int(ts_str[14:16]), # minute
        int(ts_str[17:19]), # second
        int(ts_str[20:23]), # millisecond
    )


def _datetime_to_tuple(dt):
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond // 1000)


def parse_forex_factory_events(filepath, include_high, include_med, include_low):
    impact_set = set()
    if include_high:
        impact_set.add("High Impact Expected")
    if include_med:
        impact_set.add("Medium Impact Expected")
    if include_low:
        impact_set.add("Low Impact Expected")

    events = []
    with open(filepath, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 3:
                continue
            impact = row[2].strip()
            if impact in impact_set:
                dt_str = row[0].strip()
                # Strip timezone suffix (+00:00) to get naive UTC datetime
                if dt_str.endswith("+00:00"):
                    dt_str = dt_str[:-6]
                events.append(datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S"))

    events.sort()
    return events


def build_intervals(events, seconds_before, seconds_after):
    if not events:
        return []

    before = timedelta(seconds=seconds_before)
    after = timedelta(seconds=seconds_after)

    raw = [(ev - before, ev + after) for ev in events]
    raw.sort(key=lambda x: x[0])

    merged = [raw[0]]
    for start, end in raw[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    return merged


def _read_last_line(filepath):
    with open(filepath, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        if file_size == 0:
            return None
        # Read a chunk from the end (enough for one line)
        chunk_size = min(4096, file_size)
        f.seek(file_size - chunk_size)
        chunk = f.read(chunk_size)
        lines = chunk.split(b"\n")
        # Walk backwards to find last non-empty line
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip(b"\r")
            if stripped:
                return stripped.decode("utf-8")
        return None


def _format_progress_bar(percent, current_date_str, day_num, total_days, bar_width=40):
    GREEN = "\033[32m"
    RESET = "\033[0m"
    filled = int(bar_width * percent / 100)
    pct_str = f"{percent:.1f}%"
    pad = (bar_width - len(pct_str)) // 2
    pct_end = pad + len(pct_str)
    # Build colored bar: green only for '=' chars, percentage text stays uncolored
    in_green = False
    parts = []
    for i in range(bar_width):
        is_pct = pad <= i < pct_end
        if is_pct:
            if in_green:
                parts.append(RESET)
                in_green = False
            parts.append(pct_str[i - pad])
        elif i < filled:
            if not in_green:
                parts.append(GREEN)
                in_green = True
            parts.append("=")
        else:
            if in_green:
                parts.append(RESET)
                in_green = False
            parts.append(" ")
    if in_green:
        parts.append(RESET)
    colored_inner = "".join(parts)
    return f"\r[{colored_inner}] Processing {current_date_str} (day {day_num}/{total_days})..."


def filter_ticks(tick_file, output_file, intervals, show_progress=True):
    if not intervals:
        with open(output_file, "w", encoding="utf-8"):
            pass
        return

    # Gather progress info
    file_size = os.path.getsize(tick_file) if show_progress else 0
    first_date = None
    last_date = None
    total_days = 0

    if show_progress and file_size > 0:
        with open(tick_file, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
        if first_line:
            first_date = parse_tick_timestamp(first_line[:first_line.index(";")]).date()
        last_line = _read_last_line(tick_file)
        if last_line:
            last_date = parse_tick_timestamp(last_line[:last_line.index(";")]).date()
        if first_date and last_date:
            total_days = (last_date - first_date).days + 1

    # Convert intervals to comparable tuples for fast comparison
    fast_intervals = [(_datetime_to_tuple(s), _datetime_to_tuple(e)) for s, e in intervals]

    interval_idx = 0
    num_intervals = len(fast_intervals)
    prev_tick_tuple = None
    ticks_written = 0
    last_refresh = 0.0
    start_time = time.monotonic()

    with open(tick_file, "r", encoding="utf-8") as fin, \
         open(output_file, "w", encoding="utf-8", newline="") as fout:
        line_num = 0
        while True:
            line = fin.readline()
            if not line:
                break
            line_num += 1
            stripped = line.rstrip("\n\r")
            if not stripped:
                continue

            semi_pos = stripped.index(";")
            ts_str = stripped[:semi_pos]
            tick_tuple = _parse_tick_ts_fast(ts_str)

            if prev_tick_tuple is not None and tick_tuple < prev_tick_tuple:
                raise ValueError(
                    f"Tick data is not sorted: line {line_num} timestamp "
                    f"{ts_str} is before previous"
                )
            prev_tick_tuple = tick_tuple

            # Progress display (time-gated)
            if show_progress and file_size > 0:
                now = time.monotonic()
                if now - last_refresh >= 1.0:
                    last_refresh = now
                    byte_pos = fin.tell()
                    percent = min(byte_pos / file_size * 100, 100.0)
                    current_date_str = f"{tick_tuple[0]}-{tick_tuple[1]:02d}-{tick_tuple[2]:02d}"
                    if first_date:
                        day_num = (date(tick_tuple[0], tick_tuple[1], tick_tuple[2]) - first_date).days + 1
                    else:
                        day_num = 0
                    bar = _format_progress_bar(percent, current_date_str, day_num, total_days)
                    sys.stderr.write(bar)
                    sys.stderr.flush()

            # Advance past intervals that end before this tick
            while interval_idx < num_intervals and tick_tuple > fast_intervals[interval_idx][1]:
                interval_idx += 1

            if interval_idx >= num_intervals:
                break

            if tick_tuple >= fast_intervals[interval_idx][0]:
                fout.write(line)
                ticks_written += 1

    if show_progress:
        elapsed = time.monotonic() - start_time
        # Clear progress line and print summary
        sys.stderr.write("\r" + " " * 100 + "\r")
        sys.stderr.write(f"\033[1;32mDone.\033[0m {ticks_written} ticks written in {elapsed:.1f}s.\n")
        sys.stderr.flush()


def default_output_path(tick_data_file):
    root, ext = os.path.splitext(tick_data_file)
    return root + "_filtered" + ext


def main():
    parser = argparse.ArgumentParser(
        description="Filter tick data to keep only ticks around Forex Factory events."
    )
    parser.add_argument("-tick_data_file", required=True, help="Path to tick data file")
    parser.add_argument("-forex_factory_history", required=True, help="Path to forex factory history file")
    parser.add_argument("-seconds_before", type=int, default=60, help="Seconds before an event (default: 60)")
    parser.add_argument("-seconds_after", type=int, default=60, help="Seconds after an event (default: 60)")
    parser.add_argument("-include_high", choices=["yes", "no"], default="yes", help="Include high impact events (default: yes)")
    parser.add_argument("-include_med", choices=["yes", "no"], default="no", help="Include medium impact events (default: no)")
    parser.add_argument("-include_low", choices=["yes", "no"], default="no", help="Include low impact events (default: no)")
    parser.add_argument("-output_path", default=None, help="Output file path (default: <tick_data_file>_filtered.csv)")

    args = parser.parse_args()

    output_path = args.output_path if args.output_path else default_output_path(args.tick_data_file)

    events = parse_forex_factory_events(
        args.forex_factory_history,
        include_high=args.include_high == "yes",
        include_med=args.include_med == "yes",
        include_low=args.include_low == "yes",
    )

    intervals = build_intervals(events, args.seconds_before, args.seconds_after)

    filter_ticks(args.tick_data_file, output_path, intervals)


if __name__ == "__main__":
    main()

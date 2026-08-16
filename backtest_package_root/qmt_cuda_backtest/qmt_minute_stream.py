"""Candidate-universe, timestamp-aligned stream over QMT one-minute DAT files."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    from .minute_strategy_interface import MinuteBarSnapshot
    from .qmt_dat import read_minute_bars
except ImportError:
    from minute_strategy_interface import MinuteBarSnapshot
    from qmt_dat import read_minute_bars


@dataclass(frozen=True)
class StreamCoverage:
    requested_codes: int
    loaded_codes: int
    missing_codes: tuple[str, ...]
    first_timestamp: int
    last_timestamp: int
    minute_count: int


class QmtMinuteStream:
    def __init__(self, datadir: Path, codes: list[str], start: int, end: int) -> None:
        if len(set(codes)) != len(codes):
            raise ValueError("minute universe contains duplicate codes")
        self.datadir = Path(datadir)
        self.codes = codes
        start_epoch = int(time.mktime(time.strptime(f"{start} 00:00:00", "%Y%m%d %H:%M:%S")))
        end_epoch = int(time.mktime(time.strptime(f"{end} 23:59:59", "%Y%m%d %H:%M:%S")))
        self.series = []
        missing = []
        timestamp_parts = []
        for code in codes:
            symbol, market = code.upper().split(".", 1)
            if market not in {"SH", "SZ"}:
                raise ValueError(f"unsupported minute market suffix: {code}")
            path = self.datadir / market / "60" / f"{symbol}.DAT"
            if not path.exists() or path.stat().st_size == 0:
                missing.append(code)
                self.series.append(None)
                continue
            bars = read_minute_bars(
                path, start_epoch=start_epoch, end_epoch=end_epoch, mmap=True
            )
            if not len(bars):
                missing.append(code)
                self.series.append(None)
                continue
            self.series.append(bars)
            timestamp_parts.append(bars.timestamp.astype(np.int64, copy=False))
        if not timestamp_parts:
            raise ValueError("no QMT one-minute bars were loaded for the requested range")
        timestamps = np.unique(np.concatenate(timestamp_parts))
        keep = []
        for stamp in timestamps:
            local = time.localtime(int(stamp))
            hhmm = local.tm_hour * 100 + local.tm_min
            if 931 <= hhmm <= 1130 or 1301 <= hhmm <= 1500:
                keep.append(int(stamp))
        self.timestamps = np.asarray(keep, dtype=np.int64)
        if not len(self.timestamps):
            raise ValueError("no continuous-auction minute timestamps were found")
        self.coverage = StreamCoverage(
            len(codes), len(codes) - len(missing), tuple(missing),
            int(self.timestamps[0]), int(self.timestamps[-1]), len(self.timestamps),
        )

    def __iter__(self):
        pointers = np.zeros(len(self.codes), dtype=np.int64)
        n = len(self.codes)
        for raw_stamp in self.timestamps:
            stamp = int(raw_stamp)
            open_ = np.full(n, np.nan)
            high = np.full(n, np.nan)
            low = np.full(n, np.nan)
            close = np.full(n, np.nan)
            volume = np.full(n, np.nan)
            amount = np.full(n, np.nan)
            prev_close = np.full(n, np.nan)
            for j, bars in enumerate(self.series):
                if bars is None:
                    continue
                p = int(pointers[j])
                while p < len(bars) and int(bars.timestamp[p]) < stamp:
                    p += 1
                pointers[j] = p
                if p >= len(bars) or int(bars.timestamp[p]) != stamp:
                    continue
                open_[j] = bars.open[p]; high[j] = bars.high[p]
                low[j] = bars.low[p]; close[j] = bars.close[p]
                volume[j] = bars.volume[p]; amount[j] = bars.amount[p]
                prev_close[j] = bars.prev_close[p]
            local = time.localtime(stamp)
            date = local.tm_year * 10000 + local.tm_mon * 100 + local.tm_mday
            hhmm = local.tm_hour * 100 + local.tm_min
            for values in (open_, high, low, close, volume, amount, prev_close):
                values.flags.writeable = False
            yield MinuteBarSnapshot(
                stamp, date, hhmm, self.codes, open_, high, low, close,
                volume, amount, prev_close,
            )

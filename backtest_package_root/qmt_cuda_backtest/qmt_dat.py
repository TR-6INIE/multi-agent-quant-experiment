"""Fast, dependency-light reader for QMT 1-minute DAT cache files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


RECORD_SIZE = 64
PRICE_SCALE = np.float32(1000.0)

# The first two uint32 values are QMT metadata. The useful fields follow.
_RAW_DTYPE = np.dtype(
    [
        ("meta0", "<u4"), ("meta1", "<u4"),
        ("timestamp", "<u4"), ("open_i", "<u4"),
        ("high_i", "<u4"), ("low_i", "<u4"),
        ("close_i", "<u4"), ("reserved0", "<u4"),
        ("volume", "<u4"), ("reserved1", "<u4"),
        ("amount", "<u4"), ("reserved2", "<u4"),
        ("reserved3", "<u4"), ("reserved4", "<u4"),
        ("reserved5", "<u4"), ("prev_close_i", "<u4"),
    ]
)


@dataclass(frozen=True)
class MinuteBars:
    timestamp: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    amount: np.ndarray
    prev_close: np.ndarray

    def __len__(self) -> int:
        return len(self.timestamp)


def front_ratio_adjust(bars: MinuteBars) -> MinuteBars:
    """Build a continuous front-ratio series from QMT's daily prev-close.

    On an ex-right day QMT stores the adjusted reference close in the first
    minute's ``prev_close``. Its ratio to the preceding day's final raw close
    is the backward multiplier. This removes mechanical dividend/split gaps
    without requiring the running QMT quote service.
    """
    if len(bars) < 2:
        return bars
    local_days = bars.timestamp.astype("datetime64[s]").astype("datetime64[D]")
    starts = np.r_[0, np.flatnonzero(local_days[1:] != local_days[:-1]) + 1]
    ends = np.r_[starts[1:], len(bars)]
    daily_ratio = np.ones(len(starts), dtype=np.float64)
    for day_i in range(1, len(starts)):
        old_close = float(bars.close[ends[day_i - 1] - 1])
        reference = float(bars.prev_close[starts[day_i]])
        ratio = reference / old_close if old_close > 0 and reference > 0 else 1.0
        # Ignore ordinary rounding/noise and corrupt reference fields.
        if 0.25 < ratio < 4.0 and abs(ratio - 1.0) > 0.001:
            daily_ratio[day_i] = ratio
    factors_by_day = np.ones(len(starts), dtype=np.float64)
    running = 1.0
    for day_i in range(len(starts) - 1, 0, -1):
        running *= daily_ratio[day_i]
        factors_by_day[day_i - 1] = running
    factors = np.repeat(factors_by_day, ends - starts).astype(np.float32)
    return MinuteBars(
        timestamp=bars.timestamp,
        open=bars.open * factors, high=bars.high * factors,
        low=bars.low * factors, close=bars.close * factors,
        volume=bars.volume, amount=bars.amount,
        prev_close=bars.prev_close * factors,
    )


def read_raw(path: str | Path, mmap: bool = True) -> np.ndarray:
    """Read QMT records, ignoring any trailing bytes from an in-progress write."""
    path = Path(path)
    count = path.stat().st_size // RECORD_SIZE
    if mmap:
        return np.memmap(path, dtype=_RAW_DTYPE, mode="r", shape=(count,))
    return np.fromfile(path, dtype=_RAW_DTYPE, count=count)


def read_minute_bars(
    path: str | Path,
    start_epoch: int | None = None,
    end_epoch: int | None = None,
    mmap: bool = True,
) -> MinuteBars:
    """Return compact arrays; prices are float32 and amounts remain uint32."""
    raw = read_raw(path, mmap=mmap)
    mask = raw["timestamp"] > 0
    if start_epoch is not None:
        mask &= raw["timestamp"] >= start_epoch
    if end_epoch is not None:
        mask &= raw["timestamp"] <= end_epoch

    def price(field: str) -> np.ndarray:
        return np.asarray(raw[field][mask], dtype=np.float32) / PRICE_SCALE

    return MinuteBars(
        timestamp=np.asarray(raw["timestamp"][mask], dtype=np.uint32),
        open=price("open_i"), high=price("high_i"), low=price("low_i"),
        close=price("close_i"),
        volume=np.asarray(raw["volume"][mask], dtype=np.uint32),
        amount=np.asarray(raw["amount"][mask], dtype=np.uint32),
        prev_close=price("prev_close_i"),
    )


def dat_path(datadir: str | Path, code: str) -> Path:
    symbol, market = code.upper().split(".", 1)
    if market not in {"SH", "SZ"}:
        raise ValueError(f"unsupported market suffix: {code}")
    return Path(datadir) / market / "60" / f"{symbol}.DAT"


def discover(datadir: str | Path):
    root = Path(datadir)
    for market in ("SH", "SZ"):
        for path in sorted((root / market / "60").glob("*.DAT")):
            yield f"{path.stem}.{market}", path

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple

import numpy as np


SUPPORTED_PERIODS = {'1m': 60, '5m': 300}


@dataclass(frozen=True)
class StrategyDataSpec:
    signal_period: str
    signal_times: Tuple[str, ...]
    execution_period: str
    execution_time: str
    intraday_lookback_sessions: int
    fundamental_fields: Tuple[str, ...]


def strategy_fundamental_fields(
    strategy: Any, available_fields: Sequence[str], max_fields: int
) -> Tuple[str, ...]:
    raw = getattr(strategy, 'data_spec', None) or {}
    if not isinstance(raw, dict):
        raise TypeError('strategy.data_spec must be a dict')
    requested = raw.get('fundamental_fields', ())
    if requested is None:
        requested = ()
    if not isinstance(requested, (list, tuple)):
        raise TypeError('fundamental_fields must be a list or tuple')
    if len(requested) > int(max_fields):
        raise ValueError(
            'fundamental_fields may contain at most %d fields' % int(max_fields)
        )
    lookup = {str(name).casefold(): str(name) for name in available_fields}
    canonical = []
    for raw_name in requested:
        name = str(raw_name).strip()
        if not name:
            raise ValueError('fundamental_fields must not contain empty names')
        canonical_name = lookup.get(name.casefold())
        if canonical_name is None:
            raise ValueError('unsupported fundamental field: %s' % name)
        canonical.append(canonical_name)
    if len(canonical) != len(set(canonical)):
        raise ValueError('fundamental_fields must be unique')
    return tuple(canonical)


def _time_text(value: Any) -> str:
    text = str(value or '').strip()
    if len(text) == 4 and text.isdigit():
        text = text[:2] + ':' + text[2:]
    parts = text.split(':')
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError('time must use HH:MM: %s' % value)
    hour, minute = int(parts[0]), int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError('invalid time: %s' % value)
    return '%02d:%02d' % (hour, minute)


def _minute_of_day(value: str) -> int:
    hour, minute = value.split(':')
    return int(hour) * 60 + int(minute)


def _valid_bar_minutes(period: str) -> set[int]:
    step = 1 if period == '1m' else 5
    morning_start = 9 * 60 + 30 + step
    afternoon_start = 13 * 60 + step
    return set(range(morning_start, 11 * 60 + 30 + 1, step)) | set(
        range(afternoon_start, 15 * 60 + 1, step)
    )


def strategy_data_spec(strategy: Any, limits: Dict[str, Any]) -> StrategyDataSpec:
    raw = getattr(strategy, 'data_spec', None) or {}
    if not isinstance(raw, dict):
        raise TypeError('strategy.data_spec must be a dict')
    signal_period = str(raw.get('signal_period', '1d')).strip().lower()
    execution_period = str(raw.get('execution_period', '5m')).strip().lower()
    if execution_period not in SUPPORTED_PERIODS:
        raise ValueError('execution_period must be 5m or 1m')
    execution_time = _time_text(raw.get('execution_time', '09:35'))
    if _minute_of_day(execution_time) not in _valid_bar_minutes(execution_period):
        raise ValueError(
            'execution_time is not a valid completed %s bar: %s'
            % (execution_period, execution_time)
        )
    signal_times = tuple(_time_text(item) for item in raw.get('signal_times', ()))
    if signal_period == '1d':
        if signal_times:
            raise ValueError('1d strategies must not declare intraday signal_times')
        lookback = 0
    else:
        if signal_period not in SUPPORTED_PERIODS:
            raise ValueError('signal_period must be 1d, 5m or 1m')
        if execution_period != signal_period:
            raise ValueError(
                'intraday signal and execution periods must currently match'
            )
        if not signal_times:
            raise ValueError('intraday strategies need at least one signal_time')
        invalid_times = [
            item for item in signal_times
            if _minute_of_day(item) not in _valid_bar_minutes(signal_period)
        ]
        if invalid_times:
            raise ValueError(
                'signal_times contain invalid completed %s bars: %s'
                % (signal_period, ', '.join(invalid_times))
            )
        max_times = int(limits.get('max_signal_times', 4))
        if len(signal_times) > max_times or len(set(signal_times)) != len(signal_times):
            raise ValueError('signal_times must be unique and no more than %d' % max_times)
        lookback = int(raw.get('intraday_lookback_sessions', 20))
        max_lookback = int(limits.get('max_intraday_lookback_sessions', 80))
        if lookback < 1 or lookback > max_lookback:
            raise ValueError(
                'intraday_lookback_sessions must be within 1..%d' % max_lookback
            )
        if max(_minute_of_day(item) for item in signal_times) >= _minute_of_day(execution_time):
            raise ValueError(
                'execution_time must be later than every completed signal bar'
            )
    fundamental_fields = strategy_fundamental_fields(
        strategy,
        limits.get('available_fundamental_fields') or (),
        int(limits.get('max_fundamental_fields', 0)),
    )
    return StrategyDataSpec(
        signal_period=signal_period,
        signal_times=signal_times,
        execution_period=execution_period,
        execution_time=execution_time,
        intraday_lookback_sessions=lookback,
        fundamental_fields=fundamental_fields,
    )


def spec_to_dict(spec: StrategyDataSpec) -> Dict[str, Any]:
    return {
        'signal_period': spec.signal_period,
        'signal_times': list(spec.signal_times),
        'execution_period': spec.execution_period,
        'execution_time': spec.execution_time,
        'intraday_lookback_sessions': spec.intraday_lookback_sessions,
        'fundamental_fields': list(spec.fundamental_fields),
        'rebalance_frequency': 'at_most_once_per_trading_day',
    }


def _date_ordinal(value: int) -> int:
    text = str(int(value))
    return (date(int(text[:4]), int(text[4:6]), int(text[6:8])) - date(1970, 1, 1)).days


def _source_manifest(datadir: Path, codes: Sequence[str], seconds: int) -> Dict[str, Any]:
    digest = hashlib.sha256()
    found = 0
    missing = 0
    for code in codes:
        symbol, market = str(code).split('.', 1)
        path = datadir / market / str(seconds) / (symbol + '.DAT')
        if path.is_file():
            stat = path.stat()
            record = '%s|%d|%d' % (path, stat.st_size, stat.st_mtime_ns)
            found += 1
        else:
            record = '%s|MISSING' % path
            missing += 1
        digest.update(record.encode('utf-8'))
        digest.update(b'\n')
    return {
        'metadata_sha256': digest.hexdigest(),
        'file_count': found,
        'missing_file_count': missing,
        'note': 'Hash covers path, size and mtime_ns; it is not a content hash.',
    }


def source_manifest(
    datadir: Path, codes: Sequence[str], period: str
) -> Dict[str, Any]:
    if period not in SUPPORTED_PERIODS:
        raise ValueError('unsupported source-manifest period: %s' % period)
    return _source_manifest(datadir, codes, SUPPORTED_PERIODS[period])


def load_or_build_snapshots(
    package_root: Path,
    datadir: Path,
    cache_dir: Path,
    calendar: np.ndarray,
    codes: Sequence[str],
    period: str,
    times: Sequence[str],
) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any], Path]:
    if period not in SUPPORTED_PERIODS:
        raise ValueError('unsupported snapshot period: %s' % period)
    normalized_times = tuple(sorted({_time_text(item) for item in times}, key=_minute_of_day))
    if not normalized_times:
        raise ValueError('at least one snapshot time is required')
    seconds = SUPPORTED_PERIODS[period]
    manifest = _source_manifest(datadir, codes, seconds)
    identity = json.dumps({
        'datadir': str(datadir.resolve()),
        'period': period,
        'times': normalized_times,
        'calendar_start': int(calendar[0]),
        'calendar_end': int(calendar[-1]),
        'codes_sha256': hashlib.sha256(
            '\n'.join(str(code) for code in codes).encode('utf-8')
        ).hexdigest(),
        'source_metadata_sha256': manifest['metadata_sha256'],
    }, ensure_ascii=False, sort_keys=True)
    key = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / ('snapshots_%s_%s.npz' % (period, key))
    if path.exists():
        with np.load(path, allow_pickle=False) as saved:
            close = np.array(saved['close'], copy=True)
            amount = np.array(saved['amount'], copy=True)
            metadata = json.loads(str(saved['metadata_json'].item()))
        return close, amount, metadata, path

    import sys
    sys.path.insert(0, str(package_root))
    from qmt_cuda_backtest.qmt_dat import front_ratio_adjust, read_minute_bars

    shape = (len(calendar), len(normalized_times), len(codes))
    close = np.full(shape, np.nan, dtype=np.float32)
    amount = np.full(shape, np.nan, dtype=np.float32)
    day_lookup = {_date_ordinal(int(value)): i for i, value in enumerate(calendar)}
    time_lookup = {_minute_of_day(value): i for i, value in enumerate(normalized_times)}
    for j, raw_code in enumerate(codes):
        code = str(raw_code)
        symbol, market = code.split('.', 1)
        source = datadir / market / str(seconds) / (symbol + '.DAT')
        if not source.is_file() or source.stat().st_size < 64:
            continue
        bars = front_ratio_adjust(read_minute_bars(source))
        local_seconds = bars.timestamp.astype(np.int64) + 8 * 3600
        ordinals = local_seconds // 86400
        minutes = (local_seconds % 86400) // 60
        keep = np.isin(minutes, np.fromiter(time_lookup, dtype=np.int64))
        selected = np.flatnonzero(keep)
        for k in selected:
            day_i = day_lookup.get(int(ordinals[k]))
            time_i = time_lookup.get(int(minutes[k]))
            if day_i is None or time_i is None:
                continue
            price = float(bars.close[k])
            if np.isfinite(price) and price > 0:
                close[day_i, time_i, j] = price
            amount[day_i, time_i, j] = float(bars.amount[k])

    metadata = {
        'identity': json.loads(identity),
        'source_manifest': manifest,
        'adjustment': 'front_ratio_adjust_current_source_anchor',
        'timezone': 'Asia/Shanghai (+08:00)',
        'fields': ['close', 'amount'],
    }
    temp = path.with_suffix('.tmp.npz')
    np.savez(
        temp,
        close=close,
        amount=amount,
        metadata_json=np.asarray(json.dumps(metadata, ensure_ascii=False)),
    )
    os.replace(temp, path)
    return close, amount, metadata, path

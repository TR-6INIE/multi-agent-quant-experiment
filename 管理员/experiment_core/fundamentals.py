from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence, Tuple

import numpy as np


@dataclass(frozen=True)
class FundamentalSnapshot:
    fields: Tuple[str, ...]
    values: np.ndarray
    report_dates: np.ndarray
    available_dates: np.ndarray
    cutoff_date: int


def empty_fundamental_snapshot(code_count: int) -> FundamentalSnapshot:
    shape = (int(code_count), 0)
    values = np.empty(shape, dtype=np.float64)
    dates = np.empty(shape, dtype=np.int32)
    values.setflags(write=False)
    dates.setflags(write=False)
    return FundamentalSnapshot((), values, dates, dates, 0)


class PointInTimeFundamentalProvider:
    """Incremental point-in-time financial snapshots for candidate strategies.

    Candidate code never receives the event cache.  It receives only the latest
    accepted value for each requested field and stock as of the trading session
    immediately preceding the current strategy date.
    """

    def __init__(
        self,
        calendar: np.ndarray,
        target_codes: Sequence[str],
        data: Any,
        requested_fields: Sequence[str],
    ) -> None:
        self.calendar = np.asarray(calendar, dtype=np.int64)
        if self.calendar.ndim != 1 or (
            len(self.calendar) > 1
            and np.any(self.calendar[1:] <= self.calendar[:-1])
        ):
            raise ValueError('fundamental calendar must be strictly increasing')

        available_lookup = {
            str(name).casefold(): str(name) for name in data.fields
        }
        canonical = []
        for raw in requested_fields:
            key = str(raw).strip().casefold()
            if key not in available_lookup:
                raise KeyError('unknown fundamental field: %s' % raw)
            canonical.append(available_lookup[key])
        if len(canonical) != len(set(canonical)):
            raise ValueError('fundamental_fields must not contain duplicates')
        self.fields = tuple(canonical)

        n_codes, n_fields = len(target_codes), len(self.fields)
        self.current_values = np.full((n_codes, n_fields), np.nan, dtype=np.float64)
        self.current_reports = np.zeros((n_codes, n_fields), dtype=np.int32)
        self.current_announces = np.zeros((n_codes, n_fields), dtype=np.int32)
        self.current_available = np.zeros((n_codes, n_fields), dtype=np.int32)
        self._last_cutoff = -1
        self._pointer = 0

        if not self.fields:
            self._event_indices = np.empty(0, dtype=np.int64)
            self._mapped_code = np.empty(0, dtype=np.int32)
            self._mapped_field = np.empty(0, dtype=np.int16)
            self._data = data
            return

        target_lookup = {
            str(code).strip().upper(): i for i, code in enumerate(target_codes)
        }
        source_to_target = np.asarray([
            target_lookup.get(str(code).strip().upper(), -1)
            for code in data.codes
        ], dtype=np.int32)
        source_field_lookup = {
            str(name).casefold(): i for i, name in enumerate(data.fields)
        }
        requested_source_fields = np.asarray([
            source_field_lookup[name.casefold()] for name in self.fields
        ], dtype=np.int16)
        source_to_requested = np.full(len(data.fields), -1, dtype=np.int16)
        for requested_i, source_i in enumerate(requested_source_fields):
            source_to_requested[int(source_i)] = requested_i

        mapped_code = source_to_target[np.asarray(data.event_code, dtype=np.int32)]
        mapped_field = source_to_requested[
            np.asarray(data.event_field, dtype=np.int32)
        ]
        keep = (mapped_code >= 0) & (mapped_field >= 0)
        self._event_indices = np.flatnonzero(keep)
        self._mapped_code = mapped_code
        self._mapped_field = mapped_field
        self._data = data

    def _advance(self, cutoff_date: int) -> None:
        cutoff = int(cutoff_date)
        if cutoff < self._last_cutoff:
            raise ValueError('fundamental snapshots must be requested chronologically')
        while self._pointer < len(self._event_indices):
            event_i = int(self._event_indices[self._pointer])
            available = int(self._data.available_date[event_i])
            if available > cutoff:
                break
            code_i = int(self._mapped_code[event_i])
            field_i = int(self._mapped_field[event_i])
            report = int(self._data.report_date[event_i])
            announce = int(self._data.announce_date[event_i])
            if (
                report > self.current_reports[code_i, field_i]
                or (
                    report == self.current_reports[code_i, field_i]
                    and announce >= self.current_announces[code_i, field_i]
                )
            ):
                self.current_values[code_i, field_i] = float(
                    self._data.value[event_i]
                )
                self.current_reports[code_i, field_i] = report
                self.current_announces[code_i, field_i] = announce
                self.current_available[code_i, field_i] = available
            self._pointer += 1
        self._last_cutoff = cutoff

    def snapshot_before(self, strategy_date: int) -> FundamentalSnapshot:
        stop = int(np.searchsorted(self.calendar, int(strategy_date), side='left'))
        cutoff = int(self.calendar[stop - 1]) if stop > 0 else 0
        self._advance(cutoff)
        values = np.array(self.current_values, copy=True)
        reports = np.array(self.current_reports, copy=True)
        available = np.array(self.current_available, copy=True)
        values.setflags(write=False)
        reports.setflags(write=False)
        available.setflags(write=False)
        return FundamentalSnapshot(
            fields=self.fields,
            values=values,
            report_dates=reports,
            available_dates=available,
            cutoff_date=cutoff,
        )

    def provenance(self) -> dict:
        return {
            'fields': list(self.fields),
            'mapped_event_count': int(len(self._event_indices)),
            'cutoff_rule': 'latest announcement available by previous trading session',
            'final_coverage': {
                field: int(np.sum(np.isfinite(self.current_values[:, i])))
                for i, field in enumerate(self.fields)
            },
        }

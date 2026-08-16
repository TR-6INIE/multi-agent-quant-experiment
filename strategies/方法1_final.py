# -*- coding: utf-8 -*-
"""Candidate strategy for the local backtest harness.

Basic work: implement the approved dynamic KEEP_BUFFER mechanism based on
market volatility term structure. The performance-work proposal is only
documented in ENGINEER_NOTES and is not merged into this running implementation.
"""

from __future__ import annotations

import math
import numpy as np

from experiment_core.local_backtest import SafeStrategyDecision


TOP_K = 5
MAX_PER_INDUSTRY = 1
TREND_MA = 20
EXIT_BREADTH = 0.50
MIN_AVG_AMOUNT = 50_000_000.0
BREADTH_SMOOTH_DAYS = 3
PANIC_BREADTH = 0.40

VOL_SHORT_WINDOW = 5
VOL_LONG_WINDOW = 20
VOL_SPIKE_RATIO = 1.2
BASE_KEEP_BUFFER = 2.0
HIGH_VOL_KEEP_BUFFER = 3.0
MIN_VOL_UNIVERSE = 10


def _column_mean(values):
    finite = np.isfinite(values)
    count = np.sum(finite, axis=0)
    total = np.nansum(values, axis=0)
    return np.divide(
        total, count,
        out=np.full(values.shape[1], np.nan, dtype=float),
        where=count > 0,
    )


def _percentile(values, codes):
    result = np.full(len(values), np.nan, dtype=float)
    valid = np.flatnonzero(np.isfinite(values))
    if len(valid):
        order = sorted(valid.tolist(), key=lambda j: (values[j], codes[j]))
        result[order] = (np.arange(len(order), dtype=float) + 1.0) / len(order)
    return result


def _choose_desired(ranked, held, industries, keep_buffer):
    rank_map = {int(j): rank for rank, j in enumerate(ranked)}
    keep_limit = max(TOP_K, int(math.ceil(TOP_K * keep_buffer)))
    counts = {}
    desired = []
    for j in sorted(held, key=lambda value: rank_map.get(value, 10 ** 9)):
        industry = industries[j]
        if (rank_map.get(j, 10 ** 9) < keep_limit and
                counts.get(industry, 0) < MAX_PER_INDUSTRY):
            desired.append(j)
            counts[industry] = counts.get(industry, 0) + 1
    for raw_j in ranked:
        j = int(raw_j)
        industry = industries[j]
        if j not in desired and counts.get(industry, 0) < MAX_PER_INDUSTRY:
            desired.append(j)
            counts[industry] = counts.get(industry, 0) + 1
        if len(desired) >= TOP_K:
            break
    return tuple(desired[:TOP_K])


def _choose_kept(ranked, held, industries, keep_buffer):
    rank_map = {int(j): rank for rank, j in enumerate(ranked)}
    keep_limit = max(TOP_K, int(math.ceil(TOP_K * keep_buffer)))
    counts = {}
    desired = []
    for j in sorted(held, key=lambda value: rank_map.get(value, 10 ** 9)):
        industry = industries[j]
        if (rank_map.get(j, 10 ** 9) < keep_limit and
                counts.get(industry, 0) < MAX_PER_INDUSTRY):
            desired.append(j)
            counts[industry] = counts.get(industry, 0) + 1
    return tuple(desired[:TOP_K])


class Strategy:
    name = 'broad_industry_neutral_momentum_local_v1'
    engine_mode = 'daily'
    cooldown_days = 0
    data_spec = {
        'signal_period': '1d',
        'signal_times': (),
        'execution_period': '5m',
        'execution_time': '09:35',
    }

    def ready(self, context):
        return len(context.calendar) >= 75

    def decide(self, context):
        closes = context.closes
        amounts = context.amounts
        codes = context.codes
        industries = context.industries
        held = set(context.selected_held)
        signal_i = len(closes) - 1
        scores = np.full(len(codes), np.nan, dtype=float)
        if signal_i < 60:
            return SafeStrategyDecision(tuple(), scores, 0.0, TOP_K)

        recent = closes[max(0, signal_i - 74):signal_i + 1]
        history_count = np.sum(np.isfinite(recent), axis=0)
        ma20 = _column_mean(closes[signal_i - 19:signal_i + 1])
        trend = _column_mean(closes[signal_i - TREND_MA + 1:signal_i + 1])

        # Breadth smoothing and single-day breadth extraction.
        breadths = []
        for offset in range(BREADTH_SMOOTH_DAYS):
            t = signal_i - offset
            if t < 59:
                break
            start_idx = max(0, t - 59)
            hist_count_t = np.sum(np.isfinite(closes[start_idx:t + 1]), axis=0)
            ma20_t = _column_mean(closes[t - 19:t + 1])
            valid_t = (
                (hist_count_t >= 60) & np.isfinite(closes[t]) & np.isfinite(ma20_t)
            )
            if np.any(valid_t):
                b = float(np.mean(closes[t, valid_t] > ma20_t[valid_t]))
            else:
                b = 0.0
            breadths.append(b)

        breadth = float(np.mean(breadths)) if breadths else 0.0
        breadth_1d = breadths[0] if breadths else 0.0

        amount_window = amounts[signal_i - 5:signal_i + 1]
        amount_count = np.sum(np.isfinite(amount_window), axis=0)
        liquid = np.divide(
            np.nansum(amount_window, axis=0), amount_count,
            out=np.zeros(len(codes), dtype=float), where=amount_count > 0,
        )
        eligible = (
            (history_count >= max(61, TREND_MA)) &
            np.isfinite(closes[signal_i]) & np.isfinite(trend) &
            (closes[signal_i] > trend) & (liquid >= MIN_AVG_AMOUNT)
        )

        # Dynamic KEEP_BUFFER from market volatility term structure.
        current_keep_buffer = BASE_KEEP_BUFFER
        if np.sum(eligible) >= MIN_VOL_UNIVERSE:
            returns = np.diff(
                closes[signal_i - VOL_LONG_WINDOW:signal_i + 1], axis=0
            ) / closes[signal_i - VOL_LONG_WINDOW:signal_i]
            returns[~np.isfinite(returns)] = np.nan

            valid_ret = np.isfinite(returns)
            short_ret = returns[-VOL_SHORT_WINDOW:]
            short_valid = valid_ret[-VOL_SHORT_WINDOW:]

            vol_short = np.nan
            if np.any(short_valid):
                with np.errstate(invalid='ignore'):
                    per_stock_short_std = np.nanstd(short_ret, axis=0)
                finite_short_std = per_stock_short_std[
                    np.isfinite(per_stock_short_std)
                ]
                if finite_short_std.size > 0:
                    vol_short = float(np.mean(finite_short_std))

            vol_long = np.nan
            if np.any(valid_ret):
                with np.errstate(invalid='ignore'):
                    per_stock_long_std = np.nanstd(returns, axis=0)
                finite_long_std = per_stock_long_std[
                    np.isfinite(per_stock_long_std)
                ]
                if finite_long_std.size > 0:
                    vol_long = float(np.mean(finite_long_std))

            if (
                np.isfinite(vol_short) and
                np.isfinite(vol_long) and
                vol_long > 0 and
                vol_short > VOL_SPIKE_RATIO * vol_long
            ):
                current_keep_buffer = HIGH_VOL_KEEP_BUFFER

        components = []
        for lookback in (20, 40, 60):
            old = closes[signal_i - lookback]
            raw = closes[signal_i] / old - 1.0
            raw[~eligible | ~np.isfinite(old) | (old <= 0)] = np.nan
            components.append(_percentile(raw, codes))
        scores = components[0] * 0.40 + components[1] * 0.35 + components[2] * 0.25
        scores[~eligible] = np.nan
        valid = np.flatnonzero(np.isfinite(scores))
        ranked = np.asarray(
            sorted(valid.tolist(), key=lambda j: (scores[j], codes[j]), reverse=True),
            dtype=np.int64,
        )

        if breadth < EXIT_BREADTH or breadth_1d < PANIC_BREADTH:
            desired = tuple()
        elif breadth >= EXIT_BREADTH and breadth_1d >= breadth:
            desired = _choose_desired(
                ranked, held, industries, current_keep_buffer
            )
        else:
            desired = _choose_kept(
                ranked, held, industries, current_keep_buffer
            )
        return SafeStrategyDecision(desired, scores, breadth, TOP_K)


def create_strategy():
    return Strategy()

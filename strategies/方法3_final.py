# -*- coding: utf-8 -*-
"""Baseline strategy for the automated experiment.

Only this strategy module may evolve.  Market data loading, execution, fees,
cash, positions and output accounting are owned by the frozen harness.
"""

from __future__ import annotations

import math
import numpy as np

from experiment_core.local_backtest import SafeStrategyDecision


TOP_K = 5
KEEP_BUFFER = 3.0
MAX_PER_INDUSTRY = 1
TREND_MA = 20
EXIT_BREADTH = 0.50
ENTRY_BREADTH = 0.58
MIN_AVG_AMOUNT = 50_000_000.0
BREADTH_MA_WINDOW = 10
BREADTH_MA_BUFFER = 0.05


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


def _choose_desired(ranked, held, industries):
    rank_map = {int(j): rank for rank, j in enumerate(ranked)}
    keep_limit = max(TOP_K, int(math.ceil(TOP_K * KEEP_BUFFER)))
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


def _choose_kept(ranked, held, industries):
    rank_map = {int(j): rank for rank, j in enumerate(ranked)}
    keep_limit = max(TOP_K, int(math.ceil(TOP_K * KEEP_BUFFER)))
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
    engine_mode = 'daily'
    name = 'broad_industry_neutral_momentum_local_v1'
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
            scores = np.where(np.isfinite(scores), scores, -1.0)
            return SafeStrategyDecision(tuple(), scores, 0.0, TOP_K)

        recent = closes[max(0, signal_i - 74):signal_i + 1]
        history_count = np.sum(np.isfinite(recent), axis=0)
        ma20 = _column_mean(closes[signal_i - 19:signal_i + 1])
        trend = _column_mean(closes[signal_i - TREND_MA + 1:signal_i + 1])
        breadth_valid = (
            (history_count >= 60) & np.isfinite(closes[signal_i]) &
            np.isfinite(ma20)
        )
        breadth = (
            float(np.mean(closes[signal_i, breadth_valid] > ma20[breadth_valid]))
            if np.any(breadth_valid) else 0.0
        )
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

        # Begin approved incremental change: asymmetric regime-aware breadth filter.
        # Compute the 10-day mean of historical daily breadth values, using
        # only information available as of each historical day.
        breadth_values = []
        start_idx = max(19, signal_i - BREADTH_MA_WINDOW + 1)
        for i in range(start_idx, signal_i + 1):
            ma20_i = _column_mean(closes[i - 19:i + 1])
            hist_window = closes[max(0, i - 74):i + 1]
            history_count_i = np.sum(np.isfinite(hist_window), axis=0)
            valid_i = (
                (history_count_i >= 60) &
                np.isfinite(closes[i]) &
                np.isfinite(ma20_i)
            )
            if np.any(valid_i):
                breadth_i = float(np.mean(closes[i, valid_i] > ma20_i[valid_i]))
            else:
                breadth_i = np.nan
            breadth_values.append(breadth_i)

        breadth_values = np.asarray(breadth_values, dtype=float)
        finite_breadths = breadth_values[np.isfinite(breadth_values)]
        if finite_breadths.size:
            breadth_ma = float(np.mean(finite_breadths))
        else:
            breadth_ma = breadth

        is_bear_regime = (
            (breadth < EXIT_BREADTH) or
            (breadth < (breadth_ma - BREADTH_MA_BUFFER))
        )
        is_bull_regime = (breadth >= ENTRY_BREADTH) and (breadth > breadth_ma)

        if is_bear_regime:
            desired = tuple()
        elif is_bull_regime:
            desired = _choose_desired(ranked, held, industries)
        else:
            desired = _choose_kept(ranked, held, industries)

        # Audit-only score backfill: selection and regime logic use the original
        # finite scores above.  Replacing remaining NaN values here prevents
        # trades.csv from storing empty score fields for ineligible sell names.
        output_scores = np.where(np.isfinite(scores), scores, -1.0)
        return SafeStrategyDecision(desired, output_scores, breadth, TOP_K)


def create_strategy():
    return Strategy()

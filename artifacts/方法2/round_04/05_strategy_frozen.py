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
KEEP_BUFFER = 2.0
MAX_PER_INDUSTRY = 1
TREND_MA = 20
EXIT_BREADTH = 0.50
ENTRY_BREADTH = 0.58
MIN_AVG_AMOUNT = 50_000_000.0
VOL_EPS = 1e-6


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
            raw_ret = closes[signal_i] / old - 1.0

            period_closes = closes[signal_i - lookback:signal_i + 1]

            # Maximum Drawdown (Max DD) as one component of tail risk.
            # np.fmax.accumulate safely ignores NaNs (e.g., from suspended stocks).
            cummax = np.fmax.accumulate(period_closes, axis=0)
            safe_cummax = np.where(cummax > 0, cummax, np.nan)
            drawdowns = (period_closes - cummax) / safe_cummax
            with np.errstate(invalid='ignore'):
                max_dd = np.nanmin(drawdowns, axis=0)  # Negative values

            # Calculate recovery ratio to adjust the drawdown penalty.
            # Trough is the minimum price in the window.
            trough_price = np.nanmin(period_closes, axis=0)
            current_price = period_closes[-1]
            peak_price = cummax[-1]  # The last element of cummax is the global peak in the window.

            denominator = peak_price - trough_price
            # If peak == trough (no volatility), recovery_ratio is 1.0
            # (fully recovered / no drawdown).
            with np.errstate(invalid='ignore'):
                recovery_ratio = np.where(
                    denominator > VOL_EPS,
                    (current_price - trough_price) / denominator,
                    1.0
                )

            # Adjusted risk: penalizes stocks still at their trough,
            # forgives stocks that have already recovered.
            adjusted_risk = np.abs(max_dd) * (1.0 - recovery_ratio)

            safe_risk = np.where(adjusted_risk > VOL_EPS, adjusted_risk, VOL_EPS)
            safe_risk = np.where(np.isfinite(adjusted_risk), safe_risk, np.nan)

            raw = raw_ret / safe_risk
            raw[~eligible | ~np.isfinite(old) | (old <= 0)] = np.nan
            components.append(_percentile(raw, codes))

        scores = components[0] * 0.40 + components[1] * 0.35 + components[2] * 0.25
        scores[~eligible] = np.nan
        valid = np.flatnonzero(np.isfinite(scores))
        ranked = np.asarray(
            sorted(valid.tolist(), key=lambda j: (scores[j], codes[j]), reverse=True),
            dtype=np.int64,
        )
        if breadth < EXIT_BREADTH:
            desired = tuple()
        elif breadth >= ENTRY_BREADTH:
            desired = _choose_desired(ranked, held, industries)
        else:
            desired = _choose_kept(ranked, held, industries)
        return SafeStrategyDecision(desired, scores, breadth, TOP_K)


def create_strategy():
    return Strategy()

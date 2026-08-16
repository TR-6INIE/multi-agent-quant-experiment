"""Reference candidate-pool strategy: intraday VWAP reclaim, next-bar execution."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

try:
    from .minute_strategy_interface import (
        MinuteStrategyContext,
        close_position,
        target_value,
    )
except ImportError:
    from minute_strategy_interface import MinuteStrategyContext, close_position, target_value


@dataclass(frozen=True)
class VwapReclaimConfig:
    target_value: float = 50_000.0
    max_positions: int = 5
    buy_start_hhmm: int = 945
    buy_end_hhmm: int = 1450
    reclaim_buffer: float = 0.0005
    exit_vwap_ratio: float = 0.995


class MinuteVwapReclaimStrategy:
    name = "candidate_minute_vwap_reclaim_v1"

    def __init__(self, codes: list[str], config: VwapReclaimConfig) -> None:
        self.codes = codes
        self.code_set = frozenset(codes)
        self.config = config
        n = len(codes)
        self.current_date = None
        self.cumulative_amount = np.zeros(n)
        self.cumulative_shares = np.zeros(n)
        self.previous_close = np.full(n, np.nan)
        self.previous_vwap = np.full(n, np.nan)
        self.ordered_buy_today: set[int] = set()

    def _new_session(self, date: int) -> None:
        self.current_date = date
        self.cumulative_amount.fill(0.0)
        self.cumulative_shares.fill(0.0)
        self.previous_close.fill(np.nan)
        self.previous_vwap.fill(np.nan)
        self.ordered_buy_today.clear()

    def on_minute(self, context: MinuteStrategyContext):
        bar = context.bar
        if self.current_date != bar.date:
            self._new_session(bar.date)
        valid = (
            np.isfinite(bar.close) & (bar.close > 0) &
            np.isfinite(bar.volume) & (bar.volume > 0) &
            np.isfinite(bar.amount) & (bar.amount > 0)
        )
        # QMT's volume field is in lots (100 shares), while amount is yuan.
        self.cumulative_amount[valid] += bar.amount[valid]
        self.cumulative_shares[valid] += bar.volume[valid] * 100.0
        vwap = np.divide(
            self.cumulative_amount, self.cumulative_shares,
            out=np.full(len(self.codes), np.nan),
            where=self.cumulative_shares > 0,
        )
        pending = context.pending_codes
        orders = []

        for j in np.flatnonzero(context.account.shares > 0):
            code = self.codes[int(j)]
            if code in pending or context.account.sellable_shares[j] <= 0:
                continue
            if valid[j] and np.isfinite(vwap[j]) and bar.close[j] < vwap[j] * self.config.exit_vwap_ratio:
                orders.append(close_position(code, "close_below_intraday_vwap"))

        held_count = int(np.sum(context.account.shares > 0))
        reserved = sum(1 for code in pending if code in self.code_set)
        slots = max(0, self.config.max_positions - held_count - reserved)
        if self.config.buy_start_hhmm <= bar.hhmm <= self.config.buy_end_hhmm and slots:
            for j, code in enumerate(self.codes):
                if slots <= 0:
                    break
                if code not in context.candidates or code in pending:
                    continue
                if context.account.shares[j] > 0 or j in self.ordered_buy_today:
                    continue
                reclaimed = (
                    valid[j] and np.isfinite(vwap[j]) and
                    np.isfinite(self.previous_close[j]) and
                    np.isfinite(self.previous_vwap[j]) and
                    self.previous_close[j] <= self.previous_vwap[j] and
                    bar.close[j] > vwap[j] * (1.0 + self.config.reclaim_buffer)
                )
                if reclaimed:
                    orders.append(target_value(
                        code, self.config.target_value, "intraday_vwap_reclaim"
                    ))
                    self.ordered_buy_today.add(j)
                    slots -= 1

        self.previous_close[valid] = bar.close[valid]
        self.previous_vwap[np.isfinite(vwap)] = vwap[np.isfinite(vwap)]
        return orders

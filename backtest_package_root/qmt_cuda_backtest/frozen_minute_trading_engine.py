"""Versioned A-share one-minute execution and account engine.

Signals are created after minute ``t`` closes and are processed once, at the
open of the next available global minute.  Strategies never mutate this state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

try:
    from .frozen_daily_trading_engine import InitialPortfolio
    from .minute_strategy_interface import (
        MinuteAccountSnapshot,
        MinuteBarSnapshot,
        MinuteOrder,
    )
except ImportError:
    from frozen_daily_trading_engine import InitialPortfolio
    from minute_strategy_interface import MinuteAccountSnapshot, MinuteBarSnapshot, MinuteOrder


ENGINE_VERSION = "a_share_minute_v1"


@dataclass(frozen=True)
class MinuteExecutionConfig:
    commission_rate: float = 0.000285
    stamp_rate: float = 0.00025
    minimum_commission: float = 5.0
    max_volume_participation: float = 0.10
    reject_locked_limit: bool = True


def engine_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _fee(value: float, side: str, config: MinuteExecutionConfig) -> float:
    value = float(value)
    commission = max(config.minimum_commission, value * config.commission_rate)
    stamp = value * config.stamp_rate if side == "SELL" else 0.0
    return commission + stamp


def _minimum_buy(code: str) -> int:
    return 200 if code.startswith(("688", "689")) else 100


def _limit_ratio(code: str) -> float:
    if code.startswith(("688", "689", "300", "301")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


class FrozenMinuteTradingEngine:
    def __init__(
        self,
        codes: list[str],
        config: MinuteExecutionConfig,
        initial_portfolio: InitialPortfolio,
    ) -> None:
        if not 0 < config.max_volume_participation <= 1:
            raise ValueError("max_volume_participation must be in (0, 1]")
        self.codes = codes
        self.lookup = {code: i for i, code in enumerate(codes)}
        self.config = config
        self.cash = float(initial_portfolio.cash)
        self.shares = np.zeros(len(codes), dtype=np.int64)
        self.sellable_shares = np.zeros(len(codes), dtype=np.int64)
        self.average_cost = np.full(len(codes), np.nan)
        self.last_price = np.full(len(codes), np.nan)
        self.current_date: int | None = None
        self.order_rows: list[list] = []
        for position in initial_portfolio.positions:
            if position.code not in self.lookup:
                raise KeyError(f"initial position is absent from minute universe: {position.code}")
            j = self.lookup[position.code]
            self.shares[j] = position.shares
            self.sellable_shares[j] = position.shares
            if position.average_cost is not None:
                self.average_cost[j] = position.average_cost

    def start_session(self, date: int) -> None:
        if self.current_date == date:
            return
        self.current_date = date
        # All shares carried from prior sessions become sellable.  Buys later
        # today do not increase this array, enforcing A-share T+1.
        self.sellable_shares[:] = self.shares

    def prime_marks(self, prices: np.ndarray) -> None:
        valid = np.isfinite(prices) & (prices > 0)
        self.last_price[valid] = prices[valid]
        missing_cost = (self.shares > 0) & ~np.isfinite(self.average_cost) & valid
        self.average_cost[missing_cost] = prices[missing_cost]
        fallback = (
            (self.shares > 0) & ~np.isfinite(self.last_price) &
            np.isfinite(self.average_cost)
        )
        self.last_price[fallback] = self.average_cost[fallback]

    def equity(self) -> float:
        return float(self.cash + np.sum(
            self.shares * np.nan_to_num(self.last_price, nan=0.0)
        ))

    def account_snapshot(self) -> MinuteAccountSnapshot:
        arrays = []
        for values in (self.shares, self.sellable_shares, self.average_cost):
            copy = values.copy()
            copy.flags.writeable = False
            arrays.append(copy)
        return MinuteAccountSnapshot(
            float(self.cash), self.equity(), arrays[0], arrays[1], arrays[2]
        )

    def mark_close(self, bar: MinuteBarSnapshot) -> float:
        valid = np.isfinite(bar.close) & (bar.close > 0)
        self.last_price[valid] = bar.close[valid]
        return self.equity()

    def process_orders(
        self,
        signal_timestamp: int,
        bar: MinuteBarSnapshot,
        orders: Sequence[MinuteOrder],
    ) -> None:
        self.start_session(bar.date)
        for order in orders:
            self._process_one(signal_timestamp, bar, order)

    def _record(
        self, signal_timestamp, bar, order, side, requested, filled,
        price, fee, status, reason,
    ) -> None:
        self.order_rows.append([
            signal_timestamp, bar.timestamp, bar.date, bar.hhmm, order.code,
            order.kind, side, requested, filled, price, fee, status,
            order.reason, reason,
        ])

    def _reject(self, signal_timestamp, bar, order, reason) -> None:
        self._record(
            signal_timestamp, bar, order, "", 0, 0, "", 0.0,
            "REJECTED", reason,
        )

    def _process_one(self, signal_timestamp, bar, order) -> None:
        j = self.lookup.get(order.code)
        if j is None:
            self._reject(signal_timestamp, bar, order, "code_not_in_universe")
            return
        price = float(bar.open[j])
        volume = int(bar.volume[j]) if np.isfinite(bar.volume[j]) else 0
        prev_close = float(bar.prev_close[j])
        if not np.isfinite(price) or price <= 0:
            self._reject(signal_timestamp, bar, order, "missing_execution_price")
            return
        if volume <= 0:
            self._reject(signal_timestamp, bar, order, "zero_minute_volume")
            return

        if order.kind == "close":
            target = 0
        elif order.kind == "target_shares":
            target = int(order.value)
            if target < 0:
                self._reject(signal_timestamp, bar, order, "negative_target_shares")
                return
        elif order.kind == "target_value":
            value = float(order.value)
            if not np.isfinite(value) or value < 0:
                self._reject(signal_timestamp, bar, order, "invalid_target_value")
                return
            target = int(value / price / 100) * 100
            minimum = _minimum_buy(order.code)
            # Match the existing daily/QMT rule requested for expensive
            # securities: a positive target means at least one legal lot;
            # the cash check below may still reject it.
            if 0 < value and target < minimum:
                target = minimum
        else:
            self._reject(signal_timestamp, bar, order, "unsupported_order_kind")
            return

        current = int(self.shares[j])
        delta = target - current
        if delta == 0:
            self._record(
                signal_timestamp, bar, order, "", 0, 0, price, 0.0,
                "NOOP", "already_at_target",
            )
            return
        side = "BUY" if delta > 0 else "SELL"
        ratio = _limit_ratio(order.code)
        if self.config.reject_locked_limit and np.isfinite(prev_close) and prev_close > 0:
            upper = round(prev_close * (1.0 + ratio) + 1e-10, 2)
            lower = round(prev_close * (1.0 - ratio) + 1e-10, 2)
            if side == "BUY" and price >= upper - 0.005:
                self._reject(signal_timestamp, bar, order, "buy_at_locked_upper_limit")
                return
            if side == "SELL" and price <= lower + 0.005:
                self._reject(signal_timestamp, bar, order, "sell_at_locked_lower_limit")
                return

        # QMT stores volume in lots. Convert the participation cap to shares.
        participation = int(volume * self.config.max_volume_participation) * 100
        requested = abs(delta)
        if requested < 100:
            participation = max(participation, requested)
        if side == "BUY":
            minimum = _minimum_buy(order.code) if current == 0 else 100
            qty = min(requested, participation)
            qty = int(qty / 100) * 100
            affordable = int(max(0.0, self.cash - self.config.minimum_commission) / price / 100) * 100
            qty = min(qty, affordable)
            if qty < minimum:
                self._reject(signal_timestamp, bar, order, "insufficient_cash_or_liquidity")
                return
            value = qty * price
            fee = _fee(value, "BUY", self.config)
            while qty >= minimum and value + fee > self.cash:
                qty -= 100
                value = qty * price
                fee = _fee(value, "BUY", self.config)
            if qty < minimum:
                self._reject(signal_timestamp, bar, order, "insufficient_cash_after_fee")
                return
            old = current
            old_value = 0.0 if old == 0 else float(self.average_cost[j]) * old
            self.cash -= value + fee
            self.shares[j] += qty
            self.average_cost[j] = (old_value + value) / self.shares[j]
        else:
            available = int(self.sellable_shares[j])
            qty = min(requested, available, participation)
            if target == 0 and requested <= available and requested <= participation:
                qty = requested  # Closing an odd lot in one order is allowed.
            elif qty >= 100:
                qty = int(qty / 100) * 100
            if qty <= 0:
                self._reject(signal_timestamp, bar, order, "t_plus_one_or_liquidity")
                return
            value = qty * price
            fee = _fee(value, "SELL", self.config)
            self.cash += value - fee
            self.shares[j] -= qty
            self.sellable_shares[j] -= qty
            if self.shares[j] == 0:
                self.average_cost[j] = np.nan
        status = "FILLED" if qty == requested else "PARTIAL"
        self._record(
            signal_timestamp, bar, order, side, requested, qty, price, fee,
            status, "" if status == "FILLED" else "volume_cash_or_t1_cap",
        )

    def ending_positions(self) -> dict[str, dict]:
        return {
            code: {
                "shares": int(self.shares[j]),
                "sellable_shares": int(self.sellable_shares[j]),
                "average_cost": (float(self.average_cost[j])
                                 if np.isfinite(self.average_cost[j]) else None),
            }
            for j, code in enumerate(self.codes) if self.shares[j] > 0
        }

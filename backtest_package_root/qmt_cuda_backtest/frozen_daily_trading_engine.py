"""Versioned daily trading engine.

Strategies may be replaced freely, but this module owns cash, positions,
commissions, lot sizes, order sequence and mark-to-market.  Change execution
semantics only by creating a new ENGINE_VERSION and retaining regression tests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


ENGINE_VERSION = "qmt_daily_v1"


@dataclass(frozen=True)
class ExecutionConfig:
    commission_rate: float = 0.000285
    stamp_rate: float = 0.00025
    minimum_commission: float = 5.0
    qmt_selected_state: bool = False


@dataclass(frozen=True)
class InitialPosition:
    code: str
    shares: int
    average_cost: float | None = None


@dataclass(frozen=True)
class InitialPortfolio:
    cash: float
    positions: tuple[InitialPosition, ...] = ()
    selected: tuple[str, ...] | None = None


def engine_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def load_initial_portfolio(path: Path, cash_override: float | None = None) -> InitialPortfolio:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("initial portfolio must be a JSON object")
    cash = cash_override if cash_override is not None else payload.get("cash", 0.0)
    try:
        cash = float(cash)
    except (TypeError, ValueError) as exc:
        raise ValueError("initial portfolio cash must be numeric") from exc
    if not np.isfinite(cash) or cash < 0:
        raise ValueError("initial portfolio cash must be finite and non-negative")

    raw_positions = payload.get("positions", [])
    if not isinstance(raw_positions, list):
        raise ValueError("initial portfolio positions must be a list")
    positions = []
    seen = set()
    for row in raw_positions:
        if not isinstance(row, dict):
            raise ValueError("each initial position must be a JSON object")
        code = str(row.get("code", "")).strip()
        if not code or code in seen:
            raise ValueError(f"missing or duplicate initial position code: {code!r}")
        seen.add(code)
        shares = row.get("shares")
        if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
            raise ValueError(f"initial shares for {code} must be a positive integer")
        raw_cost = row.get("average_cost")
        cost = None if raw_cost is None else float(raw_cost)
        if cost is not None and (not np.isfinite(cost) or cost <= 0):
            raise ValueError(f"initial average_cost for {code} must be positive")
        positions.append(InitialPosition(code, shares, cost))

    raw_selected = payload.get("selected")
    selected = None
    if raw_selected is not None:
        if not isinstance(raw_selected, list) or not all(isinstance(x, str) for x in raw_selected):
            raise ValueError("initial portfolio selected must be a list of codes")
        if len(set(raw_selected)) != len(raw_selected):
            raise ValueError("initial portfolio selected contains duplicate codes")
        selected = tuple(raw_selected)
    return InitialPortfolio(cash, tuple(positions), selected)


def cash_portfolio(cash: float) -> InitialPortfolio:
    cash = float(cash)
    if not np.isfinite(cash) or cash < 0:
        raise ValueError("initial cash must be finite and non-negative")
    return InitialPortfolio(cash)


def _trading_fee(value: float, side: str, config: ExecutionConfig) -> float:
    value = float(value)
    commission = max(float(config.minimum_commission), value * float(config.commission_rate))
    stamp = value * float(config.stamp_rate) if side == "SELL" else 0.0
    return commission + stamp


def _minimum_lot(code: str) -> int:
    return 200 if code.startswith(("688", "689")) else 100


class FrozenDailyTradingEngine:
    """Stateful, strategy-agnostic execution and accounting engine."""

    def __init__(
        self,
        codes: list[str],
        industries: list[str],
        config: ExecutionConfig,
        initial_portfolio: InitialPortfolio,
    ) -> None:
        if len(codes) != len(industries):
            raise ValueError("codes and industries must have equal lengths")
        self.codes = codes
        self.industries = industries
        self.config = config
        self.cash = float(initial_portfolio.cash)
        self.shares = np.zeros(len(codes), dtype=np.int64)
        self.average_cost = np.full(len(codes), np.nan)
        self.cooldown_until = np.zeros(len(codes), dtype=np.int64)
        self.last_price = np.full(len(codes), np.nan)
        lookup = {code: i for i, code in enumerate(codes)}
        position_order = []
        for position in initial_portfolio.positions:
            if position.code not in lookup:
                raise KeyError(f"initial position is absent from market cache: {position.code}")
            j = lookup[position.code]
            self.shares[j] = position.shares
            if position.average_cost is not None:
                self.average_cost[j] = position.average_cost
            position_order.append(j)
        selected_codes = initial_portfolio.selected
        if selected_codes is None:
            self.selected_state = position_order
        else:
            unknown = [code for code in selected_codes if code not in lookup]
            if unknown:
                raise KeyError(f"initial selected codes are absent from market cache: {unknown}")
            self.selected_state = [lookup[code] for code in selected_codes]
        self.trades: list[list] = []

    @property
    def actual_held(self) -> set[int]:
        return set(np.flatnonzero(self.shares > 0).tolist())

    @property
    def strategy_held(self) -> set[int]:
        return (set(self.selected_state) if self.config.qmt_selected_state
                else self.actual_held)

    def prime_marks(self, prices: np.ndarray) -> None:
        valid = np.isfinite(prices) & (prices > 0)
        self.last_price[valid] = prices[valid]
        missing_cost = (self.shares > 0) & ~np.isfinite(self.average_cost) & valid
        self.average_cost[missing_cost] = prices[missing_cost]
        cost_fallback = (
            (self.shares > 0) & ~np.isfinite(self.last_price) &
            np.isfinite(self.average_cost)
        )
        self.last_price[cost_fallback] = self.average_cost[cost_fallback]

    def equity(self) -> float:
        return self.cash + np.sum(
            self.shares * np.nan_to_num(self.last_price, nan=0.0)
        )

    def _record(
        self, date: int, j: int, side: str, price: float, qty: int,
        scores: np.ndarray, breadth: float, fee: float,
    ) -> None:
        score = scores[j] if np.isfinite(scores[j]) else ""
        self.trades.append([
            date, self.codes[j], self.industries[j], side, price, qty,
            score, breadth, fee,
        ])

    def execute(
        self,
        date: int,
        bar_index: int,
        prices: np.ndarray,
        desired: tuple[int, ...] | list[int],
        scores: np.ndarray,
        breadth: float,
        target_slots: int,
        stopped: frozenset[int] | set[int] = frozenset(),
        cooldown_days: int = 0,
    ) -> None:
        if target_slots <= 0:
            raise ValueError("target_slots must be positive")
        open_valid = np.isfinite(prices) & (prices > 0)
        self.last_price[open_valid] = prices[open_valid]
        desired = list(desired)
        desired_set = set(desired)
        if self.config.qmt_selected_state:
            self._execute_qmt(
                date, bar_index, prices, open_valid, desired, desired_set,
                scores, breadth, target_slots, set(stopped), cooldown_days,
            )
        else:
            self._execute_legacy(
                date, bar_index, prices, open_valid, desired, desired_set,
                scores, breadth, target_slots, set(stopped), cooldown_days,
            )

    def _execute_qmt(
        self, date, bar_index, prices, open_valid, desired, desired_set,
        scores, breadth, target_slots, stopped, cooldown_days,
    ) -> None:
        if desired_set == set(self.selected_state):
            return
        for j in self.selected_state:
            if j in desired_set or self.shares[j] <= 0 or not open_valid[j]:
                continue
            qty = int(self.shares[j]); price = prices[j]; value = qty * price
            fee = _trading_fee(value, "SELL", self.config)
            self.cash += value - fee; self.shares[j] = 0; self.average_cost[j] = np.nan
            if j in stopped:
                self.cooldown_until[j] = bar_index + cooldown_days + 1
            self._record(date, j, "SELL", price, qty, scores, breadth, fee)

        mark = np.where(open_valid, prices, self.last_price)
        total_asset = self.cash + float(
            np.sum(self.shares * np.nan_to_num(mark, nan=0.0))
        )
        base_target_value = total_asset / target_slots if desired else 0.0
        for j in desired:
            if not open_valid[j]:
                continue
            price = float(prices[j]); minimum = _minimum_lot(self.codes[j])
            target_value = max(base_target_value, price * minimum)
            target_qty = max(int(target_value / price / 100) * 100, minimum)
            delta = int(target_qty - self.shares[j])
            if delta < 0:
                qty = -delta; value = qty * price
                fee = _trading_fee(value, "SELL", self.config)
                self.cash += value - fee; self.shares[j] -= qty
                if self.shares[j] == 0:
                    self.average_cost[j] = np.nan
                self._record(date, j, "SELL", price, qty, scores, breadth, fee)
            elif delta > 0:
                self._buy(date, j, price, delta, minimum, scores, breadth)
        self.selected_state = desired

    def _execute_legacy(
        self, date, bar_index, prices, open_valid, desired, desired_set,
        scores, breadth, target_slots, stopped, cooldown_days,
    ) -> None:
        actual_held = self.actual_held
        if desired_set == actual_held or not (
            not actual_held or all(open_valid[j] for j in actual_held)
        ):
            return
        total_asset = self.cash + float(np.sum(self.shares[open_valid] * prices[open_valid]))
        target_value = total_asset / target_slots if desired else 0.0
        target_shares = np.zeros(len(self.codes), dtype=np.int64)
        for j in desired:
            price = prices[j]; minimum = _minimum_lot(self.codes[j])
            qty = int(target_value / price / 100) * 100
            if qty < minimum and total_asset >= price * minimum:
                qty = minimum
            target_shares[j] = qty
        for j in sorted(actual_held):
            qty = int(max(0, self.shares[j] - target_shares[j]))
            if not qty:
                continue
            price = prices[j]; value = qty * price
            fee = _trading_fee(value, "SELL", self.config)
            self.cash += value - fee; self.shares[j] -= qty
            if self.shares[j] == 0:
                self.average_cost[j] = np.nan
                if j in stopped:
                    self.cooldown_until[j] = bar_index + cooldown_days + 1
            self._record(date, j, "SELL", price, qty, scores, breadth, fee)
        for j in desired:
            qty = int(max(0, target_shares[j] - self.shares[j]))
            if qty:
                self._buy(
                    date, j, prices[j], qty, _minimum_lot(self.codes[j]),
                    scores, breadth,
                )

    def _buy(self, date, j, price, requested, minimum, scores, breadth) -> None:
        affordable = int(max(0.0, self.cash - self.config.minimum_commission) / price / 100) * 100
        qty = min(int(requested), affordable)
        if qty < minimum:
            return
        value = qty * price
        fee = _trading_fee(value, "BUY", self.config)
        while qty >= minimum and value + fee > self.cash:
            qty -= 100; value = qty * price
            fee = _trading_fee(value, "BUY", self.config)
        if qty < minimum:
            return
        old_shares = int(self.shares[j])
        old_cost_value = 0.0 if old_shares == 0 else self.average_cost[j] * old_shares
        self.cash -= value + fee; self.shares[j] += qty
        self.average_cost[j] = (old_cost_value + value) / self.shares[j]
        self._record(date, j, "BUY", price, qty, scores, breadth, fee)

    def close_bar(self, close_prices: np.ndarray) -> float:
        valid = np.isfinite(close_prices) & (close_prices > 0)
        self.last_price[valid] = close_prices[valid]
        return self.equity()

    def state_row(self, date: int, desired: tuple[int, ...] | list[int]) -> list:
        actual = self.actual_held
        target = (set(self.selected_state) if self.config.qmt_selected_state
                  else set(desired))
        return [
            date, len(target), len(actual), len(target - actual), len(actual - target),
            ",".join(self.codes[j] for j in sorted(target)),
            ",".join(self.codes[j] for j in sorted(actual)),
        ]

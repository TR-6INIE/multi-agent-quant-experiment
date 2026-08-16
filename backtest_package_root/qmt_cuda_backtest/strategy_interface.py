"""Stable interface between daily target strategies and the trading engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class StrategyContext:
    bar_index: int
    date: int
    calendar: np.ndarray
    codes: list[str]
    execution_prices: np.ndarray
    closes: np.ndarray
    amounts: np.ndarray
    industries: list[str]
    actual_held: frozenset[int]
    selected_held: frozenset[int]
    average_cost: np.ndarray
    cooldown_until: np.ndarray


@dataclass(frozen=True)
class StrategyDecision:
    desired: tuple[int, ...]
    scores: np.ndarray
    breadth: float
    target_slots: int
    stopped: frozenset[int] = frozenset()


class DailyTargetStrategy(Protocol):
    """A strategy decides targets; it never changes cash, shares or fills."""

    name: str

    def ready(self, context: StrategyContext) -> bool:
        ...

    def decide(self, context: StrategyContext) -> StrategyDecision:
        ...

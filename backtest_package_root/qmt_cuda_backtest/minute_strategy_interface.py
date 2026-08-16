"""Stable interface for event-driven one-minute strategies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, Sequence

import numpy as np


OrderKind = Literal["target_value", "target_shares", "close"]


@dataclass(frozen=True)
class MinuteOrder:
    code: str
    kind: OrderKind
    value: float | int = 0
    reason: str = ""


def target_value(code: str, value: float, reason: str = "") -> MinuteOrder:
    return MinuteOrder(code, "target_value", float(value), reason)


def target_shares(code: str, shares: int, reason: str = "") -> MinuteOrder:
    return MinuteOrder(code, "target_shares", int(shares), reason)


def close_position(code: str, reason: str = "") -> MinuteOrder:
    return MinuteOrder(code, "close", 0, reason)


@dataclass(frozen=True)
class MinuteBarSnapshot:
    timestamp: int
    date: int
    hhmm: int
    codes: list[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    amount: np.ndarray
    prev_close: np.ndarray


@dataclass(frozen=True)
class MinuteAccountSnapshot:
    cash: float
    equity: float
    shares: np.ndarray
    sellable_shares: np.ndarray
    average_cost: np.ndarray


@dataclass(frozen=True)
class MinuteStrategyContext:
    bar: MinuteBarSnapshot
    candidates: frozenset[str]
    pending_codes: frozenset[str]
    account: MinuteAccountSnapshot


class MinuteStrategy(Protocol):
    name: str

    def on_minute(self, context: MinuteStrategyContext) -> Sequence[MinuteOrder]:
        ...

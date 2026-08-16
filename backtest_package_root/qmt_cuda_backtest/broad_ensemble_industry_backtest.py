"""Broad-universe multi-horizon momentum with industry and breadth controls."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    from .fundamental_store import build_rule_mask, load_fundamental_cache, parse_rule
    from .frozen_daily_trading_engine import (
        ENGINE_VERSION,
        ExecutionConfig,
        FrozenDailyTradingEngine,
        InitialPortfolio,
        cash_portfolio,
        engine_sha256,
        load_initial_portfolio,
    )
    from .strategy_interface import StrategyContext, StrategyDecision
    from .qmt_dat import read_minute_bars
except ImportError:
    from fundamental_store import build_rule_mask, load_fundamental_cache, parse_rule
    from frozen_daily_trading_engine import (
        ENGINE_VERSION,
        ExecutionConfig,
        FrozenDailyTradingEngine,
        InitialPortfolio,
        cash_portfolio,
        engine_sha256,
        load_initial_portfolio,
    )
    from strategy_interface import StrategyContext, StrategyDecision
    from qmt_dat import read_minute_bars


@dataclass(frozen=True)
class Params:
    score: str
    skip: int
    topk: int
    buffer: float
    max_industry: int
    trend_ma: int
    min_breadth: float


@dataclass(frozen=True)
class RiskControls:
    entry_breadth: float = 0.50
    breadth_lookback: int = 0
    max_breadth_drop: float | None = None
    max_extension: float | None = None
    confirmation_days: int = 1
    stop_loss: float | None = None
    cooldown_days: int = 0


def load_industries(folder: Path, codes: list[str]) -> list[str]:
    lookup = {}
    for path in folder.glob('SW1*'):
        if not path.is_file():
            continue
        try:
            members = path.read_text(encoding='ascii').strip().split(',')
        except UnicodeDecodeError:
            continue
        for code in members:
            if code and code not in lookup:
                lookup[code] = path.name
    return [lookup.get(code, 'UNKNOWN_' + code) for code in codes]


def load_backtest_cache(path: Path, execution_price_key: str):
    with np.load(path, allow_pickle=False) as data:
        required = {'calendar', 'codes', 'closes', 'amounts', execution_price_key}
        missing = sorted(required - set(data.files))
        if missing:
            raise ValueError(f'cache is missing fields: {missing}')
        calendar = data['calendar']
        codes = data['codes'].tolist()
        execution_prices = data[execution_price_key]
        closes = data['closes']
        amounts = data['amounts']
    expected = (len(calendar), len(codes))
    for name, values in (
            (execution_price_key, execution_prices), ('closes', closes), ('amounts', amounts)):
        if values.shape != expected:
            raise ValueError(f'{name} shape {values.shape} does not match {expected}')
    return calendar, codes, execution_prices, closes, amounts


def percentile(values, codes=None):
    result = np.full(len(values), np.nan)
    valid = np.flatnonzero(np.isfinite(values))
    if len(valid):
        if codes is None:
            order = valid[np.argsort(values[valid])]
        else:
            # QMT uses sorted((value, stock)), so equal values are ordered by
            # the security code rather than NumPy's implementation-dependent
            # argsort tie order.
            order = np.array(
                sorted(valid.tolist(), key=lambda j: (values[j], codes[j])),
                dtype=np.int64,
            )
        result[order] = (np.arange(len(order)) + 1.0) / len(order)
    return result


def column_nanmean(values):
    """Column mean that leaves all-NaN columns as NaN without warning."""
    finite = np.isfinite(values)
    count = np.sum(finite, axis=0)
    total = np.nansum(values, axis=0)
    return np.divide(
        total, count, out=np.full(values.shape[1], np.nan, dtype=float), where=count > 0
    )


def momentum_component(closes, end, lookback, risk_adjusted):
    start = end - lookback
    now, old = closes[end], closes[start]
    value = now / old - 1.0
    if risk_adjusted:
        daily = closes[start + 1:end + 1] / closes[start:end] - 1.0
        vol = np.nanstd(daily, axis=0) * np.sqrt(lookback)
        value = value / np.maximum(vol, 0.05)
    value[~np.isfinite(now) | ~np.isfinite(old)] = np.nan
    return percentile(value)


def scores_at(
    closes, amounts, signal_i, p, codes=None, active=None, selection_active=None
):
    end = signal_i - p.skip
    if end - 60 < 1:
        return np.full(closes.shape[1], np.nan), 0.0
    risk = p.score == 'risk_ensemble'
    # Match the QMT strategy: establish trend/liquidity eligibility first,
    # then calculate each cross-sectional percentile only inside that pool.
    recent = closes[max(0, signal_i - 74):signal_i + 1]
    history_count = np.sum(np.isfinite(recent), axis=0)
    ma20 = column_nanmean(closes[signal_i - 19:signal_i + 1])
    trend_ma = column_nanmean(closes[signal_i - p.trend_ma + 1:signal_i + 1])
    breadth_valid = (history_count >= 60) & np.isfinite(closes[signal_i]) & np.isfinite(ma20)
    if active is not None:
        breadth_valid &= active
    breadth = (float(np.mean(closes[signal_i, breadth_valid] > ma20[breadth_valid]))
               if np.any(breadth_valid) else 0.0)
    window = amounts[signal_i - 5:signal_i + 1]
    count = np.sum(np.isfinite(window), axis=0)
    liquid = np.divide(np.nansum(window, axis=0), count,
                       out=np.zeros(closes.shape[1]), where=count > 0)
    eligible = ((history_count >= max(61, p.trend_ma)) &
                np.isfinite(closes[end]) & np.isfinite(trend_ma) &
                (closes[signal_i] > trend_ma) & (liquid >= 50_000_000))
    if active is not None:
        eligible &= active
    if selection_active is not None:
        eligible &= selection_active

    def eligible_component(lookback):
        old = closes[end - lookback]
        raw = closes[end] / old - 1.0
        raw[~eligible | ~np.isfinite(old) | (old <= 0)] = np.nan
        if risk:
            daily = closes[end - lookback + 1:end + 1] / closes[end - lookback:end] - 1.0
            vol = np.nanstd(daily, axis=0) * np.sqrt(lookback)
            raw = raw / np.maximum(vol, 0.05)
        return percentile(raw, codes=codes)

    r20 = eligible_component(20)
    r40 = eligible_component(40)
    r60 = eligible_component(60)
    if p.score == 'acceleration':
        score = r20 * .55 + r40 * .30 + r60 * .15
    elif p.score == 'short_momentum':
        score = r20 * .70 + r40 * .20 + r60 * .10
    elif p.score == 'medium_momentum':
        score = r20 * .20 + r40 * .55 + r60 * .25
    elif p.score == 'long_momentum':
        score = r20 * .15 + r40 * .30 + r60 * .55
    elif p.score == 'consensus_momentum':
        # Prefer stocks that rank well at every horizon instead of allowing one
        # exceptional horizon to compensate for a weak one.
        score = np.minimum(np.minimum(r20, r40), r60)
    elif p.score == 'balanced_momentum':
        # Geometric aggregation penalizes disagreement between horizons while
        # retaining more differentiation than the strict minimum above.
        score = np.cbrt(r20 * r40 * r60)
    else:
        score = r20 * .40 + r40 * .35 + r60 * .25
    score[~eligible] = np.nan
    return score, breadth


def trading_fee(value, side, commission_rate, stamp_rate, minimum_commission):
    commission = max(float(minimum_commission), float(value) * float(commission_rate))
    stamp = float(value) * float(stamp_rate) if side == 'SELL' else 0.0
    return commission + stamp


def choose_desired(ranked, held, industries, p):
    rank_map = {int(j): rank for rank, j in enumerate(ranked)}
    keep_limit = max(p.topk, int(np.ceil(p.topk * p.buffer)))
    counts = {}
    desired = []
    for j in sorted(held, key=lambda x: rank_map.get(x, 10**9)):
        industry = industries[j]
        if rank_map.get(j, 10**9) < keep_limit and counts.get(industry, 0) < p.max_industry:
            desired.append(j); counts[industry] = counts.get(industry, 0) + 1
    for raw_j in ranked:
        j = int(raw_j); industry = industries[j]
        if j not in desired and counts.get(industry, 0) < p.max_industry:
            desired.append(j); counts[industry] = counts.get(industry, 0) + 1
        if len(desired) >= p.topk:
            break
    return desired[:p.topk]


def choose_kept(ranked, held, industries, p):
    """Keep valid buffered holdings without filling empty slots."""
    rank_map = {int(j): rank for rank, j in enumerate(ranked)}
    keep_limit = max(p.topk, int(np.ceil(p.topk * p.buffer)))
    counts = {}
    desired = []
    for j in sorted(held, key=lambda x: rank_map.get(x, 10**9)):
        industry = industries[j]
        if rank_map.get(j, 10**9) < keep_limit and counts.get(industry, 0) < p.max_industry:
            desired.append(j)
            counts[industry] = counts.get(industry, 0) + 1
    return desired


# Historical inline implementation retained only as a byte-for-byte audit
# reference for ENGINE_VERSION qmt_daily_v1.  Production code and public
# backtest() below never call it; all account mutations go through the frozen
# engine module.
def legacy_backtest_reference(calendar, codes, opens, closes, amounts, industries, p,
             start_date, end_date, initial_cash=1_000_000.0,
             commission_rate=0.000285, stamp_rate=0.00025,
             minimum_commission=5.0, risk=None,
             qmt_selected_state=False, qmt_fill_prelisting=False,
             prelisting_prices=None, prelisting_use_first_close=False,
             qmt_min_listed_sessions=0, exclude_unknown_industries=False,
             fundamental_mask=None):
    risk = risk or RiskControls(entry_breadth=p.min_breadth)
    if fundamental_mask is not None:
        expected = (len(calendar), len(codes))
        if fundamental_mask.shape != expected:
            raise ValueError(
                f'fundamental_mask shape {fundamental_mask.shape} does not match {expected}'
            )
    cash = float(initial_cash)
    shares = np.zeros(len(codes), dtype=np.int64)
    average_cost = np.full(len(codes), np.nan)
    cooldown_until = np.zeros(len(codes), dtype=np.int64)
    last_price = np.full(len(codes), np.nan)
    trades, equity_rows = [], []
    state_rows = []
    breadth_history = []
    # QMT's ContextInfo.selected is the strategy's intended target list.  It
    # is updated after submitting a rebalance even if an order is rejected or
    # only partly filled, and therefore must not be reconstructed from shares.
    selected_state = []
    score_closes = closes
    first_listed = np.zeros(len(codes), dtype=np.int64)
    if qmt_fill_prelisting:
        score_closes = closes.copy()
        first_listed[:] = len(calendar)
        for j in range(len(codes)):
            valid = np.flatnonzero(np.isfinite(closes[:, j]) & (closes[:, j] > 0))
            if not len(valid):
                continue
            first = int(valid[0]); first_listed[j] = first
            if first:
                supplied = None if prelisting_prices is None else prelisting_prices.get(codes[j])
                first_price = (float(supplied) if supplied is not None and supplied > 0 else
                               float(closes[first, j]) if prelisting_use_first_close else
                               float(opens[first, j])
                               if np.isfinite(opens[first, j]) and opens[first, j] > 0 else
                               float(closes[first, j]))
                score_closes[:first, j] = first_price
    first = int(np.searchsorted(calendar, start_date))
    last = int(np.searchsorted(calendar, end_date, side='right'))
    industry_eligible = np.array([
        not industry.startswith('UNKNOWN_') for industry in industries
    ], dtype=bool)
    for i in range(first, last):
        # ContextInfo.lookback is 75 in the QMT strategy.  In its backtest the
        # first usable callback is bar position 75 (the 76th session).  The
        # legacy local engine historically began as soon as the 60-day score
        # was computable, so retain that behavior outside QMT mode.
        if ((qmt_selected_state and i < 75) or
                (not qmt_selected_state and i <= max(65, p.trend_ma))):
            continue
        open_valid = np.isfinite(opens[i]) & (opens[i] > 0)
        last_price[open_valid] = opens[i, open_valid]
        actual_held = set(np.flatnonzero(shares > 0).tolist())
        strategy_held = set(selected_state) if qmt_selected_state else actual_held
        stopped = set()
        if risk.stop_loss is not None:
            stopped = {
                j for j in actual_held
                if np.isfinite(average_cost[j]) and open_valid[j] and
                opens[i, j] <= average_cost[j] * (1.0 - risk.stop_loss)
            }
        score_kwargs = {
            'codes': codes if qmt_selected_state else None,
            'active': ((first_listed <= i - 1) &
                       ((i - first_listed) >= qmt_min_listed_sessions))
                      if qmt_fill_prelisting else None,
        }
        if fundamental_mask is not None:
            score_kwargs['selection_active'] = fundamental_mask[i - 1]
        score, breadth = scores_at(
            score_closes, amounts, i - 1, p, **score_kwargs
        )
        # QMT chooses the target pool from completed daily data before asking
        # for the current 1-minute price.  A missing execution price can make
        # an order fail, but it does not remove the code from selected.
        rank_valid = np.isfinite(score) & (cooldown_until <= i)
        if exclude_unknown_industries:
            rank_valid &= industry_eligible
        if not qmt_selected_state:
            rank_valid &= open_valid
        valid = np.flatnonzero(rank_valid)
        if stopped:
            valid = np.array([j for j in valid if int(j) not in stopped], dtype=np.int64)
        if qmt_selected_state:
            ranked = np.array(
                sorted(valid.tolist(), key=lambda j: (score[j], codes[j]), reverse=True),
                dtype=np.int64,
            )
        else:
            ranked = valid[np.argsort(score[valid])[::-1]]
        if risk.max_extension is not None:
            ma20 = column_nanmean(closes[i - 20:i])
            extension = closes[i - 1] / ma20 - 1.0
            ranked = np.array([
                j for j in ranked
                if int(j) in strategy_held or (np.isfinite(extension[j]) and extension[j] <= risk.max_extension)
            ], dtype=np.int64)
        stable = True
        if (risk.max_breadth_drop is not None and risk.breadth_lookback > 0 and
                len(breadth_history) >= risk.breadth_lookback):
            recent_peak = max(breadth_history[-risk.breadth_lookback:])
            stable = breadth >= recent_peak - risk.max_breadth_drop
        confirmed = breadth >= risk.entry_breadth
        if confirmed and risk.confirmation_days > 1:
            needed = risk.confirmation_days - 1
            confirmed = (len(breadth_history) >= needed and
                         all(x >= risk.entry_breadth for x in breadth_history[-needed:]))
        if breadth < p.min_breadth:
            desired = []
        elif confirmed and stable:
            desired = choose_desired(ranked, strategy_held, industries, p)
        else:
            desired = choose_kept(ranked, strategy_held, industries, p)
        breadth_history.append(breadth)
        desired_set = set(desired)
        if qmt_selected_state:
            should_rebalance = desired_set != set(selected_state)
            if should_rebalance:
                # Match rebalance() in the QMT strategy exactly: first submit
                # target-zero orders in the old selected-list order.  Missing
                # prices leave actual shares untouched, just like a failed
                # order, but selected is still replaced below.
                for j in selected_state:
                    if j in desired_set or shares[j] <= 0 or not open_valid[j]:
                        continue
                    qty = int(shares[j]); price = opens[i, j]; value = qty * price
                    fee = trading_fee(value, 'SELL', commission_rate, stamp_rate, minimum_commission)
                    cash += value - fee; shares[j] = 0; average_cost[j] = np.nan
                    if j in stopped:
                        cooldown_until[j] = i + risk.cooldown_days + 1
                    trades.append([int(calendar[i]), codes[j], industries[j], 'SELL', price, qty,
                                   score[j] if np.isfinite(score[j]) else '', breadth, fee])

                # QMT reads account balance after submitting exit orders, then
                # uses the same 20% target for every desired code.  Value the
                # remaining portfolio at this bar, falling back to last price
                # where the current bar is unavailable.
                mark = np.where(open_valid, opens[i], last_price)
                mark = np.nan_to_num(mark, nan=0.0)
                total_asset = cash + float(np.sum(shares * mark))
                base_target_value = total_asset / p.topk if desired else 0.0

                # QMT calls order_target_value one code at a time in desired
                # order.  A retained overweight code may sell here; proceeds
                # are immediately available to later buys in the same bar.
                for j in desired:
                    if not open_valid[j]:
                        continue
                    price = float(opens[i, j])
                    minimum = 200 if codes[j].startswith(('688', '689')) else 100
                    target_value = max(base_target_value, price * minimum)
                    target_qty = int(target_value / price / 100) * 100
                    target_qty = max(target_qty, minimum)
                    delta = int(target_qty - shares[j])
                    if delta < 0:
                        qty = -delta; value = qty * price
                        fee = trading_fee(value, 'SELL', commission_rate, stamp_rate, minimum_commission)
                        cash += value - fee; shares[j] -= qty
                        if shares[j] == 0:
                            average_cost[j] = np.nan
                        trades.append([int(calendar[i]), codes[j], industries[j], 'SELL', price, qty,
                                       score[j] if np.isfinite(score[j]) else '', breadth, fee])
                    elif delta > 0:
                        affordable = int(max(0.0, cash - minimum_commission) / price / 100) * 100
                        qty = min(delta, affordable)
                        if qty >= minimum:
                            value = qty * price
                            fee = trading_fee(value, 'BUY', commission_rate, stamp_rate, minimum_commission)
                            while qty >= minimum and value + fee > cash:
                                qty -= 100; value = qty * price
                                fee = trading_fee(value, 'BUY', commission_rate, stamp_rate, minimum_commission)
                            if qty >= minimum:
                                old_shares = int(shares[j])
                                cash -= value + fee; shares[j] += qty
                                average_cost[j] = ((0.0 if old_shares == 0 else average_cost[j] * old_shares) +
                                                   value) / shares[j]
                                trades.append([int(calendar[i]), codes[j], industries[j], 'BUY', price, qty,
                                               score[j], breadth, fee])
                selected_state = list(desired)
        else:
            can_rebalance = not actual_held or all(open_valid[j] for j in actual_held)
            if desired_set != actual_held and can_rebalance:
                total_asset = cash + float(np.sum(shares[open_valid] * opens[i, open_valid]))
                target_value = total_asset / p.topk if desired else 0.0
                target_shares = np.zeros(len(codes), dtype=np.int64)
                for j in desired:
                    price = opens[i, j]
                    minimum = 200 if codes[j].startswith(('688', '689')) else 100
                    qty = int(target_value / price / 100) * 100
                    if qty < minimum and total_asset >= price * minimum:
                        qty = minimum
                    target_shares[j] = qty
                for j in sorted(actual_held):
                    qty = int(max(0, shares[j] - target_shares[j]))
                    if qty:
                        price = opens[i, j]; value = qty * price
                        fee = trading_fee(value, 'SELL', commission_rate, stamp_rate, minimum_commission)
                        cash += value - fee; shares[j] -= qty
                        if shares[j] == 0:
                            average_cost[j] = np.nan
                            if j in stopped:
                                cooldown_until[j] = i + risk.cooldown_days + 1
                        trades.append([int(calendar[i]), codes[j], industries[j], 'SELL', price, qty,
                                       score[j] if np.isfinite(score[j]) else '', breadth, fee])
                for j in desired:
                    qty = int(max(0, target_shares[j] - shares[j]))
                    if qty:
                        price = opens[i, j]
                        affordable = int(max(0.0, cash - minimum_commission) / price / 100) * 100
                        qty = min(qty, affordable)
                        minimum = 200 if codes[j].startswith(('688', '689')) else 100
                        if qty >= minimum:
                            value = qty * price
                            fee = trading_fee(value, 'BUY', commission_rate, stamp_rate, minimum_commission)
                            while qty >= minimum and value + fee > cash:
                                qty -= 100; value = qty * price
                                fee = trading_fee(value, 'BUY', commission_rate, stamp_rate, minimum_commission)
                            if qty >= minimum:
                                old_shares = int(shares[j])
                                cash -= value + fee; shares[j] += qty
                                average_cost[j] = ((0.0 if old_shares == 0 else average_cost[j] * old_shares) +
                                                   value) / shares[j]
                                trades.append([int(calendar[i]), codes[j], industries[j], 'BUY', price, qty,
                                               score[j], breadth, fee])
        valid_close = np.isfinite(closes[i]) & (closes[i] > 0)
        last_price[valid_close] = closes[i, valid_close]
        equity = cash + np.sum(shares * np.nan_to_num(last_price, nan=0.0))
        equity_rows.append([int(calendar[i]), equity, breadth, len(desired)])
        actual_after = set(np.flatnonzero(shares > 0).tolist())
        target_after = set(selected_state) if qmt_selected_state else set(desired)
        state_rows.append([
            int(calendar[i]), len(target_after), len(actual_after),
            len(target_after - actual_after), len(actual_after - target_after),
            ','.join(codes[j] for j in sorted(target_after)),
            ','.join(codes[j] for j in sorted(actual_after)),
        ])
    values = np.array([x[1] for x in equity_rows])
    peaks = np.maximum.accumulate(values)
    return {'return': values[-1] / initial_cash - 1,
            'max_drawdown': float(np.min(values / peaks - 1)),
            'trades': trades, 'equity': equity_rows, 'state': state_rows}


class BroadMomentumStrategy:
    """Broad-momentum target selection with no authority to trade."""

    name = 'broad_industry_neutral_momentum'

    def __init__(
        self, calendar, codes, execution_prices, closes, amounts, industries, p,
        risk, *, qmt_selected_state=False, qmt_fill_prelisting=False,
        prelisting_prices=None, prelisting_use_first_close=False,
        qmt_min_listed_sessions=0, exclude_unknown_industries=False,
        fundamental_mask=None,
    ):
        self.calendar = calendar
        self.codes = codes
        self.execution_prices = execution_prices
        self.closes = closes
        self.amounts = amounts
        self.industries = industries
        self.p = p
        self.risk = risk
        self.qmt_selected_state = qmt_selected_state
        self.qmt_fill_prelisting = qmt_fill_prelisting
        self.qmt_min_listed_sessions = qmt_min_listed_sessions
        self.exclude_unknown_industries = exclude_unknown_industries
        self.fundamental_mask = fundamental_mask
        self.breadth_history = []
        expected = (len(calendar), len(codes))
        if fundamental_mask is not None and fundamental_mask.shape != expected:
            raise ValueError(
                f'fundamental_mask shape {fundamental_mask.shape} does not match {expected}'
            )
        self.industry_eligible = np.array([
            not industry.startswith('UNKNOWN_') for industry in industries
        ], dtype=bool)
        self.score_closes = closes
        self.first_listed = np.zeros(len(codes), dtype=np.int64)
        if qmt_fill_prelisting:
            self.score_closes = closes.copy()
            self.first_listed[:] = len(calendar)
            for j in range(len(codes)):
                valid = np.flatnonzero(np.isfinite(closes[:, j]) & (closes[:, j] > 0))
                if not len(valid):
                    continue
                first = int(valid[0]); self.first_listed[j] = first
                if not first:
                    continue
                supplied = None if prelisting_prices is None else prelisting_prices.get(codes[j])
                first_price = (
                    float(supplied) if supplied is not None and supplied > 0 else
                    float(closes[first, j]) if prelisting_use_first_close else
                    float(execution_prices[first, j])
                    if np.isfinite(execution_prices[first, j]) and execution_prices[first, j] > 0 else
                    float(closes[first, j])
                )
                self.score_closes[:first, j] = first_price

    def ready(self, context: StrategyContext) -> bool:
        i = context.bar_index
        return not ((self.qmt_selected_state and i < 75) or
                    (not self.qmt_selected_state and i <= max(65, self.p.trend_ma)))

    def decide(self, context: StrategyContext) -> StrategyDecision:
        i = context.bar_index
        prices = context.execution_prices[i]
        open_valid = np.isfinite(prices) & (prices > 0)
        actual_held = set(context.actual_held)
        strategy_held = set(context.selected_held)
        stopped = set()
        if self.risk.stop_loss is not None:
            stopped = {
                j for j in actual_held
                if np.isfinite(context.average_cost[j]) and open_valid[j] and
                prices[j] <= context.average_cost[j] * (1.0 - self.risk.stop_loss)
            }
        score_kwargs = {
            'codes': self.codes if self.qmt_selected_state else None,
            'active': ((self.first_listed <= i - 1) &
                       ((i - self.first_listed) >= self.qmt_min_listed_sessions))
                      if self.qmt_fill_prelisting else None,
        }
        if self.fundamental_mask is not None:
            score_kwargs['selection_active'] = self.fundamental_mask[i - 1]
        score, breadth = scores_at(
            self.score_closes, self.amounts, i - 1, self.p, **score_kwargs
        )
        rank_valid = np.isfinite(score) & (context.cooldown_until <= i)
        if self.exclude_unknown_industries:
            rank_valid &= self.industry_eligible
        if not self.qmt_selected_state:
            rank_valid &= open_valid
        valid = np.flatnonzero(rank_valid)
        if stopped:
            valid = np.array(
                [j for j in valid if int(j) not in stopped], dtype=np.int64
            )
        if self.qmt_selected_state:
            ranked = np.array(
                sorted(valid.tolist(), key=lambda j: (score[j], self.codes[j]), reverse=True),
                dtype=np.int64,
            )
        else:
            ranked = valid[np.argsort(score[valid])[::-1]]
        if self.risk.max_extension is not None:
            ma20 = column_nanmean(self.closes[i - 20:i])
            extension = self.closes[i - 1] / ma20 - 1.0
            ranked = np.array([
                j for j in ranked
                if int(j) in strategy_held or
                (np.isfinite(extension[j]) and extension[j] <= self.risk.max_extension)
            ], dtype=np.int64)
        stable = True
        if (self.risk.max_breadth_drop is not None and self.risk.breadth_lookback > 0 and
                len(self.breadth_history) >= self.risk.breadth_lookback):
            recent_peak = max(self.breadth_history[-self.risk.breadth_lookback:])
            stable = breadth >= recent_peak - self.risk.max_breadth_drop
        confirmed = breadth >= self.risk.entry_breadth
        if confirmed and self.risk.confirmation_days > 1:
            needed = self.risk.confirmation_days - 1
            confirmed = (
                len(self.breadth_history) >= needed and
                all(x >= self.risk.entry_breadth for x in self.breadth_history[-needed:])
            )
        if breadth < self.p.min_breadth:
            desired = []
        elif confirmed and stable:
            desired = choose_desired(ranked, strategy_held, self.industries, self.p)
        else:
            desired = choose_kept(ranked, strategy_held, self.industries, self.p)
        self.breadth_history.append(breadth)
        return StrategyDecision(
            tuple(desired), score, float(breadth), self.p.topk, frozenset(stopped)
        )


def run_strategy_backtest(
    calendar, codes, execution_prices, closes, amounts, industries, strategy,
    start_date, end_date, initial_portfolio, execution_config,
    cooldown_days=0,
):
    """Run any DailyTargetStrategy through the frozen execution engine."""
    first = int(np.searchsorted(calendar, start_date))
    last = int(np.searchsorted(calendar, end_date, side='right'))
    if first >= last:
        raise ValueError(f'no trading sessions in requested range {start_date}..{end_date}')
    broker = FrozenDailyTradingEngine(codes, industries, execution_config, initial_portfolio)
    initial_marks = np.full(len(codes), np.nan)
    if first > 0:
        initial_marks[:] = closes[first - 1]
    fallback = execution_prices[first]
    replace = ~np.isfinite(initial_marks) | (initial_marks <= 0)
    initial_marks[replace] = fallback[replace]
    broker.prime_marks(initial_marks)
    initial_equity = broker.equity()
    if not np.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError('initial portfolio equity must be positive at the backtest start')

    equity_rows, state_rows = [], []
    for i in range(first, last):
        context = StrategyContext(
            bar_index=i,
            date=int(calendar[i]),
            calendar=calendar,
            codes=codes,
            execution_prices=execution_prices,
            closes=closes,
            amounts=amounts,
            industries=industries,
            actual_held=frozenset(broker.actual_held),
            selected_held=frozenset(broker.strategy_held),
            average_cost=broker.average_cost,
            cooldown_until=broker.cooldown_until,
        )
        if not strategy.ready(context):
            continue
        decision = strategy.decide(context)
        broker.execute(
            int(calendar[i]), i, execution_prices[i], decision.desired,
            decision.scores, decision.breadth, decision.target_slots,
            decision.stopped, cooldown_days,
        )
        equity = broker.close_bar(closes[i])
        equity_rows.append([
            int(calendar[i]), equity, decision.breadth, len(decision.desired)
        ])
        state_rows.append(broker.state_row(int(calendar[i]), decision.desired))
    if not equity_rows:
        raise ValueError('strategy produced no evaluable bars in the requested range')
    values = np.asarray([row[1] for row in equity_rows], dtype=np.float64)
    peaks = np.maximum.accumulate(values)
    ending_positions = {
        code: {
            'shares': int(broker.shares[j]),
            'average_cost': (float(broker.average_cost[j])
                             if np.isfinite(broker.average_cost[j]) else None),
        }
        for j, code in enumerate(codes) if broker.shares[j] > 0
    }
    return {
        'return': values[-1] / initial_equity - 1.0,
        'max_drawdown': float(np.min(values / peaks - 1.0)),
        'initial_equity': initial_equity,
        'final_equity': float(values[-1]),
        'final_cash': float(broker.cash),
        'ending_positions': ending_positions,
        'ending_selected': [
            codes[j] for j in (
                broker.selected_state if execution_config.qmt_selected_state
                else sorted(broker.actual_held)
            )
        ],
        'trades': broker.trades,
        'equity': equity_rows,
        'state': state_rows,
        'strategy_name': strategy.name,
        'engine_version': ENGINE_VERSION,
        'engine_sha256': engine_sha256(),
    }


def backtest(calendar, codes, opens, closes, amounts, industries, p,
             start_date, end_date, initial_cash=1_000_000.0,
             commission_rate=0.000285, stamp_rate=0.00025,
             minimum_commission=5.0, risk=None,
             qmt_selected_state=False, qmt_fill_prelisting=False,
             prelisting_prices=None, prelisting_use_first_close=False,
             qmt_min_listed_sessions=0, exclude_unknown_industries=False,
             fundamental_mask=None, initial_portfolio=None, strategy=None):
    """Compatibility wrapper that wires the broad strategy to the frozen engine."""
    risk = risk or RiskControls(entry_breadth=p.min_breadth)
    portfolio = initial_portfolio or cash_portfolio(initial_cash)
    if strategy is None:
        strategy = BroadMomentumStrategy(
            calendar, codes, opens, closes, amounts, industries, p, risk,
            qmt_selected_state=qmt_selected_state,
            qmt_fill_prelisting=qmt_fill_prelisting,
            prelisting_prices=prelisting_prices,
            prelisting_use_first_close=prelisting_use_first_close,
            qmt_min_listed_sessions=qmt_min_listed_sessions,
            exclude_unknown_industries=exclude_unknown_industries,
            fundamental_mask=fundamental_mask,
        )
    execution = ExecutionConfig(
        commission_rate, stamp_rate, minimum_commission, qmt_selected_state
    )
    return run_strategy_backtest(
        calendar, codes, opens, closes, amounts, industries, strategy,
        start_date, end_date, portfolio, execution, risk.cooldown_days,
    )


def parse_segment(text):
    parts = text.split(':')
    if len(parts) != 3 or not parts[0].strip():
        raise ValueError('segment must be NAME:YYYYMMDD:YYYYMMDD')
    try:
        start, end = int(parts[1]), int(parts[2])
        dt.datetime.strptime(str(start), '%Y%m%d')
        dt.datetime.strptime(str(end), '%Y%m%d')
    except ValueError as exc:
        raise ValueError(f'invalid segment date in {text!r}') from exc
    if start > end:
        raise ValueError(f'segment start is after end in {text!r}')
    return parts[0].strip(), start, end


def quarterly_segments(start, end):
    start_date = dt.datetime.strptime(str(start), '%Y%m%d').date()
    end_date = dt.datetime.strptime(str(end), '%Y%m%d').date()
    cursor = dt.date(start_date.year, ((start_date.month - 1) // 3) * 3 + 1, 1)
    result = []
    while cursor <= end_date:
        next_month = cursor.month + 3
        next_year = cursor.year + (next_month - 1) // 12
        next_month = (next_month - 1) % 12 + 1
        next_quarter = dt.date(next_year, next_month, 1)
        quarter_end = next_quarter - dt.timedelta(days=1)
        clipped_start = max(start_date, cursor)
        clipped_end = min(end_date, quarter_end)
        result.append((
            f'{cursor.year}Q{(cursor.month - 1) // 3 + 1}',
            int(clipped_start.strftime('%Y%m%d')),
            int(clipped_end.strftime('%Y%m%d')),
        ))
        cursor = next_quarter
    return result


def benchmark_period(cache_path, code, price_key, start, end):
    with np.load(cache_path, allow_pickle=False) as data:
        required = {'calendar', 'codes', price_key}
        missing = sorted(required - set(data.files))
        if missing:
            raise ValueError(f'benchmark cache is missing fields: {missing}')
        calendar = data['calendar']
        codes = data['codes'].tolist()
        if code not in codes:
            raise KeyError(f'benchmark code is absent from cache: {code}')
        values = data[price_key]
        j = codes.index(code)
        series = values[:, j] if values.ndim == 2 else values
    inside = (
        (calendar >= start) & (calendar <= end) &
        np.isfinite(series) & (series > 0)
    )
    indices = np.flatnonzero(inside)
    if len(indices) < 2:
        raise ValueError(f'benchmark has fewer than two valid observations in {start}..{end}')
    first, last = int(indices[0]), int(indices[-1])
    return {
        'code': code,
        'price_key': price_key,
        'start_date': int(calendar[first]),
        'end_date': int(calendar[last]),
        'start_value': float(series[first]),
        'end_value': float(series[last]),
        'return': float(series[last] / series[first] - 1.0),
    }


def benchmark_qmt_daily(datadir, code, start, end):
    symbol, market = code.upper().split('.', 1)
    if market not in {'SH', 'SZ'}:
        raise ValueError(f'unsupported benchmark market suffix: {code}')
    path = Path(datadir) / market / '86400' / f'{symbol}.DAT'
    if not path.exists():
        raise FileNotFoundError(f'QMT benchmark daily DAT does not exist: {path}')
    bars = read_minute_bars(path)
    dates = np.asarray([
        int(time.strftime('%Y%m%d', time.localtime(int(stamp))))
        for stamp in bars.timestamp
    ], dtype=np.int32)
    values = np.asarray(bars.close, dtype=np.float64)
    inside = (
        (dates >= start) & (dates <= end) &
        np.isfinite(values) & (values > 0)
    )
    indices = np.flatnonzero(inside)
    if len(indices) < 2:
        raise ValueError(f'QMT benchmark has fewer than two valid observations in {start}..{end}')
    first, last = int(indices[0]), int(indices[-1])
    return {
        'code': code.upper(),
        'price_key': 'QMT_SH_86400_close' if market == 'SH' else 'QMT_SZ_86400_close',
        'path': str(path.resolve()),
        'start_date': int(dates[first]),
        'end_date': int(dates[last]),
        'start_value': float(values[first]),
        'end_value': float(values[last]),
        'return': float(values[last] / values[first] - 1.0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cache', required=True)
    ap.add_argument('--industry-dir', required=True)
    ap.add_argument(
        '--execution-price-key', default='opens',
        help='NPZ field used as the execution price; full-A 5m cache uses exec_0935',
    )
    ap.add_argument(
        '--exclude-unknown-industries', action='store_true',
        help='observe but do not select securities absent from every SW1 membership file',
    )
    ap.add_argument(
        '--fundamental-cache',
        help='point-in-time NPZ produced by build_qmt_financial_cache.py',
    )
    ap.add_argument(
        '--fundamental-rule', action='append', default=[],
        help=('repeatable selection rule such as '
              'PERSHAREINDEX.du_return_on_equity>=8; all rules are ANDed'),
    )
    ap.add_argument(
        '--fundamental-missing', choices=('exclude', 'allow'), default='exclude',
        help='whether a stock without a currently announced value passes a rule',
    )
    ap.add_argument('--start', type=int, default=20260101)
    ap.add_argument('--end', type=int, default=20260710)
    ap.add_argument(
        '--initial-cash', type=float,
        help=('cash-only starting balance, or an explicit cash override when '
              '--initial-portfolio is supplied; defaults to 500000'),
    )
    ap.add_argument(
        '--initial-portfolio',
        help='JSON containing cash, positions and optional QMT selected state',
    )
    ap.add_argument('--commission-rate', type=float, default=0.000285)
    ap.add_argument('--stamp-rate', type=float, default=0.00025)
    ap.add_argument('--minimum-commission', type=float, default=5.0)
    ap.add_argument('--entry-breadth', type=float, default=None)
    ap.add_argument('--score', default=None)
    ap.add_argument('--topk', type=int, default=None)
    ap.add_argument('--buffer', type=float, default=None)
    ap.add_argument('--max-industry', type=int, default=None)
    ap.add_argument('--trend-ma', type=int, default=None)
    ap.add_argument('--exit-breadth', type=float, default=None)
    ap.add_argument('--output', default='qmt_cuda_backtest/results_broad_ensemble')
    ap.add_argument('--production-only', action='store_true')
    ap.add_argument(
        '--segment', action='append', default=[], metavar='NAME:START:END',
        help='repeatable independent evaluation period; no dates are hard-coded',
    )
    ap.add_argument(
        '--quarterly-segments', action='store_true',
        help='evaluate every calendar quarter intersecting --start/--end',
    )
    ap.add_argument('--benchmark-cache', help='standard NPZ containing benchmark prices')
    ap.add_argument(
        '--benchmark-qmt-datadir',
        help='QMT datadir; reads MARKET/86400/SYMBOL.DAT directly',
    )
    ap.add_argument('--benchmark-code', default='000688.SH')
    ap.add_argument('--benchmark-name', default='科创50')
    ap.add_argument('--benchmark-price-key', default='closes')
    ap.add_argument(
        '--qmt-selected-state', action='store_true',
        help=('mirror QMT ContextInfo.selected separately from actual shares and '
              'submit target orders in the strategy call order'),
    )
    ap.add_argument(
        '--qmt-fill-prelisting', action='store_true',
        help=('for QMT compatibility, fill a listed stock pre-listing score '
              'history; defaults to its first available 09:35 price'),
    )
    ap.add_argument(
        '--qmt-prelisting-price-file',
        help=('JSON produced by build_adjusted_issue_prices.py; when supplied, '
              'use adjusted IPO issue prices instead of first opening prices'),
    )
    ap.add_argument(
        '--qmt-prelisting-use-first-close', action='store_true',
        help='fill pre-listing score history with the first adjusted close',
    )
    ap.add_argument(
        '--qmt-min-listed-sessions', type=int, default=0,
        help='minimum real sessions through the signal date for a filled stock',
    )
    args = ap.parse_args()
    if args.qmt_fill_prelisting and not args.qmt_selected_state:
        ap.error('--qmt-fill-prelisting requires --qmt-selected-state')
    if args.qmt_prelisting_price_file and not args.qmt_fill_prelisting:
        ap.error('--qmt-prelisting-price-file requires --qmt-fill-prelisting')
    if args.qmt_prelisting_use_first_close and not args.qmt_fill_prelisting:
        ap.error('--qmt-prelisting-use-first-close requires --qmt-fill-prelisting')
    if args.qmt_prelisting_price_file and args.qmt_prelisting_use_first_close:
        ap.error('choose either --qmt-prelisting-price-file or --qmt-prelisting-use-first-close')
    if args.qmt_min_listed_sessions < 0:
        ap.error('--qmt-min-listed-sessions must be non-negative')
    if args.fundamental_rule and not args.fundamental_cache:
        ap.error('--fundamental-rule requires --fundamental-cache')
    if args.benchmark_cache and args.benchmark_qmt_datadir:
        ap.error('choose either --benchmark-cache or --benchmark-qmt-datadir')
    if args.start > args.end:
        ap.error('--start must not be after --end')
    try:
        dt.datetime.strptime(str(args.start), '%Y%m%d')
        dt.datetime.strptime(str(args.end), '%Y%m%d')
    except ValueError:
        ap.error('--start and --end must be valid YYYYMMDD dates')
    try:
        segments = [parse_segment(text) for text in args.segment]
        if args.quarterly_segments:
            segments.extend(quarterly_segments(args.start, args.end))
    except ValueError as exc:
        ap.error(str(exc))
    segment_names = [name for name, _, _ in segments]
    if len(segment_names) != len(set(segment_names)):
        ap.error('segment names must be unique')
    outside = [
        name for name, start, end in segments
        if start < args.start or end > args.end
    ]
    if outside:
        ap.error(f'segments must stay inside --start/--end: {outside}')
    try:
        if args.initial_portfolio:
            initial_portfolio = load_initial_portfolio(
                Path(args.initial_portfolio), args.initial_cash
            )
        else:
            initial_portfolio = cash_portfolio(
                500000.0 if args.initial_cash is None else args.initial_cash
            )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        ap.error(f'invalid initial portfolio: {exc}')
    prelisting_prices = None
    if args.qmt_prelisting_price_file:
        price_rows = json.loads(Path(args.qmt_prelisting_price_file).read_text(encoding='utf-8'))
        prelisting_prices = {
            code: float(row['issue_price_adjusted'])
            for code, row in price_rows.items()
            if isinstance(row, dict) and row.get('issue_price_adjusted')
        }
    calendar,codes,opens,closes,amounts = load_backtest_cache(
        Path(args.cache), args.execution_price_key
    )
    fundamental_mask = None
    fundamental_stats = None
    if args.fundamental_cache:
        try:
            fundamental_rules = [parse_rule(text) for text in args.fundamental_rule]
            fundamental_data = load_fundamental_cache(Path(args.fundamental_cache))
            fundamental_mask, fundamental_stats = build_rule_mask(
                calendar, codes, fundamental_data, fundamental_rules,
                missing=args.fundamental_missing,
            )
        except (KeyError, ValueError) as exc:
            ap.error(str(exc))
    industries = load_industries(Path(args.industry_dir), codes)
    benchmark = None
    segment_benchmarks = {}
    if args.benchmark_cache or args.benchmark_qmt_datadir:
        try:
            if args.benchmark_qmt_datadir:
                benchmark_loader = lambda start, end: benchmark_qmt_daily(
                    Path(args.benchmark_qmt_datadir), args.benchmark_code, start, end
                )
            else:
                benchmark_loader = lambda start, end: benchmark_period(
                    Path(args.benchmark_cache), args.benchmark_code,
                    args.benchmark_price_key, start, end,
                )
            benchmark = benchmark_loader(args.start, args.end)
            for name, start, end in segments:
                segment_benchmarks[name] = benchmark_loader(start, end)
        except (OSError, KeyError, ValueError) as exc:
            ap.error(f'invalid benchmark data: {exc}')
    unknown_codes = [
        code for code, industry in zip(codes, industries)
        if industry.startswith('UNKNOWN_')
    ]
    production = Params('ensemble', 0, 5, 2.0, 1, 20, 0.50)
    if any(value is not None for value in (
            args.score, args.topk, args.buffer, args.max_industry,
            args.trend_ma, args.exit_breadth)):
        production = Params(
            production.score if args.score is None else args.score,
            production.skip,
            production.topk if args.topk is None else args.topk,
            production.buffer if args.buffer is None else args.buffer,
            production.max_industry if args.max_industry is None else args.max_industry,
            production.trend_ma if args.trend_ma is None else args.trend_ma,
            production.min_breadth if args.exit_breadth is None else args.exit_breadth,
        )
    grid = [Params(score, skip, topk, buffer, cap, ma, breadth)
            for score in ('ensemble', 'risk_ensemble', 'acceleration')
            for skip in (0, 3)
            for topk in (5, 8, 10)
            for buffer in (1.5, 2.0, 3.0)
            for cap in (1, 2, 3)
            for ma in (20, 60)
            for breadth in (0.0, .45, .55)]
    if args.production_only:
        grid = [production]
    rows=[]
    for p in grid:
        risk=RiskControls(entry_breadth=(p.min_breadth if args.entry_breadth is None else args.entry_breadth))
        def run_period(period_start, period_end):
            return backtest(
                calendar, codes, opens, closes, amounts, industries, p,
                period_start, period_end,
                commission_rate=args.commission_rate,
                stamp_rate=args.stamp_rate,
                minimum_commission=args.minimum_commission,
                risk=risk,
                qmt_selected_state=args.qmt_selected_state,
                qmt_fill_prelisting=args.qmt_fill_prelisting,
                prelisting_prices=prelisting_prices,
                prelisting_use_first_close=args.qmt_prelisting_use_first_close,
                qmt_min_listed_sessions=args.qmt_min_listed_sessions,
                exclude_unknown_industries=args.exclude_unknown_industries,
                fundamental_mask=fundamental_mask,
                initial_portfolio=initial_portfolio,
            )
        full = run_period(args.start, args.end)
        segment_results = {
            name: run_period(start, end) for name, start, end in segments
        }
        rows.append((p, full, segment_results))
    rows.sort(
        key=lambda x: (
            min((result['return'] for result in x[2].values()), default=x[1]['return']),
            x[1]['return'],
        ),
        reverse=True,
    )
    p,full,selected_segments=rows[0]
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    with (out/'grid.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f)
        w.writerow([
            'score','skip','topk','buffer','max_industry','trend_ma','min_breadth',
            'return','max_drawdown',
            *[f'{name}_return' for name, _, _ in segments],
            'trades',
        ])
        for x, result, segment_results in rows:
            w.writerow([
                x.score,x.skip,x.topk,x.buffer,x.max_industry,x.trend_ma,x.min_breadth,
                result['return'],result['max_drawdown'],
                *[segment_results[name]['return'] for name, _, _ in segments],
                len(result['trades']),
            ])
    with (out/'trades.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f);w.writerow(['date','code','industry','side','price','shares','score','breadth','fee']);w.writerows(full['trades'])
    with (out/'equity.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f);w.writerow(['date','equity','breadth','target_count']);w.writerows(full['equity'])
    with (out/'state.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f);w.writerow(['date','selected_count','actual_count','selected_not_held',
                                    'held_not_selected','selected','actual']);w.writerows(full['state'])
    ending_payload = {
        'cash': full['final_cash'],
        'positions': [
            {'code': code, **position}
            for code, position in full['ending_positions'].items()
        ],
        'selected': full['ending_selected'],
        'source_end_date': args.end,
        'engine_version': full['engine_version'],
    }
    (out/'ending_portfolio.json').write_text(
        json.dumps(ending_payload,ensure_ascii=False,indent=2),encoding='utf-8'
    )
    known_industries = {x for x in industries if not x.startswith('UNKNOWN_')}
    initial_positions_summary = [
        {'code': position.code, 'shares': position.shares,
         'average_cost': position.average_cost}
        for position in initial_portfolio.positions
    ]
    segment_summary = {
        name: {
            'start': start,
            'end': end,
            'return': selected_segments[name]['return'],
            'max_drawdown': selected_segments[name]['max_drawdown'],
            'benchmark': segment_benchmarks.get(name),
        }
        for name, start, end in segments
    }
    summary={'universe':len(codes),'industry_count':len(known_industries),
             'industry_dir':str(Path(args.industry_dir).resolve()),
             'unknown_industry_count':len(unknown_codes),
             'unknown_industry_codes':unknown_codes,
             'exclude_unknown_industries':args.exclude_unknown_industries,
             'execution_price_key':args.execution_price_key,
             'initial_cash':initial_portfolio.cash,
             'initial_positions':initial_positions_summary,
             'initial_selected':initial_portfolio.selected,
             'initial_equity':full['initial_equity'],
             'final_equity':full['final_equity'],
             'final_cash':full['final_cash'],
             'ending_positions':full['ending_positions'],
             'commission_rate':args.commission_rate,'stamp_rate':args.stamp_rate,
             'minimum_commission':args.minimum_commission,
             'qmt_selected_state':args.qmt_selected_state,
             'qmt_fill_prelisting':args.qmt_fill_prelisting,
              'qmt_prelisting_price_source':('ipo_issue_price' if prelisting_prices else
                                             'first_available_close' if args.qmt_prelisting_use_first_close else
                                             f'first_available_{args.execution_price_key}'
                                             if args.qmt_fill_prelisting else None),
             'qmt_min_listed_sessions':args.qmt_min_listed_sessions,
             'fundamental_cache':(str(Path(args.fundamental_cache).resolve())
                                  if args.fundamental_cache else None),
             'fundamental_rules':args.fundamental_rule,
             'fundamental_missing':args.fundamental_missing,
             'fundamental_stats':fundamental_stats,
             'risk_controls':risk.__dict__,
             'strategy_name':full['strategy_name'],
             'engine_version':full['engine_version'],
             'engine_sha256':full['engine_sha256'],
             'benchmark':({
                 'name':args.benchmark_name,
                 'source_type':('qmt_daily_dat' if args.benchmark_qmt_datadir else
                                'standard_npz'),
                 'source':(str(Path(args.benchmark_qmt_datadir).resolve())
                           if args.benchmark_qmt_datadir else
                           str(Path(args.benchmark_cache).resolve())),
                 **benchmark,
             } if benchmark is not None else None),
             'segments':segment_summary,
             'params':p.__dict__,'return':full['return'],'max_drawdown':full['max_drawdown'],
             'trade_rows':len(full['trades']),'grid_size':len(rows)}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

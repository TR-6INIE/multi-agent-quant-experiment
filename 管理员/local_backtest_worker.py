from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from experiment_core.local_backtest import (
    SafeMinuteAccount,
    SafeMinuteBar,
    SafeMinuteOrder,
    SafeMinuteStrategyContext,
    SafeStrategyContext,
    SafeStrategyDecision,
    file_sha256,
    readonly_copy,
)
from experiment_core.fundamentals import (
    PointInTimeFundamentalProvider,
    empty_fundamental_snapshot,
)
from experiment_core.market_snapshots import (
    load_or_build_snapshots,
    source_manifest,
    spec_to_dict,
    strategy_data_spec,
    strategy_fundamental_fields,
)


def load_candidate(path: Path):
    name = 'experiment_candidate_' + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError('cannot create candidate module spec')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, 'create_strategy', None)
    if not callable(factory):
        raise TypeError('candidate must define callable create_strategy()')
    strategy = factory()
    if not isinstance(getattr(strategy, 'name', None), str):
        raise TypeError('strategy.name must be a string')
    engine_mode = str(getattr(strategy, 'engine_mode', 'daily')).strip().lower()
    if engine_mode == 'minute':
        if not callable(getattr(strategy, 'on_minute', None)):
            raise TypeError('minute strategy must define on_minute(context)')
    elif engine_mode == 'daily':
        if not callable(getattr(strategy, 'ready', None)):
            raise TypeError('daily strategy must define ready(context)')
        if not callable(getattr(strategy, 'decide', None)):
            raise TypeError('daily strategy must define decide(context)')
    else:
        raise ValueError('strategy.engine_mode must be daily or minute')
    return strategy


def validate_decision(raw, code_count: int) -> SafeStrategyDecision:
    if isinstance(raw, SafeStrategyDecision):
        decision = raw
    elif isinstance(raw, dict):
        decision = SafeStrategyDecision(
            desired=tuple(raw.get('desired', ())),
            scores=np.asarray(raw.get('scores'), dtype=float),
            breadth=float(raw.get('breadth')),
            target_slots=int(raw.get('target_slots')),
            stopped=frozenset(raw.get('stopped', ())),
        )
    else:
        raise TypeError('decide() must return SafeStrategyDecision or a dict')
    if decision.scores.shape != (code_count,):
        raise ValueError('scores must have shape (%d,)' % code_count)
    if not np.isfinite(decision.breadth) or not 0.0 <= decision.breadth <= 1.0:
        raise ValueError('breadth must be finite and between 0 and 1')
    if decision.target_slots <= 0:
        raise ValueError('target_slots must be positive')
    desired = tuple(int(x) for x in decision.desired)
    if len(desired) != len(set(desired)):
        raise ValueError('desired contains duplicate indices')
    if any(x < 0 or x >= code_count for x in desired):
        raise ValueError('desired contains an out-of-range index')
    stopped = frozenset(int(x) for x in decision.stopped)
    if any(x < 0 or x >= code_count for x in stopped):
        raise ValueError('stopped contains an out-of-range index')
    scores = readonly_copy(decision.scores)
    return SafeStrategyDecision(
        desired, scores, float(decision.breadth), decision.target_slots, stopped
    )


def annualize(total_return: float, sessions: int) -> float:
    if sessions <= 0:
        raise ValueError('sessions must be positive')
    if total_return <= -1.0:
        return -1.0
    return (1.0 + total_return) ** (252.0 / sessions) - 1.0


def aggregate_directory_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(x for x in path.rglob('*') if x.is_file()):
        digest.update(str(item.relative_to(path)).replace('\\', '/').encode('utf-8'))
        digest.update(b'\0')
        digest.update(hashlib.sha256(item.read_bytes()).digest())
    return digest.hexdigest()


class MinuteCandidateAdapter:
    def __init__(
        self, candidate, order_class, codes, max_orders_per_bar,
        daily_calendar=None, daily_closes=None, daily_amounts=None,
        industries=None, fundamental_provider=None,
    ):
        self.candidate = candidate
        self.order_class = order_class
        self.codes = tuple(str(code) for code in codes)
        self.code_set = frozenset(self.codes)
        self.max_orders_per_bar = int(max_orders_per_bar)
        self.name = candidate.name
        self.full_daily_calendar = daily_calendar
        self.full_daily_closes = daily_closes
        self.full_daily_amounts = daily_amounts
        self.industries = tuple(
            str(value) for value in (
                industries if industries is not None else ()
            )
        )
        self.fundamental_provider = fundamental_provider
        self.daily_context_date = None
        self.daily_calendar = np.empty(0, dtype=np.int64)
        self.daily_closes = np.empty((0, len(self.codes)))
        self.daily_amounts = np.empty((0, len(self.codes)))
        self.fundamental_snapshot = empty_fundamental_snapshot(len(self.codes))

    def _refresh_daily_context(self, current_date):
        if self.daily_context_date == current_date:
            return
        self.daily_context_date = current_date
        if self.full_daily_calendar is None:
            return
        stop = int(np.searchsorted(self.full_daily_calendar, current_date))
        self.daily_calendar = readonly_copy(self.full_daily_calendar[:stop])
        self.daily_closes = readonly_copy(self.full_daily_closes[:stop])
        self.daily_amounts = readonly_copy(self.full_daily_amounts[:stop])
        if self.fundamental_provider is not None:
            self.fundamental_snapshot = (
                self.fundamental_provider.snapshot_before(current_date)
            )

    def on_minute(self, context):
        bar = context.bar
        self._refresh_daily_context(int(bar.date))
        safe_bar = SafeMinuteBar(
            timestamp=int(bar.timestamp),
            date=int(bar.date),
            hhmm=int(bar.hhmm),
            codes=self.codes,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            amount=bar.amount,
            prev_close=bar.prev_close,
        )
        account = context.account
        safe_account = SafeMinuteAccount(
            cash=float(account.cash),
            equity=float(account.equity),
            shares=account.shares,
            sellable_shares=account.sellable_shares,
            average_cost=account.average_cost,
        )
        safe_context = SafeMinuteStrategyContext(
            bar=safe_bar,
            candidates=frozenset(context.candidates),
            pending_codes=frozenset(context.pending_codes),
            account=safe_account,
            daily_calendar=self.daily_calendar,
            daily_closes=self.daily_closes,
            daily_amounts=self.daily_amounts,
            industries=self.industries,
            fundamental_fields=self.fundamental_snapshot.fields,
            fundamentals=self.fundamental_snapshot.values,
            fundamental_report_dates=self.fundamental_snapshot.report_dates,
            fundamental_available_dates=(
                self.fundamental_snapshot.available_dates
            ),
            fundamental_cutoff_date=self.fundamental_snapshot.cutoff_date,
        )
        raw = self.candidate.on_minute(safe_context)
        if raw is None:
            return []
        orders = list(raw)
        if len(orders) > self.max_orders_per_bar:
            raise ValueError(
                'minute strategy returned %d orders; limit is %d'
                % (len(orders), self.max_orders_per_bar)
            )
        result = []
        seen = set()
        for item in orders:
            if isinstance(item, SafeMinuteOrder):
                code, kind, value, reason = (
                    item.code, item.kind, item.value, item.reason
                )
            elif isinstance(item, dict):
                code = item.get('code')
                kind = item.get('kind')
                value = item.get('value', 0)
                reason = item.get('reason', '')
            else:
                raise TypeError('minute orders must be SafeMinuteOrder or dict')
            code = str(code or '').strip().upper()
            kind = str(kind or '').strip().lower()
            reason = str(reason or '')[:200]
            if code not in self.code_set:
                raise ValueError('minute order code is outside the fixed universe: %s' % code)
            if code in seen:
                raise ValueError('minute strategy returned duplicate code in one bar: %s' % code)
            if kind not in ('target_value', 'target_shares', 'close'):
                raise ValueError('unsupported minute order kind: %s' % kind)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError('minute order value must be numeric')
            if not np.isfinite(float(value)) or float(value) < 0:
                raise ValueError('minute order value must be finite and non-negative')
            if kind == 'target_shares' and int(value) != float(value):
                raise ValueError('target_shares value must be an integer')
            seen.add(code)
            result.append(self.order_class(code, kind, value, reason))
        return result


def run_minute_mode(
    args, strategy, candidate_path, cache_path, industry_dir, output,
    calendar, codes, closes, amounts, benchmark_qmt_daily, load_industries,
    load_initial_portfolio, cash_portfolio, fundamental_provider,
    fundamental_cache_path,
):
    from qmt_cuda_backtest.frozen_minute_trading_engine import (
        ENGINE_VERSION as MINUTE_ENGINE_VERSION,
        MinuteExecutionConfig,
        engine_sha256 as minute_engine_sha256,
    )
    from qmt_cuda_backtest.minute_backtest_runner import (
        CandidateSchedule,
        run_minute_backtest,
    )
    from qmt_cuda_backtest.minute_strategy_interface import MinuteOrder
    from qmt_cuda_backtest.qmt_minute_stream import QmtMinuteStream

    code_list = [str(code) for code in codes]
    industries = load_industries(industry_dir, codes)
    if args.initial_portfolio:
        initial_path = Path(args.initial_portfolio).resolve()
        portfolio = load_initial_portfolio(initial_path)
        initial_record = json.loads(initial_path.read_text(encoding='utf-8-sig'))
    else:
        portfolio = cash_portfolio(args.initial_cash)
        initial_record = {'cash': float(args.initial_cash), 'positions': [], 'selected': []}
    adapter = MinuteCandidateAdapter(
        strategy, MinuteOrder, code_list, args.max_minute_orders_per_bar,
        calendar, closes, amounts, industries,
        fundamental_provider,
    )
    stream = QmtMinuteStream(
        Path(args.qmt_datadir).resolve(), code_list, args.start, args.end
    )
    execution = MinuteExecutionConfig(
        commission_rate=args.commission_rate,
        stamp_rate=args.stamp_rate,
        minimum_commission=args.minimum_commission,
        max_volume_participation=args.max_volume_participation,
    )
    result = run_minute_backtest(
        stream, adapter, CandidateSchedule(code_list), portfolio, execution
    )
    daily_rows = result['daily_equity']
    if not daily_rows:
        raise ValueError('minute backtest produced no daily equity rows')
    trading_sessions = len(daily_rows)
    total_return = float(result['return'])
    benchmark = benchmark_qmt_daily(
        Path(args.benchmark_qmt_datadir), args.benchmark_code, args.start, args.end
    )
    benchmark_total = float(benchmark['return'])
    ending_positions = [
        {
            'code': code,
            'stock': code,
            'shares': int(row['shares']),
            'volume': int(row['shares']),
            'average_cost': row.get('average_cost'),
        }
        for code, row in result['ending_positions'].items()
    ]
    ending_selected = [row['code'] for row in ending_positions]
    ending_portfolio = {
        'cash': float(result['final_cash']),
        'positions': ending_positions,
        'selected': ending_selected,
        'source_end_date': args.end,
        'engine_version': MINUTE_ENGINE_VERSION,
    }
    filled = [
        row for row in result['orders'] if row[11] in ('FILLED', 'PARTIAL')
    ]
    raw_manifest = source_manifest(
        Path(args.qmt_datadir).resolve(), code_list, '1m'
    )
    summary = {
        'period_start': str(args.start),
        'period_end': str(args.end),
        'trading_sessions': trading_sessions,
        'initial_asset': float(result['initial_equity']),
        'ending_asset': float(result['final_equity']),
        'strategy_total_return': total_return,
        'strategy_annualized_return': annualize(total_return, trading_sessions),
        'benchmark_name': args.benchmark_name,
        'benchmark_total_return': benchmark_total,
        'benchmark_annualized_return': annualize(benchmark_total, trading_sessions),
        'max_drawdown': float(result['max_drawdown']),
        'trade_count': len(filled),
        'order_count': len(result['orders']),
        'signals_submitted': int(result['signals_submitted']),
        'bars_processed': int(result['bars_processed']),
        'pending_at_end': result['pending_at_end'],
        'cash': float(result['final_cash']),
        'starting_positions': initial_record.get('positions', []),
        'ending_positions': ending_positions,
        'ending_selected': ending_selected,
        'commission_rate': args.commission_rate,
        'stamp_rate': args.stamp_rate,
        'minimum_commission': args.minimum_commission,
        'max_volume_participation': args.max_volume_participation,
        'candidate_sha256': file_sha256(candidate_path),
        'engine_version': MINUTE_ENGINE_VERSION,
        'engine_sha256': minute_engine_sha256(),
        'cache_sha256': raw_manifest['metadata_sha256'],
        'minute_source_manifest': raw_manifest,
        'industry_snapshot_sha256': aggregate_directory_sha256(industry_dir),
        'strategy_name': strategy.name,
        'strategy_data_spec': {
            'engine_mode': 'minute',
            'signal_data': (
                'completed_current_1m_OHLCVA plus daily history ending at '
                'the previous session'
            ),
            'execution': 'next_global_1m_bar_open',
            'max_orders_per_bar': args.max_minute_orders_per_bar,
            'max_volume_participation': args.max_volume_participation,
            'fundamental_fields': (
                list(fundamental_provider.fields)
                if fundamental_provider is not None else []
            ),
        },
        'future_data_guard': (
            'strategy sees only the fully completed current minute and daily '
            'history ending at the previous session; orders are '
            'processed once at the next global one-minute bar open; financial '
            'values include only announcements available by the previous session'
        ),
        'snapshot_bias_disclosure': (
            'Current fixed universe is intentionally used; survivorship bias remains.'
        ),
        'stream_coverage': stream.coverage.__dict__,
        'known_limitations': [
            'historical ST 5% price limits are not identified',
            'IPO initial no-limit sessions are not identified',
            'cash dividends and share distributions are not posted to the minute account',
        ],
    }
    if fundamental_provider is not None:
        summary['fundamental_provenance'] = fundamental_provider.provenance()
        summary['fundamental_cache_sha256'] = file_sha256(
            fundamental_cache_path
        )
    (output / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    (output / 'ending_portfolio.json').write_text(
        json.dumps(ending_portfolio, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    order_header = [
        'signal_timestamp', 'execution_timestamp', 'date', 'hhmm', 'code',
        'order_kind', 'side', 'requested_shares', 'filled_shares', 'price',
        'fee', 'status', 'signal_reason', 'execution_reason',
    ]
    with (output / 'orders.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle); writer.writerow(order_header); writer.writerows(result['orders'])
    with (output / 'trades.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle); writer.writerow(order_header); writer.writerows(filled)
    with (output / 'equity_minute.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle); writer.writerow(['timestamp', 'date', 'hhmm', 'total_asset']); writer.writerows(result['minute_equity'])
    with (output / 'equity.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle); writer.writerow(['date', 'total_asset']); writer.writerows(daily_rows)
    with (output / 'state.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle); writer.writerow(['date', 'total_asset', 'note'])
        writer.writerows([row + ['minute_engine_daily_close'] for row in daily_rows])
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--candidate', required=True)
    ap.add_argument('--package-root', required=True)
    ap.add_argument('--cache', required=True)
    ap.add_argument('--industry-dir', required=True)
    ap.add_argument('--start', type=int, required=True)
    ap.add_argument('--end', type=int, required=True)
    ap.add_argument('--initial-cash', type=float, default=500000.0)
    ap.add_argument('--initial-portfolio')
    ap.add_argument('--commission-rate', type=float, default=0.000285)
    ap.add_argument('--stamp-rate', type=float, default=0.00025)
    ap.add_argument('--minimum-commission', type=float, default=5.0)
    ap.add_argument('--qmt-selected-state', action='store_true')
    ap.add_argument('--benchmark-qmt-datadir', required=True)
    ap.add_argument('--benchmark-code', default='000688.SH')
    ap.add_argument('--benchmark-name', default='科创50')
    ap.add_argument('--qmt-datadir', required=True)
    ap.add_argument('--snapshot-cache-dir', required=True)
    ap.add_argument('--max-signal-times', type=int, default=4)
    ap.add_argument('--max-intraday-lookback-sessions', type=int, default=80)
    ap.add_argument('--max-minute-orders-per-bar', type=int, default=50)
    ap.add_argument('--max-volume-participation', type=float, default=0.10)
    ap.add_argument('--fundamental-cache')
    ap.add_argument('--max-fundamental-fields', type=int, default=8)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()

    package_root = Path(args.package_root).resolve()
    sys.path.insert(0, str(package_root))
    from qmt_cuda_backtest.broad_ensemble_industry_backtest import (
        benchmark_qmt_daily,
        load_backtest_cache,
        load_industries,
    )
    from qmt_cuda_backtest.frozen_daily_trading_engine import (
        ENGINE_VERSION,
        ExecutionConfig,
        FrozenDailyTradingEngine,
        cash_portfolio,
        engine_sha256,
        load_initial_portfolio,
    )
    from qmt_cuda_backtest.fundamental_store import load_fundamental_cache

    candidate_path = Path(args.candidate).resolve()
    cache_path = Path(args.cache).resolve()
    industry_dir = Path(args.industry_dir).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    strategy = load_candidate(candidate_path)
    calendar, codes, execution_prices, closes, amounts = load_backtest_cache(
        cache_path, 'opens'
    )
    fundamental_cache_path = (
        Path(args.fundamental_cache).resolve()
        if args.fundamental_cache else None
    )
    fundamental_data = (
        load_fundamental_cache(fundamental_cache_path)
        if fundamental_cache_path is not None else None
    )
    requested_fundamental_fields = strategy_fundamental_fields(
        strategy,
        fundamental_data.fields if fundamental_data is not None else (),
        args.max_fundamental_fields,
    )
    fundamental_provider = (
        PointInTimeFundamentalProvider(
            calendar, [str(code) for code in codes], fundamental_data,
            requested_fundamental_fields,
        )
        if fundamental_data is not None and requested_fundamental_fields else None
    )
    if str(getattr(strategy, 'engine_mode', 'daily')).strip().lower() == 'minute':
        return run_minute_mode(
            args, strategy, candidate_path, cache_path, industry_dir, output,
            calendar, codes, closes, amounts, benchmark_qmt_daily, load_industries,
            load_initial_portfolio, cash_portfolio,
            fundamental_provider, fundamental_cache_path,
        )
    data_spec = strategy_data_spec(strategy, {
        'max_signal_times': args.max_signal_times,
        'max_intraday_lookback_sessions': args.max_intraday_lookback_sessions,
        'available_fundamental_fields': (
            fundamental_data.fields if fundamental_data is not None else ()
        ),
        'max_fundamental_fields': args.max_fundamental_fields,
    })
    industries = load_industries(industry_dir, codes)
    snapshot_path = None
    snapshot_metadata = None
    signal_times = data_spec.signal_times
    if (
        data_spec.signal_period == '1d'
        and data_spec.execution_period == '5m'
        and data_spec.execution_time == '09:35'
    ):
        execution_matrix = execution_prices
        signal_close = None
        signal_amount = None
    else:
        requested_times = tuple(signal_times) + (data_spec.execution_time, '15:00')
        snapshot_close, snapshot_amount, snapshot_metadata, snapshot_path = (
            load_or_build_snapshots(
                package_root,
                Path(args.qmt_datadir).resolve(),
                Path(args.snapshot_cache_dir).resolve(),
                calendar,
                codes,
                data_spec.execution_period,
                requested_times,
            )
        )
        stored_times = tuple(snapshot_metadata['identity']['times'])
        close_1500 = snapshot_close[:, stored_times.index('15:00'), :]
        scale = np.divide(
            closes,
            close_1500,
            out=np.full(closes.shape, np.nan, dtype=float),
            where=np.isfinite(close_1500) & (close_1500 > 0),
        )
        snapshot_close = snapshot_close * scale[:, None, :]
        execution_matrix = snapshot_close[
            :, stored_times.index(data_spec.execution_time), :
        ]
        if signal_times:
            signal_indices = [stored_times.index(value) for value in signal_times]
            signal_close = snapshot_close[:, signal_indices, :]
            signal_amount = snapshot_amount[:, signal_indices, :]
        else:
            signal_close = None
            signal_amount = None
    if args.initial_portfolio:
        initial_portfolio_path = Path(args.initial_portfolio).resolve()
        portfolio = load_initial_portfolio(initial_portfolio_path)
        initial_portfolio_record = json.loads(
            initial_portfolio_path.read_text(encoding='utf-8-sig')
        )
    else:
        portfolio = cash_portfolio(args.initial_cash)
        initial_portfolio_record = {'cash': float(args.initial_cash), 'positions': [], 'selected': []}
    config = ExecutionConfig(
        args.commission_rate, args.stamp_rate, args.minimum_commission,
        args.qmt_selected_state,
    )
    broker = FrozenDailyTradingEngine(codes, industries, config, portfolio)
    first = int(np.searchsorted(calendar, args.start))
    last = int(np.searchsorted(calendar, args.end, side='right'))
    if first >= last:
        raise ValueError('no sessions in requested evaluation period')
    marks = np.array(closes[first - 1] if first > 0 else execution_prices[first], copy=True)
    fallback = execution_matrix[first]
    invalid = ~np.isfinite(marks) | (marks <= 0)
    marks[invalid] = fallback[invalid]
    broker.prime_marks(marks)
    initial_asset = float(broker.equity())
    if not np.isfinite(initial_asset) or initial_asset <= 0:
        raise ValueError('initial asset must be positive')

    equity_rows = []
    state_rows = []
    last_breadth = 0.0
    last_desired = tuple(sorted(broker.strategy_held))
    code_tuple = tuple(str(x) for x in codes)
    industry_tuple = tuple(str(x) for x in industries)
    for i in range(first, last):
        fundamental_snapshot = (
            fundamental_provider.snapshot_before(int(calendar[i]))
            if fundamental_provider is not None
            else empty_fundamental_snapshot(len(codes))
        )
        context = SafeStrategyContext(
            date=int(calendar[i]),
            session_index=i,
            signal_period=data_spec.signal_period,
            calendar=readonly_copy(calendar[:i]),
            codes=code_tuple,
            closes=readonly_copy(closes[:i]),
            amounts=readonly_copy(amounts[:i]),
            intraday_dates=readonly_copy(
                calendar[
                    max(0, i - data_spec.intraday_lookback_sessions + 1):i + 1
                ] if signal_times else np.empty(0, dtype=calendar.dtype)
            ),
            intraday_times=tuple(signal_times),
            intraday_closes=readonly_copy(
                signal_close[
                    max(0, i - data_spec.intraday_lookback_sessions + 1):i + 1
                ] if signal_close is not None else np.empty((0, 0, len(codes)))
            ),
            intraday_amounts=readonly_copy(
                signal_amount[
                    max(0, i - data_spec.intraday_lookback_sessions + 1):i + 1
                ] if signal_amount is not None else np.empty((0, 0, len(codes)))
            ),
            industries=industry_tuple,
            fundamental_fields=fundamental_snapshot.fields,
            fundamentals=fundamental_snapshot.values,
            fundamental_report_dates=fundamental_snapshot.report_dates,
            fundamental_available_dates=fundamental_snapshot.available_dates,
            fundamental_cutoff_date=fundamental_snapshot.cutoff_date,
            actual_held=frozenset(broker.actual_held),
            selected_held=frozenset(broker.strategy_held),
            average_cost=readonly_copy(broker.average_cost),
            cooldown_until=readonly_copy(broker.cooldown_until),
        )
        if bool(strategy.ready(context)):
            decision = validate_decision(strategy.decide(context), len(codes))
            broker.execute(
                '%d %s' % (int(calendar[i]), data_spec.execution_time),
                i, execution_matrix[i], decision.desired,
                decision.scores, decision.breadth, decision.target_slots,
                decision.stopped, int(getattr(strategy, 'cooldown_days', 0)),
            )
            last_breadth = decision.breadth
            last_desired = decision.desired
        equity = float(broker.close_bar(closes[i]))
        equity_rows.append([int(calendar[i]), equity, last_breadth, len(last_desired)])
        state_rows.append(broker.state_row(int(calendar[i]), last_desired))

    values = np.asarray([row[1] for row in equity_rows], dtype=float)
    total_return = float(values[-1] / initial_asset - 1.0)
    peaks = np.maximum.accumulate(values)
    max_drawdown = float(np.min(values / peaks - 1.0))
    benchmark = benchmark_qmt_daily(
        Path(args.benchmark_qmt_datadir), args.benchmark_code, args.start, args.end
    )
    benchmark_total = float(benchmark['return'])
    ending_positions = [
        {
            'code': str(code),
            'stock': str(code),
            'shares': int(broker.shares[j]),
            'volume': int(broker.shares[j]),
            'average_cost': (
                float(broker.average_cost[j])
                if np.isfinite(broker.average_cost[j]) else None
            ),
            'market_value': float(
                broker.shares[j] * (
                    closes[last - 1, j]
                    if np.isfinite(closes[last - 1, j]) and closes[last - 1, j] > 0
                    else execution_prices[last - 1, j]
                )
            ),
        }
        for j, code in enumerate(codes) if broker.shares[j] > 0
    ]
    ending_portfolio = {
        'cash': float(broker.cash),
        'positions': ending_positions,
        'selected': [
            str(codes[j]) for j in (
                broker.selected_state
                if args.qmt_selected_state else sorted(broker.actual_held)
            )
        ],
    }
    summary = {
        'period_start': str(args.start),
        'period_end': str(args.end),
        'trading_sessions': len(equity_rows),
        'initial_asset': initial_asset,
        'ending_asset': float(values[-1]),
        'strategy_total_return': total_return,
        'strategy_annualized_return': annualize(total_return, len(equity_rows)),
        'benchmark_name': args.benchmark_name,
        'benchmark_total_return': benchmark_total,
        'benchmark_annualized_return': annualize(benchmark_total, len(equity_rows)),
        'max_drawdown': max_drawdown,
        'trade_count': len(broker.trades),
        'cash': float(broker.cash),
        'starting_positions': initial_portfolio_record.get('positions', []),
        'ending_positions': ending_positions,
        'ending_selected': ending_portfolio['selected'],
        'commission_rate': args.commission_rate,
        'stamp_rate': args.stamp_rate,
        'minimum_commission': args.minimum_commission,
        'qmt_selected_state': args.qmt_selected_state,
        'candidate_sha256': file_sha256(candidate_path),
        'engine_version': ENGINE_VERSION,
        'engine_sha256': engine_sha256(),
        'cache_sha256': file_sha256(cache_path),
        'industry_snapshot_sha256': aggregate_directory_sha256(industry_dir),
        'strategy_name': strategy.name,
        'strategy_data_spec': spec_to_dict(data_spec),
        'future_data_guard': (
            'daily history ends at previous session; declared completed intraday '
            'signal bars precede the Broker-private execution bar; financial '
            'values include only announcements available by the previous session'
        ),
        'snapshot_bias_disclosure': (
            'Current fixed universe and SW1 industry snapshots are intentionally used; '
            'survivorship and classification look-ahead bias remain.'
        ),
    }
    if fundamental_provider is not None:
        summary['fundamental_provenance'] = fundamental_provider.provenance()
        summary['fundamental_cache_sha256'] = file_sha256(
            fundamental_cache_path
        )
    if snapshot_path is not None:
        summary['intraday_snapshot_cache'] = str(snapshot_path)
        summary['intraday_snapshot_sha256'] = file_sha256(snapshot_path)
        summary['intraday_source_manifest'] = snapshot_metadata['source_manifest']
    (output / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    (output / 'ending_portfolio.json').write_text(
        json.dumps(ending_portfolio, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    with (output / 'trades.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow([
            'trade_time', 'stock', 'industry', 'side', 'price', 'quantity',
            'score', 'breadth', 'commission',
        ])
        writer.writerows(broker.trades)
    with (output / 'equity.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow(['date', 'total_asset', 'breadth', 'target_count'])
        writer.writerows(equity_rows)
    with (output / 'state.csv').open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow([
            'date', 'selected_count', 'actual_count', 'selected_not_held',
            'held_not_selected', 'selected', 'actual',
        ])
        writer.writerows(state_rows)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

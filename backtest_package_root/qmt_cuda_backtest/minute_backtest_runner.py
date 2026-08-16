"""CLI and reusable runner for QMT one-minute candidate-pool strategies."""

from __future__ import annotations

import argparse
import csv
import itertools
import json
from pathlib import Path

import numpy as np

try:
    from .frozen_daily_trading_engine import cash_portfolio, load_initial_portfolio
    from .frozen_minute_trading_engine import (
        ENGINE_VERSION,
        FrozenMinuteTradingEngine,
        MinuteExecutionConfig,
        engine_sha256,
    )
    from .minute_strategy_interface import MinuteStrategyContext
    from .minute_vwap_reclaim_strategy import (
        MinuteVwapReclaimStrategy,
        VwapReclaimConfig,
    )
    from .qmt_minute_stream import QmtMinuteStream
except ImportError:
    from frozen_daily_trading_engine import cash_portfolio, load_initial_portfolio
    from frozen_minute_trading_engine import (
        ENGINE_VERSION, FrozenMinuteTradingEngine, MinuteExecutionConfig, engine_sha256,
    )
    from minute_strategy_interface import MinuteStrategyContext
    from minute_vwap_reclaim_strategy import MinuteVwapReclaimStrategy, VwapReclaimConfig
    from qmt_minute_stream import QmtMinuteStream


class CandidateSchedule:
    def __init__(self, codes: list[str], by_date: dict[int, frozenset[str]] | None = None):
        self.codes = codes
        self.all_codes = frozenset(codes)
        self.by_date = by_date

    def at(self, date: int) -> frozenset[str]:
        return self.all_codes if self.by_date is None else self.by_date.get(date, frozenset())


def load_candidate_csv(path: Path) -> tuple[list[str], dict[int, frozenset[str]]]:
    mapping: dict[int, set[str]] = {}
    order = []
    seen = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"date", "code"}.issubset(reader.fieldnames):
            raise ValueError("candidate CSV must contain date,code columns")
        for row in reader:
            date = int(row["date"])
            code = row["code"].strip().upper()
            if "." not in code:
                raise ValueError(f"candidate code requires market suffix: {code}")
            mapping.setdefault(date, set()).add(code)
            if code not in seen:
                seen.add(code); order.append(code)
    return order, {date: frozenset(values) for date, values in mapping.items()}


def run_minute_backtest(
    stream: QmtMinuteStream,
    strategy,
    candidates: CandidateSchedule,
    initial_portfolio,
    execution_config: MinuteExecutionConfig,
):
    iterator = iter(stream)
    first_bar = next(iterator)
    broker = FrozenMinuteTradingEngine(
        stream.codes, execution_config, initial_portfolio
    )
    initial_marks = np.where(
        np.isfinite(first_bar.prev_close) & (first_bar.prev_close > 0),
        first_bar.prev_close, first_bar.open,
    )
    broker.prime_marks(initial_marks)
    broker.start_session(first_bar.date)
    initial_equity = broker.equity()
    if initial_equity <= 0:
        raise ValueError("initial minute portfolio equity must be positive")

    pending_orders = []
    pending_signal_timestamp = 0
    minute_equity, daily_equity = [], []
    last_date = None
    last_daily_row = None
    bars_processed = 0
    signals_submitted = 0

    for bar in itertools.chain([first_bar], iterator):
        if last_date is not None and bar.date != last_date and last_daily_row is not None:
            daily_equity.append(last_daily_row)
        broker.start_session(bar.date)
        if pending_orders:
            broker.process_orders(pending_signal_timestamp, bar, pending_orders)
            pending_orders = []
        equity = broker.mark_close(bar)
        context = MinuteStrategyContext(
            bar=bar,
            candidates=candidates.at(bar.date),
            pending_codes=frozenset(),
            account=broker.account_snapshot(),
        )
        generated = list(strategy.on_minute(context))
        if generated:
            pending_orders = generated
            pending_signal_timestamp = bar.timestamp
            signals_submitted += len(generated)
        minute_equity.append([bar.timestamp, bar.date, bar.hhmm, equity])
        last_daily_row = [bar.date, equity]
        last_date = bar.date
        bars_processed += 1
    if last_daily_row is not None:
        daily_equity.append(last_daily_row)

    values = np.asarray([row[3] for row in minute_equity], dtype=np.float64)
    peaks = np.maximum.accumulate(values)
    ending_positions = broker.ending_positions()
    return {
        "return": float(values[-1] / initial_equity - 1.0),
        "max_drawdown": float(np.min(values / peaks - 1.0)),
        "initial_equity": float(initial_equity),
        "final_equity": float(values[-1]),
        "final_cash": float(broker.cash),
        "ending_positions": ending_positions,
        "orders": broker.order_rows,
        "minute_equity": minute_equity,
        "daily_equity": daily_equity,
        "pending_at_end": [order.__dict__ for order in pending_orders],
        "bars_processed": bars_processed,
        "signals_submitted": signals_submitted,
        "strategy_name": strategy.name,
        "engine_version": ENGINE_VERSION,
        "engine_sha256": engine_sha256(),
    }


def _parse_codes(values: list[str]) -> list[str]:
    result = []
    for value in values:
        for code in value.replace(";", ",").split(","):
            code = code.strip().upper()
            if code and code not in result:
                if "." not in code:
                    raise ValueError(f"code requires market suffix: {code}")
                result.append(code)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--datadir", required=True)
    parser.add_argument("--codes", action="append", default=[])
    parser.add_argument("--candidate-pool", help="CSV with date,code columns")
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--initial-cash", type=float)
    parser.add_argument("--initial-portfolio")
    parser.add_argument("--target-value", type=float, default=50_000.0)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--buy-start", type=int, default=945)
    parser.add_argument("--buy-end", type=int, default=1450)
    parser.add_argument("--reclaim-buffer", type=float, default=0.0005)
    parser.add_argument("--exit-vwap-ratio", type=float, default=0.995)
    parser.add_argument("--commission-rate", type=float, default=0.000285)
    parser.add_argument("--stamp-rate", type=float, default=0.00025)
    parser.add_argument("--minimum-commission", type=float, default=5.0)
    parser.add_argument("--max-volume-participation", type=float, default=0.10)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    try:
        explicit_codes = _parse_codes(args.codes)
        schedule_codes, by_date = (load_candidate_csv(Path(args.candidate_pool))
                                   if args.candidate_pool else ([], None))
        if args.initial_portfolio:
            portfolio = load_initial_portfolio(
                Path(args.initial_portfolio), args.initial_cash
            )
        else:
            portfolio = cash_portfolio(
                500_000.0 if args.initial_cash is None else args.initial_cash
            )
        codes = []
        for code in explicit_codes + schedule_codes + [p.code for p in portfolio.positions]:
            if code not in codes:
                codes.append(code)
        if not codes:
            parser.error("provide --codes, --candidate-pool, or initial positions")
        candidates = CandidateSchedule(codes, by_date)
        stream = QmtMinuteStream(Path(args.datadir), codes, args.start, args.end)
        strategy = MinuteVwapReclaimStrategy(
            codes,
            VwapReclaimConfig(
                args.target_value, args.max_positions, args.buy_start,
                args.buy_end, args.reclaim_buffer, args.exit_vwap_ratio,
            ),
        )
        execution = MinuteExecutionConfig(
            args.commission_rate, args.stamp_rate, args.minimum_commission,
            args.max_volume_participation,
        )
        result = run_minute_backtest(
            stream, strategy, candidates, portfolio, execution
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "orders.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "signal_timestamp","execution_timestamp","date","hhmm","code",
            "order_kind","side","requested_shares","filled_shares","price",
            "fee","status","signal_reason","execution_reason",
        ])
        writer.writerows(result["orders"])
    with (output / "equity_minute.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle); writer.writerow(["timestamp","date","hhmm","equity"])
        writer.writerows(result["minute_equity"])
    with (output / "equity_daily.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle); writer.writerow(["date","equity"])
        writer.writerows(result["daily_equity"])
    ending = {
        "cash": result["final_cash"],
        "positions": [
            {"code": code, "shares": row["shares"], "average_cost": row["average_cost"]}
            for code, row in result["ending_positions"].items()
        ],
        "source_end_date": args.end,
        "engine_version": result["engine_version"],
    }
    (output / "ending_portfolio.json").write_text(
        json.dumps(ending, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        key: value for key, value in result.items()
        if key not in {"orders", "minute_equity", "daily_equity", "ending_positions"}
    }
    summary.update({
        "start": args.start,
        "end": args.end,
        "universe": codes,
        "candidate_pool": (str(Path(args.candidate_pool).resolve())
                           if args.candidate_pool else None),
        "stream_coverage": stream.coverage.__dict__,
        "execution_config": execution.__dict__,
        "strategy_config": strategy.config.__dict__,
        "ending_position_count": len(result["ending_positions"]),
    })
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

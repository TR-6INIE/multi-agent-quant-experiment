"""Neutral candidate contract.

This template intentionally contains no signal, portfolio rule, risk rule, or
suggested strategy family.  Researchers must form the investment hypothesis;
engineers only use this file to learn the allowed interfaces.

Daily interface facts (these are mechanics, not investment suggestions):

* ``create_strategy()`` is called once for one frozen backtest.  The returned
  object's instance state persists across all sessions in that backtest.
* ``calendar`` has shape ``(T,)``. ``closes`` and ``amounts`` have shape
  ``(T, N)``: rows are completed sessions and columns correspond exactly to
  ``codes``.  ``T`` grows by global trading sessions; an individual suspension
  does not change the column alignment.
* ``fundamentals`` and both financial-date matrices have shape ``(N, F)``;
  their columns follow ``fundamental_fields``. ``industries`` has length ``N``.
* ``actual_held`` and ``selected_held`` contain integer stock-column indices.
* The same strategy object receives ``ready(context)`` once per evaluation
  session.  When it returns ``False``, ``decide`` and Broker execution are both
  skipped and existing positions persist unchanged.
* When ``ready`` is true, an empty ``desired`` means an intentional liquidation.
  Otherwise ``desired`` must be distinct integer column indices in ``[0, N)``;
  never return stock-code strings or ``(code, weight)`` pairs.  The Broker
  allocates the target portfolio; daily strategies do not return weights.
* ``scores`` must have shape ``(N,)`` and ``target_slots`` must always be a
  positive integer, including when ``desired`` is empty.
* Daily/fixed-time execution periods may only be ``'1m'`` or ``'5m'``.
  ``signal_period='1d'`` describes signal history; it is not a valid execution
  period.  Execution prices remain private to the Broker.
"""

import numpy as np

from experiment_core.local_backtest import SafeMinuteOrder, SafeStrategyDecision


class Strategy:
    engine_mode = 'daily'
    name = 'empty_daily_contract'
    cooldown_days = 0
    # Interface declaration only. It runs at most once per trading day.
    data_spec = {
        'signal_period': '1d',
        'signal_times': (),
        'execution_period': '5m',
        'execution_time': '09:35',
        # Optional: declare at most 8 names from the documented financial
        # field catalog. An empty tuple requests no financial data.
        'fundamental_fields': (),
    }

    def ready(self, context):
        # context.closes/context.amounts end at the previous completed session.
        # Declared financial columns are exposed as context.fundamentals with
        # context.fundamental_fields defining column order. Values are the
        # latest announcements available by context.fundamental_cutoff_date;
        # missing values are NaN. Report/availability dates are separate arrays.
        # False skips decide/Broker execution for this session and preserves
        # current positions. Replace this condition with the approved design.
        return False

    def decide(self, context):
        # Replace this cash-only body with the approved research design.
        # desired contains integer column indices, never codes or weights.
        scores = np.full(len(context.codes), np.nan, dtype=float)
        return SafeStrategyDecision(
            desired=tuple(),
            scores=scores,
            breadth=0.0,
            target_slots=1,
        )


class MinuteStrategyContract:
    """Interface declaration only; it contains no trading logic."""

    name = 'empty_minute_contract'
    engine_mode = 'minute'
    data_spec = {
        # The minute interface uses the same point-in-time financial contract.
        'fundamental_fields': (),
    }

    def on_minute(self, context):
        # Replace this empty body with the approved research design. Declared
        # financial values still stop at the previous trading session. Returned
        # SafeMinuteOrder kinds may only be target_value, target_shares, or close.
        return []


def create_strategy():
    return Strategy()

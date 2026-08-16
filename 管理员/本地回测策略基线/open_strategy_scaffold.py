"""Neutral, runnable scaffold for round 1 of the open-design experiment.

This file contains no trading hypothesis.  It exists only so audit failure has
a safe cash-only fallback and so every group starts from the same valid module.
"""

from __future__ import annotations

import numpy as np

from experiment_core.local_backtest import SafeStrategyDecision


class Strategy:
    name = 'open_design_empty_scaffold'
    engine_mode = 'daily'
    cooldown_days = 0
    data_spec = {
        'signal_period': '1d',
        'signal_times': (),
        'execution_period': '5m',
        'execution_time': '09:35',
        'fundamental_fields': (),
    }

    def ready(self, context):
        return True

    def decide(self, context):
        scores = np.full(len(context.codes), np.nan, dtype=float)
        return SafeStrategyDecision(
            desired=tuple(),
            scores=scores,
            breadth=0.0,
            target_slots=1,
        )


def create_strategy():
    return Strategy()

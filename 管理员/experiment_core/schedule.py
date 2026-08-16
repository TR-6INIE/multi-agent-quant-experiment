from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class RoundSchedule:
    number: int
    decision_as_of: str
    evaluation_start: str
    evaluation_end: str
    final_round: bool


def get_round_schedule(config: Dict[str, Any], round_number: int) -> RoundSchedule:
    rounds = config.get('rounds') or []
    for item in rounds:
        if int(item.get('number', 0)) == round_number:
            return RoundSchedule(
                number=round_number,
                decision_as_of=str(item['decision_as_of']),
                evaluation_start=str(item['evaluation_start']),
                evaluation_end=str(item['evaluation_end']),
                final_round=bool(item.get('final_round', False)),
            )
    raise ValueError('Round %s is not configured' % round_number)

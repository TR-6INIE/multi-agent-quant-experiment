from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .runner import ExperimentError, ExperimentRunner


def build_parser(group_kind: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Run one group of the multi-agent quant experiment.'
    )
    parser.add_argument(
        '--models-config',
        default=None,
        help='Path to models.local.json (default: 管理员/config/models.local.json)',
    )
    parser.add_argument(
        '--experiment-config',
        default=None,
        help='Path to experiment.json (default: 管理员/config/experiment.json)',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    prepare = sub.add_parser(
        'prepare-round', help='Generate, audit, freeze and locally backtest code'
    )
    prepare.add_argument('--round', type=int, required=True)

    if group_kind == 'control':
        repair_analysis = sub.add_parser(
            'repair-analysis',
            help='Rerun only an incomplete control-group data analysis',
        )
        repair_analysis.add_argument('--round', type=int, required=True)

    if group_kind == 'hr':
        evaluate = sub.add_parser(
            'apply-evaluation', help='Apply manager scores and promotion slots'
        )
        evaluate.add_argument('--round', type=int, required=True)
        evaluate.add_argument('--file', required=True)
        evaluate.add_argument('--promotion-slots', type=int, required=True)

    sub.add_parser('status', help='Show non-private workflow status')
    return parser


def main(group_kind: str, group_dir: Path, admin_dir: Path) -> int:
    parser = build_parser(group_kind)
    args = parser.parse_args()
    models_config = Path(args.models_config).resolve() if args.models_config else (
        admin_dir / 'config' / 'models.local.json'
    )
    experiment_config = (
        Path(args.experiment_config).resolve()
        if args.experiment_config
        else admin_dir / 'config' / 'experiment.json'
    )
    runner = None
    try:
        runner = ExperimentRunner(
            group_kind=group_kind,
            group_dir=group_dir,
            admin_dir=admin_dir,
            models_config_path=models_config,
            experiment_config_path=experiment_config,
        )
        if args.command == 'prepare-round':
            runner.prepare_round(args.round)
            status = runner.status()['rounds'].get(str(args.round), {}).get('status')
            print('Round %d local workflow finished; status=%s.' % (args.round, status))
        elif args.command == 'repair-analysis':
            runner.repair_control_analysis(args.round)
            print('Data analysis repaired for round %d.' % args.round)
        elif args.command == 'apply-evaluation':
            runner.apply_evaluation(
                args.round,
                Path(args.file).resolve(),
                args.promotion_slots,
            )
            print('Evaluation applied for round %d.' % args.round)
        elif args.command == 'status':
            print(json.dumps(runner.status(), ensure_ascii=False, indent=2))
        return 0
    except (ExperimentError, RuntimeError, ValueError, OSError) as exc:
        if (
            runner is not None and
            args.command == 'prepare-round' and
            getattr(args, 'round', None) is not None
        ):
            runner.mark_prepare_failed(args.round, str(exc))
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1

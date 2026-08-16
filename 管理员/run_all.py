from pathlib import Path
import argparse
import importlib
import json
import sys


ADMIN_DIR = Path(__file__).resolve().parent
ROOT_DIR = ADMIN_DIR.parent
sys.path.insert(0, str(ADMIN_DIR))

# Jupyter keeps imported modules cached across repeated %run calls. Remove only
# this project's package so every execution uses the current files on disk.
for module_name in list(sys.modules):
    if module_name == 'experiment_core' or module_name.startswith(
        'experiment_core.'
    ):
        del sys.modules[module_name]
importlib.invalidate_caches()

from experiment_core.runner import ExperimentError, ExperimentRunner


GROUPS = (
    ('方法1', 'hr'),
    ('方法2', 'reflection'),
    ('方法3', 'control'),
)


def make_runner(name, kind, args):
    models = (
        Path(args.models_config).resolve()
        if args.models_config
        else ADMIN_DIR / 'config' / 'models.local.json'
    )
    experiment = (
        Path(args.experiment_config).resolve()
        if args.experiment_config
        else ADMIN_DIR / 'config' / 'experiment.json'
    )
    return ExperimentRunner(
        group_kind=kind,
        group_dir=ROOT_DIR / name,
        admin_dir=ADMIN_DIR,
        models_config_path=models,
        experiment_config_path=experiment,
    )


def main():
    parser = argparse.ArgumentParser(
        description='Prepare or inspect all three experiment groups.'
    )
    parser.add_argument('--models-config', default=None)
    parser.add_argument('--experiment-config', default=None)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare-round')
    prepare.add_argument('--round', type=int, required=True)
    sub.add_parser('status')
    args = parser.parse_args()

    try:
        if args.command == 'prepare-round':
            failures = []
            for name, kind in GROUPS:
                runner = make_runner(name, kind, args)
                current = runner.status()['rounds'].get(str(args.round), {})
                if current.get('status') not in (
                    None,
                    'prepare_failed',
                    'failed_plan_review',
                    'backtest_complete',
                ):
                    print(
                        '%s skipped; round %d status is %s.' % (
                            name, args.round, current.get('status')
                        )
                    )
                    continue
                print('Preparing %s round %d...' % (name, args.round))
                try:
                    runner.prepare_round(args.round)
                except KeyboardInterrupt:
                    runner.mark_prepare_failed(
                        args.round, 'Interrupted by user; safe to resume.'
                    )
                    print(
                        'INTERRUPTED: %s round %d was saved as resumable.' % (
                            name, args.round
                        ),
                        file=sys.stderr,
                    )
                    return 130
                except Exception as exc:
                    runner.mark_prepare_failed(args.round, str(exc))
                    failures.append((name, str(exc)))
                    print('ERROR: %s: %s' % (name, exc), file=sys.stderr)
                    continue
                updated = runner.status()['rounds'].get(str(args.round), {})
                mode = updated.get('deployment_mode')
                if mode == 'reuse_baseline':
                    print(
                        '%s candidate was not deployed; the unchanged round baseline '
                        'was evaluated by the frozen local backtest.' % name
                    )
                else:
                    print(
                        '%s automatic local backtest and post-round processing '
                        'finished; status=%s.' % (name, updated.get('status'))
                    )
            if failures:
                print(
                    'Preparation finished with %d failed group(s); other groups '
                    'were still processed.' % len(failures),
                    file=sys.stderr,
                )
                return 1
        else:
            result = {}
            for name, kind in GROUPS:
                result[name] = make_runner(name, kind, args).status()
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ExperimentError, RuntimeError, ValueError, OSError) as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

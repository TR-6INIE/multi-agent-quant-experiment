from pathlib import Path
import os
import sys

import numpy as np


ADMIN_DIR = Path(__file__).resolve().parent
ROOT_DIR = ADMIN_DIR.parent
sys.path.insert(0, str(ADMIN_DIR))

from experiment_core.config import (
    ConfigurationError,
    load_experiment_config,
    load_provider_configs,
)


def main():
    errors = []
    experiment_path = ADMIN_DIR / 'config' / 'experiment.json'
    models_path = ADMIN_DIR / 'config' / 'models.local.json'
    required_paths = [
        ROOT_DIR / '方法1' / 'run.py',
        ROOT_DIR / '方法2' / 'run.py',
        ROOT_DIR / '方法3' / 'run.py',
        ADMIN_DIR / '本地回测策略模板' / 'strategy_template.py',
        ADMIN_DIR / '本地回测策略基线' / 'open_strategy_scaffold.py',
        ADMIN_DIR / 'local_backtest_worker.py',
        ADMIN_DIR / 'experiment_core' / 'fundamentals.py',
        experiment_path,
    ]
    for path in required_paths:
        if not path.exists():
            errors.append('Missing file: %s' % path)

    try:
        config = load_experiment_config(experiment_path)
        evolution = config.get('strategy_evolution') or {}
        if evolution.get('mode') != 'open_strategy_design':
            errors.append('strategy_evolution.mode must be open_strategy_design')
        if len(config.get('rounds') or []) != 5:
            errors.append('experiment.json must define exactly five rounds')
        local = config.get('local_backtest') or {}
        for key in (
            'package_root', 'cache', 'industry_dir', 'benchmark_qmt_datadir',
            'qmt_datadir', 'fundamental_cache',
        ):
            value = str(local.get(key) or '').strip()
            if not value:
                errors.append('local_backtest.%s is required' % key)
            elif not Path(value).exists():
                errors.append('Missing local_backtest.%s: %s' % (key, value))
        package_root = Path(str(local.get('package_root') or ''))
        for name in (
            'minute_strategy_interface.py', 'qmt_minute_stream.py',
            'frozen_minute_trading_engine.py', 'minute_backtest_runner.py',
        ):
            path = package_root / 'qmt_cuda_backtest' / name
            if not path.exists():
                errors.append('Missing one-minute backtest component: %s' % path)
        participation = float(local.get('max_volume_participation', 0.10))
        if not 0 < participation <= 1:
            errors.append('local_backtest.max_volume_participation must be in (0, 1]')
        max_orders = int(local.get('max_minute_orders_per_bar', 50))
        if max_orders < 1:
            errors.append('local_backtest.max_minute_orders_per_bar must be positive')
        max_financial = int(local.get('max_fundamental_fields', 8))
        if max_financial < 1 or max_financial > 11:
            errors.append('local_backtest.max_fundamental_fields must be within 1..11')
        snapshot_dir = str(local.get('snapshot_cache_dir') or '').strip()
        if not snapshot_dir:
            errors.append('local_backtest.snapshot_cache_dir is required')
        development = local.get('development') or {}
        for key in ('start', 'end', 'cache', 'fundamental_cache', 'manifest'):
            value = str(development.get(key) or '').strip()
            if not value:
                errors.append('local_backtest.development.%s is required' % key)
            elif key in ('cache', 'fundamental_cache', 'manifest') and not Path(value).is_file():
                errors.append(
                    'Missing local_backtest.development.%s: %s' % (key, value)
                )
        development_end = str(development.get('end') or '').replace('-', '')
        if development_end and development_end != '20241231':
            errors.append('development end must be frozen at 2024-12-31')
        rounds = config.get('rounds') or []
        if rounds and development_end >= str(rounds[0]['evaluation_start']).replace('-', ''):
            errors.append('development period overlaps the first evaluation period')
        development_cache = Path(str(development.get('cache') or ''))
        if development_cache.is_file():
            with np.load(development_cache, allow_pickle=False) as data:
                calendar = np.asarray(data['calendar'])
                if not len(calendar) or int(calendar[-1]) > 20241231:
                    errors.append('development market cache contains post-2024 rows')
                if int(calendar[0]) >= 20220101:
                    errors.append('development market cache lacks pre-2022 warm-up rows')
        development_fundamental = Path(
            str(development.get('fundamental_cache') or '')
        )
        if development_fundamental.is_file():
            with np.load(development_fundamental, allow_pickle=False) as data:
                available = np.asarray(data['available_date'])
                if len(available) and int(np.max(available)) > 20241231:
                    errors.append(
                        'development financial cache contains post-2024 events'
                    )
    except Exception as exc:
        errors.append(str(exc))

    if not models_path.exists():
        errors.append(
            'Copy models.example.json to models.local.json and fill fixed model settings'
        )
    else:
        try:
            providers = load_provider_configs(models_path)
            for name, provider in providers.items():
                if 'FILL_WITH' in provider.base_url or 'FILL_WITH' in provider.model:
                    errors.append('Provider %s still contains placeholder values' % name)
                if not os.environ.get(provider.api_key_env):
                    errors.append(
                        'Environment variable %s is not set' % provider.api_key_env
                    )
        except ConfigurationError as exc:
            errors.append(str(exc))

    if errors:
        print('SETUP NOT READY')
        for item in errors:
            print('- ' + item)
        return 1
    print('SETUP READY')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

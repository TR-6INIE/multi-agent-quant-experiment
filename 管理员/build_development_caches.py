from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np


ADMIN_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = ADMIN_DIR / 'development_caches'
MARKET_SOURCE = Path(
    r'<BACKTEST_PACKAGE_ROOT>\qmt_cuda_backtest\broad_5m_daily_cache_qmt_v6_current_anchor.npz'
)
FUNDAMENTAL_SOURCE = Path(
    r'<BACKTEST_PACKAGE_ROOT>\qmt_cuda_backtest\full_a_financial_pit_202001_20260722.npz'
)
MARKET_OUTPUT = OUTPUT_DIR / 'market_warmup_20200401_through_20241231.npz'
FUNDAMENTAL_OUTPUT = OUTPUT_DIR / 'fundamental_pit_through_20241231.npz'
MANIFEST_OUTPUT = OUTPUT_DIR / 'development_cache_manifest.json'
DEVELOPMENT_START = 20220101
DEVELOPMENT_END = 20241231


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def build_market_cache() -> dict:
    with np.load(MARKET_SOURCE, allow_pickle=False) as source:
        calendar = np.asarray(source['calendar'])
        keep = calendar <= DEVELOPMENT_END
        if not np.any(keep):
            raise RuntimeError('market source contains no pre-cutoff sessions')
        arrays = {
            'calendar': calendar[keep],
            'codes': np.asarray(source['codes']),
            'opens': np.asarray(source['opens'])[keep],
            'closes': np.asarray(source['closes'])[keep],
            'amounts': np.asarray(source['amounts'])[keep],
        }
    np.savez_compressed(MARKET_OUTPUT, **arrays)
    return {
        'source': str(MARKET_SOURCE),
        'source_sha256': sha256(MARKET_SOURCE),
        'output': str(MARKET_OUTPUT),
        'output_sha256': sha256(MARKET_OUTPUT),
        'calendar_start': int(arrays['calendar'][0]),
        'calendar_end': int(arrays['calendar'][-1]),
        'session_count': int(len(arrays['calendar'])),
        'code_count': int(len(arrays['codes'])),
        'scored_development_start': DEVELOPMENT_START,
        'scored_development_end': DEVELOPMENT_END,
        'note': 'Pre-2022 rows are retained only as strategy warm-up history.',
    }


def build_fundamental_cache() -> dict:
    with np.load(FUNDAMENTAL_SOURCE, allow_pickle=False) as source:
        available_date = np.asarray(source['available_date'])
        keep = available_date <= DEVELOPMENT_END
        arrays = {
            'schema_version': np.asarray(source['schema_version']),
            'codes': np.asarray(source['codes']),
            'fields': np.asarray(source['fields']),
            'event_code': np.asarray(source['event_code'])[keep],
            'event_field': np.asarray(source['event_field'])[keep],
            'report_date': np.asarray(source['report_date'])[keep],
            'announce_date': np.asarray(source['announce_date'])[keep],
            'available_date': available_date[keep],
            'value': np.asarray(source['value'])[keep],
        }
    np.savez_compressed(FUNDAMENTAL_OUTPUT, **arrays)
    return {
        'source': str(FUNDAMENTAL_SOURCE),
        'source_sha256': sha256(FUNDAMENTAL_SOURCE),
        'output': str(FUNDAMENTAL_OUTPUT),
        'output_sha256': sha256(FUNDAMENTAL_OUTPUT),
        'maximum_available_date': int(np.max(arrays['available_date'])),
        'event_count': int(len(arrays['value'])),
        'code_count': int(len(arrays['codes'])),
        'field_count': int(len(arrays['fields'])),
        'cutoff_rule': 'available_date <= 20241231',
    }


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        'schema_version': 1,
        'purpose': 'frozen development-only cache; never used for evaluation',
        'development_period': {
            'start': DEVELOPMENT_START,
            'end': DEVELOPMENT_END,
        },
        'market': build_market_cache(),
        'fundamental': build_fundamental_cache(),
    }
    MANIFEST_OUTPUT.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

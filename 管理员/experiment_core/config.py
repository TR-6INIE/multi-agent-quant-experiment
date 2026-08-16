from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


class ConfigurationError(RuntimeError):
    pass


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in ('', 'none', 'null'):
        return None
    return float(value)


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    model: str
    api_key_env: str
    timeout_seconds: int = 120
    max_retries: int = 3
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = None
    stream: bool = False
    extra_headers: Dict[str, str] = field(default_factory=dict)
    extra_body: Dict[str, Any] = field(default_factory=dict)

    @property
    def api_key(self) -> str:
        value = os.environ.get(self.api_key_env, '').strip()
        if not value:
            raise ConfigurationError(
                'Missing API key environment variable: %s' % self.api_key_env
            )
        return value


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ConfigurationError('Configuration file not found: %s' % path)
    try:
        with path.open('r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigurationError('Invalid JSON configuration %s: %s' % (path, exc))
    if not isinstance(data, dict):
        raise ConfigurationError('Top-level JSON must be an object: %s' % path)
    return data


def load_provider_configs(path: Path) -> Dict[str, ProviderConfig]:
    raw = load_json(path)
    providers = raw.get('providers')
    if not isinstance(providers, dict):
        raise ConfigurationError('models config must contain a providers object')

    result: Dict[str, ProviderConfig] = {}
    for name, item in providers.items():
        if not isinstance(item, dict):
            raise ConfigurationError('Provider %s must be an object' % name)
        required = ('base_url', 'model', 'api_key_env')
        missing = [key for key in required if not str(item.get(key, '')).strip()]
        if missing:
            raise ConfigurationError(
                'Provider %s is missing: %s' % (name, ', '.join(missing))
            )
        result[name] = ProviderConfig(
            name=name,
            base_url=str(item['base_url']).strip(),
            model=str(item['model']).strip(),
            api_key_env=str(item['api_key_env']).strip(),
            timeout_seconds=int(item.get('timeout_seconds', 120)),
            max_retries=int(item.get('max_retries', 3)),
            temperature=(
                _optional_float(item['temperature'])
                if 'temperature' in item
                else 0.2
            ),
            max_tokens=(
                int(item['max_tokens'])
                if item.get('max_tokens') is not None
                else None
            ),
            stream=bool(item.get('stream', False)),
            extra_headers=dict(item.get('extra_headers') or {}),
            extra_body=dict(item.get('extra_body') or {}),
        )
    return result


def load_experiment_config(path: Path) -> Dict[str, Any]:
    data = load_json(path)
    required = ('role_models', 'rounds', 'random_seed')
    missing = [key for key in required if key not in data]
    if missing:
        raise ConfigurationError(
            'Experiment config is missing: %s' % ', '.join(missing)
        )
    return data

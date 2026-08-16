from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_text(path: Path, text: str) -> None:
    ensure_directory(path.parent)
    handle, temp_name = tempfile.mkstemp(
        prefix=path.name + '.', suffix='.tmp', dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            if text and not text.endswith('\n'):
                stream.write('\n')
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_qmt_code(path: Path, text: str) -> None:
    """Atomically write QMT Python source as real GBK bytes without a BOM."""
    ensure_directory(path.parent)
    payload = text
    if payload and not payload.endswith('\n'):
        payload += '\n'
    data = payload.encode('gbk')
    handle, temp_name = tempfile.mkstemp(
        prefix=path.name + '.', suffix='.tmp', dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open('r', encoding='utf-8') as handle:
        return json.load(handle)


def read_text(path: Path, default: str = '') -> str:
    if not path.exists():
        return default
    data = path.read_bytes()
    for encoding in ('utf-8-sig', 'gbk'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def copy_file(source: Path, destination: Path) -> None:
    ensure_directory(destination.parent)
    shutil.copy2(str(source), str(destination))


def round_directory(group_dir: Path, round_number: int) -> Path:
    return group_dir / 'runs' / ('round_%02d' % round_number)


def initialize_round_directories(group_dir: Path, round_number: int) -> Dict[str, Path]:
    root = round_directory(group_dir, round_number)
    paths = {
        'root': root,
        'shared': root / 'shared',
        'private': root / 'private',
        'logs': root / 'logs',
        'admin': root / 'admin',
        'input': root / 'input',
    }
    for path in paths.values():
        ensure_directory(path)
    return paths


def collect_text_files(paths: Iterable[Path], max_chars: int = 80000) -> str:
    chunks = []
    used = 0
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        text = read_text(path)
        remaining = max_chars - used
        if remaining <= 0:
            break
        text = text[:remaining]
        chunks.append('\n===== %s =====\n%s' % (path.name, text))
        used += len(text)
    return '\n'.join(chunks)

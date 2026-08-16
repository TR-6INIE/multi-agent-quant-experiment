from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np


ALLOWED_IMPORT_ROOTS = {
    '__future__', 'collections', 'dataclasses', 'math', 'numpy', 'typing',
}
FORBIDDEN_NAMES = {
    '__builtins__', '__import__', 'breakpoint', 'compile', 'eval', 'exec',
    'delattr', 'dir', 'getattr', 'globals', 'help', 'input', 'locals', 'open',
    'setattr', 'vars',
}
FORBIDDEN_ATTRIBUTE_CALLS = {
    'fromfile', 'genfromtxt', 'load', 'loadtxt', 'memmap', 'open_memmap',
    'load_library', 'open', 'read_bytes', 'read_text', 'request', 'save',
    'savetxt', 'tofile',
    'urlopen',
}


@dataclass(frozen=True)
class SafeStrategyContext:
    """The only market view supplied to experiment strategies.

    ``calendar``, ``closes`` and ``amounts`` end at the previous completed
    session.  The current session execution-price vector is intentionally not
    exposed to candidate code; it remains private to the frozen Broker. Arrays
    are detached read-only copies, so ``.base`` cannot reveal the full cache.
    """

    date: int
    session_index: int
    signal_period: str
    calendar: np.ndarray
    codes: Tuple[str, ...]
    closes: np.ndarray
    amounts: np.ndarray
    intraday_dates: np.ndarray
    intraday_times: Tuple[str, ...]
    intraday_closes: np.ndarray
    intraday_amounts: np.ndarray
    industries: Tuple[str, ...]
    fundamental_fields: Tuple[str, ...]
    fundamentals: np.ndarray
    fundamental_report_dates: np.ndarray
    fundamental_available_dates: np.ndarray
    fundamental_cutoff_date: int
    actual_held: frozenset[int]
    selected_held: frozenset[int]
    average_cost: np.ndarray
    cooldown_until: np.ndarray


@dataclass(frozen=True)
class SafeStrategyDecision:
    desired: Tuple[int, ...]
    scores: np.ndarray
    breadth: float
    target_slots: int
    stopped: frozenset[int] = frozenset()


@dataclass(frozen=True)
class SafeMinuteOrder:
    """One-shot order created after the current minute has fully closed."""

    code: str
    kind: str
    value: float | int = 0
    reason: str = ''


@dataclass(frozen=True)
class SafeMinuteBar:
    timestamp: int
    date: int
    hhmm: int
    codes: Tuple[str, ...]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    amount: np.ndarray
    prev_close: np.ndarray


@dataclass(frozen=True)
class SafeMinuteAccount:
    cash: float
    equity: float
    shares: np.ndarray
    sellable_shares: np.ndarray
    average_cost: np.ndarray


@dataclass(frozen=True)
class SafeMinuteStrategyContext:
    """Current completed minute and a detached account snapshot.

    Orders returned from this context are processed only at the next global
    one-minute bar open.  No next-bar price or future minute is exposed.
    """

    bar: SafeMinuteBar
    candidates: frozenset[str]
    pending_codes: frozenset[str]
    account: SafeMinuteAccount
    daily_calendar: np.ndarray
    daily_closes: np.ndarray
    daily_amounts: np.ndarray
    industries: Tuple[str, ...]
    fundamental_fields: Tuple[str, ...]
    fundamentals: np.ndarray
    fundamental_report_dates: np.ndarray
    fundamental_available_dates: np.ndarray
    fundamental_cutoff_date: int


def readonly_copy(values: np.ndarray) -> np.ndarray:
    result = np.array(values, copy=True)
    result.setflags(write=False)
    return result


def local_strategy_static_check(code: str) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [{
            'id': 'LOCAL-SYNTAX',
            'severity': 'BLOCKER',
            'line': exc.lineno,
            'message': str(exc),
        }]

    has_factory = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == 'create_strategy':
                has_factory = True
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split('.', 1)[0]
                if root not in ALLOWED_IMPORT_ROOTS:
                    issues.append({
                        'id': 'LOCAL-IMPORT-%d' % node.lineno,
                        'severity': 'BLOCKER',
                        'line': node.lineno,
                        'message': 'forbidden import: %s' % alias.name,
                    })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            root = module.split('.', 1)[0]
            allowed = (
                root in ALLOWED_IMPORT_ROOTS
                or module == 'experiment_core.local_backtest'
            )
            if not allowed:
                issues.append({
                    'id': 'LOCAL-IMPORT-%d' % node.lineno,
                    'severity': 'BLOCKER',
                    'line': node.lineno,
                        'message': 'forbidden import: %s' % (node.module or ''),
                })
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith('_')
            and not (
                isinstance(node.value, ast.Name)
                and node.value.id == 'self'
                and not node.attr.startswith('__')
            )
        ):
            issues.append({
                'id': 'LOCAL-PRIVATE-ATTR-%d' % getattr(node, 'lineno', 0),
                'severity': 'BLOCKER',
                'line': getattr(node, 'lineno', None),
                'message': (
                    'private context/library attributes and all dunder attributes '
                    'are forbidden: %s' % node.attr
                ),
            })
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            issues.append({
                'id': 'LOCAL-NAME-%d' % getattr(node, 'lineno', 0),
                'severity': 'BLOCKER',
                'line': getattr(node, 'lineno', None),
                'message': 'forbidden runtime capability: %s' % node.id,
            })
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_ATTRIBUTE_CALLS:
                issues.append({
                    'id': 'LOCAL-IO-%d' % node.lineno,
                    'severity': 'BLOCKER',
                    'line': node.lineno,
                    'message': 'strategy may not read files, URLs, or caches: .%s()'
                    % node.func.attr,
                })

    if not has_factory:
        issues.append({
            'id': 'LOCAL-FACTORY',
            'severity': 'BLOCKER',
            'line': None,
            'message': 'candidate must define create_strategy()',
        })
    unique = {}
    for issue in issues:
        unique[(issue['id'], issue['message'])] = issue
    return list(unique.values())


def local_static_report(issues: Sequence[Dict[str, Any]]) -> str:
    if not issues:
        return 'LOCAL_DETERMINISTIC_CHECK: PASSED'
    lines = ['LOCAL_DETERMINISTIC_CHECK: FAILED']
    for issue in issues:
        location = '' if issue.get('line') is None else ' line=%s' % issue['line']
        lines.append(
            '- [%s] %s%s: %s' % (
                issue.get('severity', 'BLOCKER'), issue.get('id', 'LOCAL'),
                location, issue.get('message', ''),
            )
        )
    return '\n'.join(lines)


def run_backtest_worker(
    worker_path: Path,
    candidate_path: Path,
    output_dir: Path,
    config: Dict[str, Any],
    start: str,
    end: str,
    initial_portfolio: Optional[Path],
    timeout_seconds: int,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, '-B', str(worker_path),
        '--candidate', str(candidate_path),
        '--package-root', str(config['package_root']),
        '--cache', str(config['cache']),
        '--industry-dir', str(config['industry_dir']),
        '--start', start.replace('-', ''),
        '--end', end.replace('-', ''),
        '--initial-cash', str(config.get('initial_cash', 500000)),
        '--commission-rate', str(config.get('commission_rate', 0.000285)),
        '--stamp-rate', str(config.get('stamp_rate', 0.00025)),
        '--minimum-commission', str(config.get('minimum_commission', 5.0)),
        '--benchmark-qmt-datadir', str(config['benchmark_qmt_datadir']),
        '--benchmark-code', str(config.get('benchmark_code', '000688.SH')),
        '--benchmark-name', str(config.get('benchmark_name', '科创50')),
        '--qmt-datadir', str(config['qmt_datadir']),
        '--snapshot-cache-dir', str(config['snapshot_cache_dir']),
        '--max-signal-times', str(config.get('max_signal_times', 4)),
        '--max-intraday-lookback-sessions', str(
            config.get('max_intraday_lookback_sessions', 80)
        ),
        '--max-minute-orders-per-bar', str(
            config.get('max_minute_orders_per_bar', 50)
        ),
        '--max-volume-participation', str(
            config.get('max_volume_participation', 0.10)
        ),
        '--output', str(output_dir),
    ]
    fundamental_cache = str(config.get('fundamental_cache') or '').strip()
    if fundamental_cache:
        command.extend([
            '--fundamental-cache', fundamental_cache,
            '--max-fundamental-fields', str(
                int(config.get('max_fundamental_fields', 8))
            ),
        ])
    if initial_portfolio is not None:
        command.extend(['--initial-portfolio', str(initial_portfolio)])
    if bool(config.get('qmt_selected_state', True)):
        command.append('--qmt-selected-state')

    command_path = output_dir / 'command.json'
    command_path.write_text(
        json.dumps(command, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    try:
        completed = subprocess.run(
            command,
            cwd=str(worker_path.parent.parent),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, 'LOCAL_BACKTEST_TIMEOUT after %d seconds' % timeout_seconds, None
    (output_dir / 'stdout.log').write_text(completed.stdout, encoding='utf-8')
    (output_dir / 'stderr.log').write_text(completed.stderr, encoding='utf-8')
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or '').strip()
        return False, 'LOCAL_BACKTEST_FAILED exit=%d\n%s' % (
            completed.returncode, detail[-6000:]
        ), None
    summary_path = output_dir / 'summary.json'
    if not summary_path.exists():
        return False, 'LOCAL_BACKTEST_FAILED: summary.json was not produced', None
    try:
        summary = json.loads(summary_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        return False, 'LOCAL_BACKTEST_FAILED: invalid summary.json: %s' % exc, None
    return True, 'LOCAL_BACKTEST: PASSED', summary


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def local_result_markdown(summary: Dict[str, Any]) -> str:
    lines = ['# Local backtest result', '']
    for key in (
        'period_start', 'period_end', 'trading_sessions', 'initial_asset',
        'ending_asset', 'strategy_total_return', 'strategy_annualized_return',
        'benchmark_name', 'benchmark_total_return',
        'benchmark_annualized_return', 'max_drawdown', 'trade_count', 'cash',
        'candidate_sha256', 'engine_version', 'engine_sha256', 'cache_sha256',
    ):
        if key in summary and summary[key] is not None:
              lines.append('- %s: %s' % (key, summary[key]))
    data_spec = summary.get('strategy_data_spec')
    if data_spec:
        lines.extend(['', '## Strategy data/execution specification', ''])
        for key in (
            'engine_mode', 'signal_data', 'execution',
            'signal_period', 'signal_times', 'execution_period',
            'execution_time', 'intraday_lookback_sessions',
            'rebalance_frequency', 'max_orders_per_bar',
            'max_volume_participation', 'fundamental_fields',
        ):
            if key in data_spec:
                lines.append('- %s: %s' % (key, data_spec[key]))
    snapshot_path = summary.get('intraday_snapshot_cache')
    snapshot_hash = summary.get('intraday_snapshot_sha256')
    snapshot_source = summary.get('intraday_source_manifest') or {}
    if snapshot_path or snapshot_hash or snapshot_source:
        lines.extend(['', '## Intraday snapshot provenance', ''])
        if snapshot_hash:
            lines.append('- snapshot_sha256: %s' % snapshot_hash)
        if snapshot_path:
            lines.append('- snapshot_cache: %s' % snapshot_path)
        for key in ('metadata_sha256', 'file_count', 'missing_file_count', 'note'):
            if key in snapshot_source:
                lines.append('- source_%s: %s' % (key, snapshot_source[key]))
    minute_source = summary.get('minute_source_manifest') or {}
    if minute_source:
        lines.extend(['', '## One-minute raw-data provenance', ''])
        for key in ('metadata_sha256', 'file_count', 'missing_file_count', 'note'):
            if key in minute_source:
                lines.append('- source_%s: %s' % (key, minute_source[key]))
    fundamental = summary.get('fundamental_provenance') or {}
    if fundamental:
        lines.extend(['', '## Point-in-time financial provenance', ''])
        lines.append(
            '- fundamental_cache_sha256: %s'
            % summary.get('fundamental_cache_sha256')
        )
        for key in ('fields', 'mapped_event_count', 'cutoff_rule', 'final_coverage'):
            if key in fundamental:
                lines.append('- %s: %s' % (key, fundamental[key]))
    lines.extend(['', '## Data-boundary disclosure', ''])
    if (data_spec or {}).get('engine_mode') == 'minute':
        lines.extend([
            '- Candidate code sees only the fully completed current one-minute bar.',
            '- Orders are processed once at the next global one-minute bar open.',
            '- Next-bar execution prices are private to the frozen Broker.',
        ])
    else:
        lines.extend([
            '- Candidate code receives detached, read-only history ending at the previous session.',
            '- Execution prices are private to the frozen Broker.',
            '- Fixed-time signals contain only completed bars strictly before the execution bar.',
        ])
    if (data_spec or {}).get('fundamental_fields'):
        lines.append(
            '- Financial values contain only announcements available by the '
            'previous trading session; the raw event cache is not exposed.'
        )
    lines.extend([
        '- Broker, fees, lot sizing, cash and position accounting are outside candidate code.',
        '- The stock universe and SW1 industry classification are fixed current snapshots; '
        'survivorship and classification look-ahead bias are intentionally retained.',
    ])
    for limitation in summary.get('known_limitations') or []:
        lines.append('- Known limitation: %s' % limitation)
    return '\n'.join(lines)

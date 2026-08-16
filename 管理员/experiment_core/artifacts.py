from __future__ import annotations

import ast
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .storage import read_json, read_text


CODE_START = '<<<STRATEGY_CODE>>>'
CODE_END = '<<<END_STRATEGY_CODE>>>'
NOTES_START = '<<<ENGINEER_NOTES>>>'
NOTES_END = '<<<END_ENGINEER_NOTES>>>'

QMT_DETAIL_FILE_SPECS = {
    'daily_nav': {
        'field': 'daily_nav_file',
        'archive_name': 'qmt_daily_nav.csv',
    },
    'trades': {
        'field': 'trade_records_file',
        'archive_name': 'qmt_trade_records.csv',
    },
    'orders': {
        'field': 'order_records_file',
        'archive_name': 'qmt_order_records.csv',
    },
}

CSV_ALIASES = {
    'date': ('date', '日期', '交易日期'),
    'total_asset': ('total_asset', '总资产', '资产'),
    'cash': ('cash', '现金', '可用资金'),
    'market_value': ('market_value', '持仓市值', '股票市值'),
    'strategy_nav': ('strategy_nav', '策略净值', '单位净值'),
    'benchmark_nav': ('benchmark_nav', '基准净值'),
    'trade_time': ('trade_time', '成交时间', '操作时间', '时间'),
    'order_time': ('order_time', '委托时间', '报单时间', '时间'),
    'stock': ('stock', '证券代码', '股票代码', '代码'),
    'side': ('side', '买卖方向', '操作', '操作类型', '业务类型', '方向'),
    'quantity': ('quantity', '成交数量', '委托数量', '数量'),
    'price': ('price', '成交价格', '成交均价', '操作价格', '委托价格', '价格'),
    'amount': ('amount', '成交金额', '市值', '金额'),
    'commission': ('commission', '佣金', '手续费', '交易费用'),
    'stamp_tax': ('stamp_tax', '印花税'),
    'order_id': ('order_id', '委托编号', '合同编号', '订单编号'),
    'realized_pnl': ('realized_pnl', '实现盈亏', '平仓盈亏', '盈利'),
    'holding_days': ('holding_days', '持仓天数', '持有天数'),
    'status': ('status', '委托状态', '订单状态', '状态'),
    'reject_reason': ('reject_reason', '废单原因', '拒单原因', '失败原因'),
    'sellable_volume': ('sellable_volume', '可卖数量', '可用数量'),
}


def parse_engineer_output(text: str) -> Tuple[str, str]:
    if CODE_START in text and CODE_END in text:
        code = text.split(CODE_START, 1)[1].split(CODE_END, 1)[0].strip()
        notes = ''
        if NOTES_START in text and NOTES_END in text:
            notes = text.split(NOTES_START, 1)[1].split(NOTES_END, 1)[0].strip()
        return normalize_python_encoding(_strip_code_fence(code)), notes

    match = re.search(r'```(?:python)?\s*(.*?)```', text, flags=re.I | re.S)
    if not match:
        raise ValueError('Engineer response does not contain a strategy code block')
    code = match.group(1).strip()
    notes = (text[:match.start()] + text[match.end():]).strip()
    return normalize_python_encoding(code), notes


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith('```'):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        return '\n'.join(lines).strip()
    return stripped


def normalize_python_encoding(code: str) -> str:
    """Normalize generated strategy source for the UTF-8 local runner."""
    lines = code.splitlines()
    coding = re.compile(r'coding\s*[:=]\s*[-\w.]+')
    replaced = False
    for index in range(min(2, len(lines))):
        if coding.search(lines[index]):
            lines[index] = '# -*- coding: utf-8 -*-'
            replaced = True
            break
    if not replaced:
        lines.insert(0, '# -*- coding: utf-8 -*-')
    return '\n'.join(lines)


def parse_audit_decision(text: str) -> Optional[str]:
    match = re.search(
        r'^\s*DECISION\s*:\s*(PASS|REVISE|BLOCK)\s*$',
        text,
        flags=re.I | re.M,
    )
    if not match:
        return None
    return match.group(1).upper()


def parse_plan_review_decision(text: str) -> Optional[str]:
    match = re.search(
        r'^\s*PLAN_DECISION\s*:\s*(PASS|REVISE)\s*$',
        text,
        flags=re.I | re.M,
    )
    if not match:
        return None
    return match.group(1).upper()


def parse_open_audit_issue_ids(text: str) -> Tuple[str, ...]:
    """Return structured open BLOCKER/MAJOR issue IDs in report order."""
    marker = re.compile(r'^\s*ISSUE_ID\s*:\s*([A-Z0-9-]+)\s*$', re.I | re.M)
    matches = list(marker.finditer(text))
    result = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end():end]
        severity = re.search(
            r'^\s*SEVERITY\s*:\s*(BLOCKER|MAJOR|MINOR|SUGGESTION)\s*$',
            block,
            re.I | re.M,
        )
        status = re.search(
            r'^\s*STATUS\s*:\s*(OPEN|RESOLVED)\s*$',
            block,
            re.I | re.M,
        )
        if not severity or not status:
            continue
        if (
            severity.group(1).upper() in ('BLOCKER', 'MAJOR')
            and status.group(1).upper() == 'OPEN'
        ):
            issue_id = match.group(1).upper()
            if issue_id not in result:
                result.append(issue_id)
    return tuple(result)


def qmt_static_check(code: str, decision_as_of: str) -> Tuple[Dict[str, str], ...]:
    """Check deterministic QMT contracts that do not require model judgment."""
    issues = []

    def add(issue_id: str, message: str) -> None:
        if not any(item['id'] == issue_id for item in issues):
            issues.append({'id': issue_id, 'message': message})

    try:
        code.encode('gbk')
    except UnicodeEncodeError as exc:
        add(
            'STATIC-QMT-ENCODING-GBK',
            'Strategy source is not encodable as GBK: %s' % exc,
        )

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        add('STATIC-SYNTAX', 'Python syntax error: %s' % exc)
        return tuple(issues)

    symbol_pattern = re.compile(r'^\d{6}\.(?:SH|SZ)$', re.I)
    symbol_variables: Dict[str, set] = {}

    def expression_symbols(node: Optional[ast.AST]) -> set:
        if node is None:
            return set()
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and symbol_pattern.fullmatch(node.value.strip())
        ):
            return {node.value.strip().upper()}
        if isinstance(node, ast.Name):
            return set(symbol_variables.get(node.id, set()))
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            result = set()
            for item in node.elts:
                result.update(expression_symbols(item))
            return result
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return expression_symbols(node.left) | expression_symbols(node.right)
        if isinstance(node, ast.Call):
            result = set()
            for item in node.args:
                result.update(expression_symbols(item))
            return result
        return set()

    # Resolve simple constant-list propagation such as
    # universe = unique(trade_pool + INDEX_CODES).
    for _ in range(6):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = expression_symbols(node.value)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    before = symbol_variables.get(target.id, set())
                    after = before | value
                    if after != before:
                        symbol_variables[target.id] = after
                        changed = True
        if not changed:
            break

    subscribed_symbols = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr if isinstance(node.func, ast.Attribute)
            else (node.func.id if isinstance(node.func, ast.Name) else '')
        )
        if name == 'set_universe' and node.args:
            subscribed_symbols.update(expression_symbols(node.args[0]))

    required_history_symbols = set()
    for loop in (node for node in ast.walk(tree) if isinstance(node, ast.For)):
        if not isinstance(loop.target, ast.Name):
            continue
        loop_symbols = expression_symbols(loop.iter)
        if not loop_symbols:
            continue
        target_name = loop.target.id
        reads_by_key = False
        for child in ast.walk(ast.Module(body=loop.body, type_ignores=[])):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == 'get'
                and child.args
                and isinstance(child.args[0], ast.Name)
                and child.args[0].id == target_name
            ):
                reads_by_key = True
            if (
                isinstance(child, ast.Subscript)
                and isinstance(child.slice, ast.Name)
                and child.slice.id == target_name
            ):
                reads_by_key = True
        if reads_by_key:
            required_history_symbols.update(loop_symbols)

    missing_subscriptions = sorted(required_history_symbols - subscribed_symbols)
    if missing_subscriptions:
        add(
            'STATIC-QMT-DATA-SUBSCRIPTION',
            'Fixed symbols are read from a data mapping but are not present in '
            'the set_universe subscription chain: %s'
            % ', '.join(missing_subscriptions),
        )

    functions = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    for required in ('init', 'handlebar', 'stop'):
        if required not in functions:
            add(
                'STATIC-LIFECYCLE-' + required.upper(),
                'Missing required QMT lifecycle function: ' + required,
            )

    if re.search(r'\bContextInfo\.portfolio\b', code):
        add(
            'STATIC-QMT-ACCOUNT-PORTFOLIO',
            'ContextInfo.portfolio is not portable in QMT; preserve the baseline '
            'account pattern using m_dBalance from get_trade_detail_data(..., account).',
        )
    string_variables = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == 'str'
            ):
                for target in targets:
                    if isinstance(target, ast.Name):
                        string_variables.add(target.id)

    cutoff = datetime.strptime(decision_as_of, '%Y-%m-%d').date()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            compact = re.fullmatch(r'(20\d{2})[-/]?(\d{2})[-/]?(\d{2})', node.value)
            if compact:
                try:
                    literal_date = datetime.strptime(
                        ''.join(compact.groups()), '%Y%m%d'
                    ).date()
                except ValueError:
                    literal_date = None
                if literal_date and literal_date > cutoff:
                    add(
                        'STATIC-FUTURE-DATE',
                        'Code contains a hard-coded date after decision_as_of: '
                        + node.value,
                    )

        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        elif isinstance(node.func, ast.Name):
            name = node.func.id
        else:
            continue
        if name == 'get_market_data_ex':
            if len(node.args) < 2:
                add(
                    'STATIC-QMT-MARKETDATA-POSITIONAL',
                    'get_market_data_ex must pass fields and stocks as the first '
                    'two positional arguments.',
                )
            if any(keyword.arg == 'stock_list' for keyword in node.keywords):
                add(
                    'STATIC-QMT-MARKETDATA-KEYWORD',
                    'Unverified stock_list keyword used with get_market_data_ex.',
                )
        elif name == 'get_trading_dates':
            count_expr = node.args[3] if len(node.args) >= 4 else None
            if count_expr is None:
                for keyword in node.keywords:
                    if keyword.arg == 'count':
                        count_expr = keyword.value
                        break
            if count_expr is None:
                add(
                    'STATIC-QMT-TRADING-DATES-COUNT',
                    'get_trading_dates must receive an explicit positive integer count.',
                )
            elif (
                isinstance(count_expr, ast.Constant)
                and isinstance(count_expr.value, str)
            ) or (
                isinstance(count_expr, ast.Call)
                and isinstance(count_expr.func, ast.Name)
                and count_expr.func.id == 'str'
            ) or (
                isinstance(count_expr, ast.Name)
                and count_expr.id in string_variables
            ):
                add(
                    'STATIC-QMT-TRADING-DATES-COUNT-TYPE',
                    'get_trading_dates count must be an integer, not a string.',
                )
        elif name == 'order_target_percent' and len(node.args) >= 2:
            weight = node.args[1]
            if isinstance(weight, ast.Constant) and isinstance(weight.value, (int, float)):
                if float(weight.value) < 0 or float(weight.value) > 1:
                    add(
                        'STATIC-NO-LEVERAGE-LITERAL',
                        'Literal target weight is outside the long-only [0, 1] range.',
                    )

    has_sell_order = bool(
        re.search(r'order_target_percent\s*\([^\n]+,\s*0(?:\.0+)?\s*,', code)
    )
    if has_sell_order and not re.search(r'can_use|m_nCanUseVolume', code, re.I):
        add(
            'STATIC-T1-SELLABLE-CHECK',
            'Sell orders exist but no T+1 sellable-volume check was found.',
        )
    return tuple(issues)


def qmt_static_report(issues: Tuple[Dict[str, str], ...]) -> str:
    if not issues:
        return 'DETERMINISTIC_CHECKS: PASSED'
    lines = ['DETERMINISTIC_CHECKS: FAILED']
    for issue in issues:
        lines.extend([
            '',
            'ISSUE_ID: ' + issue['id'],
            'SEVERITY: BLOCKER',
            'STATUS: OPEN',
            'LOCATION: deterministic static check',
            'TRIGGER: verified contract violation',
            'IMPACT: ' + issue['message'],
            'FIX: conform to the neutral QMT template and rerun the check',
        ])
    return '\n'.join(lines)


def load_qmt_result(path: Path) -> Dict[str, Any]:
    if path.suffix.lower() != '.json':
        raise ValueError('QMT result must use the supplied JSON template')
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError('QMT result must be a JSON object')
    required = (
        'strategy_annualized_return',
        'benchmark_annualized_return',
        'period_start',
        'period_end',
    )
    missing = [key for key in required if data.get(key) is None]
    if missing:
        raise ValueError('QMT result is missing: %s' % ', '.join(missing))
    value = float(data['strategy_annualized_return'])
    if abs(value) > 20:
        raise ValueError(
            'strategy_annualized_return must be a decimal, e.g. 0.15 for 15%%'
        )
    data['strategy_annualized_return'] = value
    if data.get('benchmark_annualized_return') is not None:
        data['benchmark_annualized_return'] = float(
            data['benchmark_annualized_return']
        )
    return data


def qmt_result_to_markdown(data: Dict[str, Any]) -> str:
    lines = ['# QMT result', '']
    preferred = (
        'backtest_start', 'carryover_mode', 'bootstrap_trade_count',
        'period_start', 'period_end', 'initial_asset', 'ending_asset',
        'strategy_total_return', 'strategy_annualized_return',
        'benchmark_name', 'benchmark_annualized_return', 'max_drawdown', 'volatility',
        'sharpe', 'win_rate', 'profit_loss_ratio', 'trade_count',
        'turnover_rate', 'cash', 'executed_strategy_sha256',
    )
    for key in preferred:
        if key in data and data[key] is not None:
            lines.append('- %s: %s' % (key, data[key]))
    notes = data.get('notes')
    if notes:
        lines.extend(['', '## Notes', '', str(notes)])
    settings = data.get('qmt_settings')
    if settings:
        lines.extend(['', '## QMT settings', '', '```json'])
        lines.append(json.dumps(settings, ensure_ascii=False, indent=2))
        lines.append('```')
    starting_positions = data.get('starting_positions') or []
    if starting_positions:
        lines.extend(['', '## Starting positions', '', '```json'])
        lines.append(json.dumps(starting_positions, ensure_ascii=False, indent=2))
        lines.append('```')
    positions = data.get('ending_positions') or []
    if positions:
        lines.extend(['', '## Ending positions', '', '```json'])
        lines.append(json.dumps(positions, ensure_ascii=False, indent=2))
        lines.append('```')
    return '\n'.join(lines)


def _read_csv_rows(
    path: Path, allow_empty: bool = False
) -> Tuple[List[Dict[str, str]], str]:
    if path.suffix.lower() != '.csv':
        raise ValueError('QMT detail file must be CSV: %s' % path)
    raw = path.read_bytes()
    text = None
    encoding = ''
    for candidate in ('utf-8-sig', 'gb18030', 'utf-8'):
        try:
            text = raw.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError('Cannot decode QMT CSV as UTF-8 or GBK: %s' % path)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError('QMT CSV has no header row: %s' % path)
    rows = []
    for source in reader:
        row = {
            str(key or '').strip(): str(value or '').strip()
            for key, value in source.items()
        }
        if any(row.values()):
            rows.append(row)
    if not rows and not allow_empty:
        raise ValueError('QMT CSV has no data rows: %s' % path)
    return rows, encoding


def _row_value(row: Dict[str, str], field: str) -> str:
    aliases = {item.strip().casefold() for item in CSV_ALIASES[field]}
    for key, value in row.items():
        if key.strip().casefold() in aliases:
            return value.strip()
    return ''


def _number(value: str) -> Optional[float]:
    text = str(value or '').strip().replace(',', '')
    if not text or text.lower() in ('none', 'null', 'nan', '--'):
        return None
    percent = text.endswith('%')
    if percent:
        text = text[:-1].strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return number / 100.0 if percent else number


def _date_key(value: str) -> Tuple[int, Any]:
    text = str(value or '').strip()
    for pattern in (
        '%Y-%m-%d', '%Y/%m/%d', '%Y%m%d',
        '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S',
    ):
        try:
            return 0, datetime.strptime(text, pattern)
        except ValueError:
            continue
    return 1, text


def _month_label(value: str) -> str:
    key = _date_key(value)
    if key[0] == 0:
        return key[1].strftime('%Y-%m')
    match = re.search(r'(20\d{2})[-/]?(\d{2})', str(value))
    return '%s-%s' % match.groups() if match else str(value)


def load_qmt_detail_files(
    result_path: Path, data: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Load optional QMT CSV exports referenced by a result JSON."""
    loaded: Dict[str, Dict[str, Any]] = {}
    for kind, spec in QMT_DETAIL_FILE_SPECS.items():
        reference = data.get(spec['field'])
        if reference is None or not str(reference).strip():
            continue
        source = Path(str(reference).strip())
        if not source.is_absolute():
            source = result_path.parent / source
        source = source.resolve()
        if not source.is_file():
            raise ValueError(
                'QMT detail file does not exist for %s: %s' % (
                    spec['field'], source,
                )
            )
        allow_empty = (
            kind == 'trades'
            and data.get('trade_count') is not None
            and int(data.get('trade_count') or 0) == 0
        )
        rows, encoding = _read_csv_rows(source, allow_empty=allow_empty)
        loaded[kind] = {
            'field': spec['field'],
            'path': source,
            'archive_name': spec['archive_name'],
            'encoding': encoding,
            'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'row_count': len(rows),
            'rows': rows,
        }
    return loaded


def qmt_detail_manifest(details: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {
        kind: {
            'result_field': item['field'],
            'source_path': str(item['path']),
            'archived_name': item['archive_name'],
            'encoding': item['encoding'],
            'sha256': item['sha256'],
            'row_count': item['row_count'],
        }
        for kind, item in details.items()
    }


def _daily_nav_summary(
    item: Dict[str, Any], qmt_data: Optional[Dict[str, Any]] = None
) -> List[str]:
    records = []
    for row in item['rows']:
        date = _row_value(row, 'date')
        total_asset = _number(_row_value(row, 'total_asset'))
        strategy_nav = _number(_row_value(row, 'strategy_nav'))
        value = total_asset if total_asset is not None else strategy_nav
        if date and value is not None and value > 0:
            records.append((date, value, total_asset, strategy_nav))
    if not records:
        raise ValueError(
            'daily_nav_file needs date plus total_asset or strategy_nav columns'
        )
    records.sort(key=lambda record: _date_key(record[0]))
    first_date, first_value = records[0][0], records[0][1]
    last_date, last_value = records[-1][0], records[-1][1]
    uses_total_asset = records[0][2] is not None
    configured_initial = None
    if qmt_data:
        configured_initial = _number(qmt_data.get('initial_asset'))
    anchor_value = (
        configured_initial
        if uses_total_asset and configured_initial is not None
        else (1.0 if not uses_total_asset else first_value)
    )
    anchor_date = (
        str(qmt_data.get('period_start')) + ' START'
        if qmt_data and qmt_data.get('period_start')
        else first_date
    )
    peak_value = anchor_value
    peak_date = anchor_date
    drawdown = 0.0
    drawdown_peak = peak_date
    drawdown_trough = peak_date
    period_returns = []
    previous = anchor_value
    for index, (date, value, _, _) in enumerate(records):
        if value > peak_value:
            peak_value = value
            peak_date = date
        current_drawdown = value / peak_value - 1.0
        if current_drawdown < drawdown:
            drawdown = current_drawdown
            drawdown_peak = peak_date
            drawdown_trough = date
        if previous > 0:
            period_returns.append((value / previous - 1.0, date))
        previous = value

    month_ends: Dict[str, Tuple[str, float]] = {}
    for date, value, _, _ in records:
        month_ends[_month_label(date)] = (date, value)
    monthly = []
    previous_value = anchor_value
    for month, (_, value) in month_ends.items():
        monthly.append((month, value / previous_value - 1.0))
        previous_value = value

    lines = [
        '### Daily NAV and assets',
        '',
        '- rows: %d' % item['row_count'],
        '- observed_period: %s to %s' % (first_date, last_date),
        '- calculated_period_return: %.6f' % (last_value / anchor_value - 1.0),
        '- calculated_max_drawdown: %.6f' % (-drawdown),
        '- max_drawdown_window: %s to %s' % (
            drawdown_peak, drawdown_trough,
        ),
    ]
    if period_returns:
        best = max(period_returns, key=lambda item: item[0])
        worst = min(period_returns, key=lambda item: item[0])
        lines.extend([
            '- best_single_period_return: %.6f on %s' % best,
            '- worst_single_period_return: %.6f on %s' % worst,
        ])
    if monthly:
        lines.append('- monthly_returns: ' + ', '.join(
            '%s=%.6f' % item for item in monthly
        ))
    return lines


def _normalized_side(value: str) -> str:
    text = str(value or '').strip().casefold()
    if text in ('b', 'buy', '买', '买入') or '买' in text:
        return 'buy'
    if text in ('s', 'sell', '卖', '卖出') or '卖' in text:
        return 'sell'
    return text or 'unknown'


def _trade_summary(
    item: Dict[str, Any],
    average_asset: Optional[float] = None,
    declared_trade_count: Optional[int] = None,
) -> List[str]:
    trades = []
    by_stock: Dict[str, float] = {}
    total_fees = 0.0
    realized_pnl = []
    holding_days = []
    for row in item['rows']:
        stock = _row_value(row, 'stock')
        side = _normalized_side(_row_value(row, 'side'))
        quantity = _number(_row_value(row, 'quantity'))
        price = _number(_row_value(row, 'price'))
        if not stock or quantity is None or price is None:
            continue
        amount = _number(_row_value(row, 'amount'))
        if amount is None:
            amount = abs(quantity * price)
        commission = _number(_row_value(row, 'commission')) or 0.0
        stamp_tax = _number(_row_value(row, 'stamp_tax')) or 0.0
        # QMT's native "盈利" column reports entry fees as negative P&L on
        # buy rows. Only sell rows represent realized P&L.
        pnl = (
            _number(_row_value(row, 'realized_pnl'))
            if side == 'sell' else None
        )
        holding = _number(_row_value(row, 'holding_days'))
        trade_time = _row_value(row, 'trade_time')
        trades.append((trade_time, stock, side, quantity, price, amount))
        by_stock[stock] = by_stock.get(stock, 0.0) + abs(amount)
        total_fees += commission + stamp_tax
        if pnl is not None:
            realized_pnl.append((pnl, stock, trade_time))
        if holding is not None and holding >= 0:
            holding_days.append(holding)
    if not trades and declared_trade_count == 0:
        return [
            '### Executed trades',
            '',
            '- raw_rows: %d' % item['row_count'],
            '- valid_trade_rows: 0',
            '- trade_count_reconciliation: declared=0, valid_csv_rows=0, match=yes',
            '- realized_pnl_analysis: unavailable (no executed trades)',
            '- holding_period_analysis: unavailable (no executed trades)',
        ]
    if not trades:
        raise ValueError(
            'trade_records_file needs stock, side, quantity and price columns'
        )
    counts: Dict[str, int] = {}
    for trade in trades:
        counts[trade[2]] = counts.get(trade[2], 0) + 1
    times = [trade[0] for trade in trades if trade[0]]
    top_stocks = sorted(by_stock.items(), key=lambda value: value[1], reverse=True)[:5]
    gross_amount = sum(abs(trade[5]) for trade in trades)
    lines = [
        '### Executed trades',
        '',
        '- raw_rows: %d' % item['row_count'],
        '- valid_trade_rows: %d' % len(trades),
        '- side_counts: ' + ', '.join(
            '%s=%d' % value for value in sorted(counts.items())
        ),
        '- unique_stocks: %d' % len(by_stock),
        '- gross_traded_amount: %.2f' % gross_amount,
        '- recorded_commission_and_stamp_tax: %.2f' % total_fees,
        '- recorded_fee_rate_on_traded_amount: %.8f' % (
            total_fees / gross_amount if gross_amount > 0 else 0.0
        ),
        '- top_stocks_by_traded_amount: ' + ', '.join(
            '%s=%.2f' % value for value in top_stocks
        ),
    ]
    if times:
        lines.append('- execution_time_span: %s to %s' % (min(times), max(times)))
    if average_asset is not None and average_asset > 0:
        lines.append(
            '- calculated_turnover_on_average_asset: %.6f'
            % (gross_amount / average_asset)
        )
    if declared_trade_count is not None:
        lines.append(
            '- trade_count_reconciliation: declared=%d, valid_csv_rows=%d, match=%s'
            % (
                declared_trade_count,
                len(trades),
                'yes' if declared_trade_count == len(trades) else 'no',
            )
        )
    if realized_pnl:
        profits = [value[0] for value in realized_pnl if value[0] > 0]
        losses = [value[0] for value in realized_pnl if value[0] < 0]
        gross_profit = sum(profits)
        gross_loss = abs(sum(losses))
        top_profits = sorted(realized_pnl, key=lambda value: value[0], reverse=True)
        top_losses = sorted(realized_pnl, key=lambda value: value[0])
        pnl_by_stock: Dict[str, float] = {}
        for pnl, stock, _ in realized_pnl:
            pnl_by_stock[stock] = pnl_by_stock.get(stock, 0.0) + pnl
        lines.extend([
            '- rows_with_realized_pnl: %d' % len(realized_pnl),
            '- recorded_realized_pnl_total: %.2f' % sum(
                value[0] for value in realized_pnl
            ),
            '- profitable_pnl_rows: %d' % sum(
                1 for value in realized_pnl if value[0] > 0
            ),
            '- losing_pnl_rows: %d' % sum(
                1 for value in realized_pnl if value[0] < 0
            ),
            '- gross_profit: %.2f' % gross_profit,
            '- gross_loss_absolute: %.2f' % gross_loss,
            '- profit_loss_ratio: %s' % (
                'inf' if gross_loss == 0 and gross_profit > 0
                else ('%.6f' % (gross_profit / gross_loss) if gross_loss else 'unavailable')
            ),
            '- average_profit: %s' % (
                '%.2f' % (gross_profit / len(profits)) if profits else 'unavailable'
            ),
            '- average_loss_absolute: %s' % (
                '%.2f' % (gross_loss / len(losses)) if losses else 'unavailable'
            ),
            '- largest_profit: %s' % (
                '%.2f on %s' % (top_profits[0][0], top_profits[0][1])
                if profits else 'unavailable'
            ),
            '- largest_loss: %s' % (
                '%.2f on %s' % (top_losses[0][0], top_losses[0][1])
                if losses else 'unavailable'
            ),
        ])
        if gross_profit > 0:
            sorted_profits = sorted(profits, reverse=True)
            lines.extend([
                '- largest_profit_share_of_gross_profit: %.6f'
                % (sorted_profits[0] / gross_profit),
                '- top3_profit_share_of_gross_profit: %.6f'
                % (sum(sorted_profits[:3]) / gross_profit),
            ])
        top_stock_pnl = sorted(
            pnl_by_stock.items(), key=lambda value: abs(value[1]), reverse=True
        )[:5]
        lines.append(
            '- top_stocks_by_absolute_realized_pnl: '
            + ', '.join('%s=%.2f' % value for value in top_stock_pnl)
        )
    else:
        lines.append(
            '- realized_pnl_analysis: unavailable (CSV has no realized_pnl values)'
        )
    if holding_days:
        lines.append(
            '- average_recorded_holding_days: %.4f'
            % (sum(holding_days) / len(holding_days))
        )
    else:
        lines.append(
            '- holding_period_analysis: unavailable (CSV has no holding_days values)'
        )
    return lines


def _order_summary(item: Dict[str, Any]) -> List[str]:
    statuses: Dict[str, int] = {}
    reasons: Dict[str, int] = {}
    valid = 0
    for row in item['rows']:
        status = _row_value(row, 'status') or 'unknown'
        reason = _row_value(row, 'reject_reason')
        statuses[status] = statuses.get(status, 0) + 1
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        valid += 1
    if not valid:
        raise ValueError('order_records_file has no usable rows')
    top_reasons = sorted(reasons.items(), key=lambda value: value[1], reverse=True)[:5]
    lines = [
        '### Orders and execution exceptions',
        '',
        '- rows: %d' % item['row_count'],
        '- status_counts: ' + ', '.join(
            '%s=%d' % value for value in sorted(statuses.items())
        ),
    ]
    if top_reasons:
        lines.append('- top_reject_or_failure_reasons: ' + ', '.join(
            '%s=%d' % value for value in top_reasons
        ))
    else:
        lines.append('- recorded_reject_or_failure_reasons: none')
    return lines


def _average_total_asset(item: Dict[str, Any]) -> Optional[float]:
    values = [
        _number(_row_value(row, 'total_asset')) for row in item['rows']
    ]
    usable = [value for value in values if value is not None and value > 0]
    return sum(usable) / len(usable) if usable else None


def _normalized_datetime(value: str) -> Optional[datetime]:
    key = _date_key(value)
    return key[1] if key[0] == 0 else None


def validate_qmt_detail_consistency(
    details: Dict[str, Dict[str, Any]], data: Dict[str, Any]
) -> None:
    """Reject detail files that belong to another period or contradict totals."""
    start = datetime.strptime(str(data['period_start']), '%Y-%m-%d').date()
    end = datetime.strptime(str(data['period_end']), '%Y-%m-%d').date()
    allowed_start = datetime.strptime(
        str(data.get('backtest_start') or data['period_start']), '%Y-%m-%d'
    ).date()
    daily = details.get('daily_nav')
    if daily:
        records = []
        for row in daily['rows']:
            dt = _normalized_datetime(_row_value(row, 'date'))
            total_asset = _number(_row_value(row, 'total_asset'))
            strategy_nav = _number(_row_value(row, 'strategy_nav'))
            if dt is not None and (total_asset is not None or strategy_nav is not None):
                records.append((dt.date(), total_asset, strategy_nav))
        if not records:
            raise ValueError('daily_nav_file has no valid dated NAV or asset rows')
        records.sort(key=lambda value: value[0])
        if any(item[0] < start or item[0] > end for item in records):
            raise ValueError(
                'daily_nav_file contains a date outside the configured period: %s to %s'
                % (start.isoformat(), end.isoformat())
            )
        if records[0][0] > start + timedelta(days=7) or records[-1][0] < end - timedelta(days=7):
            raise ValueError(
                'daily_nav_file does not cover the first and last trading-week '
                'boundaries of the configured period'
            )
        ending_asset = data.get('ending_asset')
        final_total_asset = records[-1][1]
        if ending_asset is not None and final_total_asset is not None:
            tolerance = max(0.05, abs(float(ending_asset)) * 1e-7)
            if abs(final_total_asset - float(ending_asset)) > tolerance:
                raise ValueError(
                    'daily_nav_file final total_asset does not match ending_asset'
                )

    for kind, field in (('trades', 'trade_time'), ('orders', 'order_time')):
        item = details.get(kind)
        if not item:
            continue
        for row in item['rows']:
            raw = _row_value(row, field)
            dt = _normalized_datetime(raw)
            if not raw:
                raise ValueError('%s contains a row without %s' % (item['field'], field))
            if dt is None or dt.date() < allowed_start or dt.date() > end:
                raise ValueError(
                    '%s contains a timestamp outside the configured period: %s'
                    % (item['field'], raw)
                )


def _detail_rows_in_evaluation_period(
    item: Dict[str, Any], field: str, qmt_data: Optional[Dict[str, Any]]
) -> Tuple[Dict[str, Any], int]:
    if not qmt_data or not qmt_data.get('period_start') or not qmt_data.get('period_end'):
        return item, 0
    start = datetime.strptime(str(qmt_data['period_start']), '%Y-%m-%d').date()
    end = datetime.strptime(str(qmt_data['period_end']), '%Y-%m-%d').date()
    rows = []
    excluded = 0
    for row in item['rows']:
        dt = _normalized_datetime(_row_value(row, field))
        if dt is not None and start <= dt.date() <= end:
            rows.append(row)
        else:
            excluded += 1
    filtered = dict(item)
    filtered['rows'] = rows
    filtered['row_count'] = len(rows)
    return filtered, excluded


def qmt_detail_summary(
    details: Dict[str, Dict[str, Any]],
    qmt_data: Optional[Dict[str, Any]] = None,
) -> str:
    if not details:
        return ''
    lines = [
        '# Locally derived QMT detail summary',
        '',
        'The raw CSV files are archived locally. Only this compact, deterministic ',
        'summary is supplied to the data-analysis model.',
        '',
    ]
    average_asset = (
        _average_total_asset(details['daily_nav'])
        if 'daily_nav' in details else None
    )
    if 'daily_nav' in details:
        lines.extend(_daily_nav_summary(details['daily_nav'], qmt_data))
        lines.append('')
    if 'trades' in details:
        declared_trade_count = None
        if qmt_data:
            if qmt_data.get('evaluation_trade_count') is not None:
                declared_trade_count = int(qmt_data['evaluation_trade_count'])
            elif qmt_data.get('trade_count') is not None:
                declared_trade_count = int(qmt_data['trade_count']) - int(
                    qmt_data.get('bootstrap_trade_count') or 0
                )
        evaluation_trades, excluded = _detail_rows_in_evaluation_period(
            details['trades'], 'trade_time', qmt_data
        )
        lines.extend(_trade_summary(
            evaluation_trades, average_asset, declared_trade_count
        ))
        if excluded:
            lines.append(
                '- pre_evaluation_trade_rows_excluded: %d' % excluded
            )
        lines.append('')
    if 'orders' in details:
        evaluation_orders, excluded = _detail_rows_in_evaluation_period(
            details['orders'], 'order_time', qmt_data
        )
        if evaluation_orders['rows']:
            lines.extend(_order_summary(evaluation_orders))
        else:
            lines.extend([
                '### Orders and execution exceptions', '',
                '- rows: 0',
                '- status_counts: none',
            ])
        if excluded:
            lines.append(
                '- pre_evaluation_order_rows_excluded: %d' % excluded
            )
        lines.append('')
    return '\n'.join(lines).rstrip()


def python_syntax_check(path: Path) -> Tuple[bool, str]:
    try:
        source = path.read_bytes()
        compile(source, str(path), 'exec')
        return True, 'syntax_ok'
    except Exception as exc:
        return False, str(exc)

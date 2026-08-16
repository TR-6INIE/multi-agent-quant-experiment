from __future__ import annotations

import difflib
import hashlib
import json
import math
import random
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .api_client import OpenAICompatibleClient, extract_json_object
from .artifacts import (
    load_qmt_detail_files,
    load_qmt_result,
    parse_audit_decision,
    parse_engineer_output,
    parse_open_audit_issue_ids,
    parse_plan_review_decision,
    python_syntax_check,
    qmt_result_to_markdown,
    qmt_detail_manifest,
    qmt_detail_summary,
    validate_qmt_detail_consistency,
)
from .config import (
    ConfigurationError,
    load_experiment_config,
    load_provider_configs,
)
from .literature import collect_literature, literature_to_markdown
from .local_backtest import (
    file_sha256,
    local_result_markdown,
    local_static_report,
    local_strategy_static_check,
    run_backtest_worker,
)
from .prompts import (
    HR_LEVEL_REQUIREMENTS,
    ROLE_NAMES,
    analyst_prompt,
    audit_followup_prompt,
    audit_prompt,
    development_engineer_revision_prompt,
    development_review_prompt,
    audit_finalize_prompt,
    engineer_prompt,
    engineer_revision_prompt,
    innovation_audit_prompt,
    plan_review_finalize_prompt,
    plan_review_handoff_prompt,
    plan_review_prompt,
    reflection_prompt,
    research_query_prompt,
    research_report_prompt,
    research_revision_prompt,
    self_report_prompt,
    system_prompt,
)
from .schedule import RoundSchedule, get_round_schedule
from .storage import (
    collect_text_files,
    copy_file,
    initialize_round_directories,
    read_json,
    read_text,
    round_directory,
    write_json,
    write_text,
)


ROLES = ('researcher', 'engineer', 'auditor', 'analyst')
LEVELS = ('初级', '中级', '高级')
STATE_SCHEMA_VERSION = 4


class ExperimentError(RuntimeError):
    pass


class PlanReviewFailure(ExperimentError):
    def __init__(
        self,
        message: str,
        research: str,
        attempts: int,
        returns_used: int,
    ):
        super().__init__(message)
        self.research = research
        self.attempts = attempts
        self.returns_used = returns_used


class ExperimentRunner:
    def __init__(
        self,
        group_kind: str,
        group_dir: Path,
        admin_dir: Path,
        models_config_path: Path,
        experiment_config_path: Path,
    ):
        if group_kind not in ('hr', 'reflection', 'control'):
            raise ValueError('Unknown group kind: %s' % group_kind)
        self.group_kind = group_kind
        self.group_dir = group_dir.resolve()
        self.admin_dir = admin_dir.resolve()
        self.models_config_path = models_config_path.resolve()
        self.experiment_config_path = experiment_config_path.resolve()
        self.experiment_config = load_experiment_config(
            self.experiment_config_path
        )
        self.state_path = self.group_dir / 'experiment_state.json'
        self._clients: Dict[str, OpenAICompatibleClient] = {}

    def initial_state(self) -> Dict[str, Any]:
        evolution = self.experiment_config.get('strategy_evolution') or {}
        strategy_mode = str(evolution.get('mode') or 'open_strategy_design')
        return {
            'schema_version': STATE_SCHEMA_VERSION,
            'strategy_mode': strategy_mode,
            'execution_backend': 'local_backtest',
            'group_kind': self.group_kind,
            'levels': {role: '初级' for role in ROLES},
            'rounds': {},
        }

    def load_state(self) -> Dict[str, Any]:
        state = read_json(self.state_path)
        if state is None:
            state = self.initial_state()
            write_json(self.state_path, state)
        expected_mode = str(
            (self.experiment_config.get('strategy_evolution') or {}).get('mode')
            or 'open_strategy_design'
        )
        if state.get('schema_version') != STATE_SCHEMA_VERSION:
            raise ExperimentError(
                'Existing experiment state belongs to another experiment schema. '
                'Archive it and initialize a new state before continuing.'
            )
        if state.get('strategy_mode') != expected_mode:
            raise ExperimentError(
                'State strategy_mode %s does not match configured mode %s' % (
                    state.get('strategy_mode'), expected_mode
                )
            )
        if state.get('execution_backend') != 'local_backtest':
            raise ExperimentError('State execution_backend is not local_backtest')
        if state.get('group_kind') != self.group_kind:
            raise ExperimentError(
                'State group_kind does not match this launcher: %s' % self.state_path
            )
        return state

    def save_state(self, state: Dict[str, Any]) -> None:
        write_json(self.state_path, state)

    def schedule(self, round_number: int) -> RoundSchedule:
        return get_round_schedule(self.experiment_config, round_number)

    def _client_for_role(self, role: str) -> OpenAICompatibleClient:
        provider_name = (self.experiment_config.get('role_models') or {}).get(role)
        if not provider_name:
            raise ConfigurationError('No model provider configured for role %s' % role)
        if provider_name not in self._clients:
            configs = load_provider_configs(self.models_config_path)
            if provider_name not in configs:
                raise ConfigurationError(
                    'Provider %s is missing from %s' % (
                        provider_name, self.models_config_path
                    )
                )
            self._clients[provider_name] = OpenAICompatibleClient(
                configs[provider_name]
            )
        return self._clients[provider_name]

    @staticmethod
    def _validate_analysis_response(analysis: str) -> None:
        """Reject obviously interrupted analyst responses before closing a round."""
        text = (analysis or '').strip()
        # Kimi often supplies every requested analysis item under descriptive or
        # numbered headings.  Validate substantive coverage in the full text,
        # rather than enforcing a particular heading vocabulary or order.
        required_topics = {
            'performance': lambda value: '收益' in value,
            'benchmark': lambda value: (
                '基准' in value or '科创50' in value
            ),
            'risk': lambda value: '回撤' in value,
            'execution': lambda value: (
                '交易' in value or '成交' in value or '换仓' in value
            ),
            'portfolio': lambda value: (
                '持仓' in value or '仓位' in value or '集中' in value
            ),
            'limitations': lambda value: any(
                marker in value
                for marker in ('不可证明', '无法', '数据不足', '未提供')
            ),
            'next_test': lambda value: (
                '下一轮' in value
                and any(marker in value for marker in ('建议', '优化', '验证'))
            ),
        }
        missing = [
            topic for topic, predicate in required_topics.items()
            if not predicate(text)
        ]
        if len(text) < 1200 or missing:
            raise ExperimentError(
                'Data-analysis response appears incomplete '
                '(chars=%d, missing_topics=%s). Rerun the submission; '
                'the round was not closed.' % (
                    len(text), ','.join(missing) if missing else 'none'
                )
            )

    def _generate_analysis(
        self,
        round_number: int,
        state: Dict[str, Any],
        paths: Dict[str, Path],
        label: str = '07_data_analysis',
    ) -> str:
        schedule = self.schedule(round_number)
        research = read_text(paths['shared'] / '01_research_report.md')
        audit = read_text(paths['shared'] / '04_audit_final.md')
        qmt_markdown = read_text(paths['shared'] / '06_qmt_result.md')
        deployment_path = paths['shared'] / '05_deployment_decision.json'
        if deployment_path.is_file():
            deployment = read_json(deployment_path)
            delivery_status = str(
                deployment.get('delivery_status') or 'unknown'
            )
            deployment_mode = str(
                deployment.get('deployment_mode') or 'unknown'
            )
            source_round = deployment.get('source_round')
            deployment_note = (
                '## 管理员确认的部署归属（优先级最高）\n\n'
                '- delivery_status: %s\n'
                '- deployment_mode: %s\n'
                '- source_round: %s\n\n'
            ) % (delivery_status, deployment_mode, source_round)
            if deployment_mode == 'reuse_baseline':
                deployment_note += (
                    '**本轮候选没有部署。下方冻结盲测运行的是本轮开始时的'
                    '未修改基线。不得把盲测收益、持仓或机制归因给本轮研究员'
                    '提出且已被否决/未交付的候选；开发候选哈希与冻结盲测哈希'
                    '不同是预期结果，不是版本缺陷。**\n\n'
                )
            elif deployment_mode == 'reuse_previous':
                deployment_note += (
                    '**本轮候选没有部署。下方结果属于上一轮策略继续运行，'
                    '不得归因给本轮候选。**\n\n'
                )
            elif deployment_mode == 'cash_only':
                deployment_note += (
                    '**本轮候选没有部署。下方结果属于账户保持现金，不能'
                    '解释为候选策略取得该收益。**\n\n'
                )
            else:
                deployment_note += (
                    '**下方结果属于本轮实际冻结部署代码。**\n\n'
                )
            qmt_markdown = deployment_note + qmt_markdown
        development_paths = sorted(
            paths['shared'].glob('04_development_result_attempt_*.md')
        )
        development_max_chars = int(
            (
                (
                    self.experiment_config.get('local_backtest') or {}
                ).get('development') or {}
            ).get('max_feedback_chars', 60000)
        )
        development_results = collect_text_files(
            development_paths,
            max_chars=development_max_chars,
        )
        if development_paths:
            final_development = read_text(development_paths[-1])
            development_results = (
                '## 管理员标注：最终开发期结果\n\n'
                '以下最高编号 attempt 是冻结候选对应的最终开发期结果，'
                '分析时必须以它为准；较低编号仅是历史试验，不得称为最终结果。\n\n'
                + final_development
                + '\n\n## 历史开发期尝试（仅用于比较）\n\n'
                + development_results
            )[:development_max_chars]
        previous_analysis = ''
        if round_number > 1:
            previous_analysis = read_text(
                round_directory(self.group_dir, round_number - 1)
                / 'shared' / '07_data_analysis.md'
            )
        cached_response = paths['logs'] / (label + '_response.md')
        if cached_response.exists():
            cached = read_text(cached_response)
            try:
                self._validate_analysis_response(cached)
                return cached
            except ExperimentError:
                pass
        analysis = self._call_model(
            'analyst',
            self._role_system(state, 'analyst', schedule),
            analyst_prompt(
                research,
                audit,
                qmt_markdown,
                previous_analysis,
                development_results,
            ),
            paths['logs'],
            label,
        )
        self._validate_analysis_response(analysis)
        return analysis

    def _complete_saved_post_round(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
    ) -> None:
        if not (paths['shared'] / '06_qmt_result.md').is_file():
            raise ExperimentError(
                'Cannot resume post-round processing: saved evaluation result is missing'
            )
        analysis = self._generate_analysis(schedule.number, state, paths)
        write_text(paths['shared'] / '07_data_analysis.md', analysis)
        result_markdown = read_text(paths['shared'] / '06_qmt_result.md')
        if self.group_kind == 'hr':
            self._complete_hr_post_round(
                state, schedule, paths, result_markdown
            )
        elif self.group_kind == 'reflection':
            self._complete_reflection_post_round(
                state, schedule, paths, result_markdown
            )
            self._set_round_status(state, schedule.number, 'complete')
        else:
            self._set_round_status(state, schedule.number, 'complete')

    def repair_control_analysis(self, round_number: int) -> None:
        """Repair an interrupted analysis without rerunning earlier experiment stages."""
        if self.group_kind != 'control':
            raise ExperimentError(
                'repair-analysis is limited to the control group because other '
                'groups have dependent post-round private artifacts'
            )
        state = self.load_state()
        item = self._round_state(state, round_number)
        if item.get('status') != 'complete':
            raise ExperimentError(
                'Round %d status is %s, not complete' % (
                    round_number, item.get('status')
                )
            )
        paths = initialize_round_directories(self.group_dir, round_number)
        analysis = self._generate_analysis(
            round_number,
            state,
            paths,
            label='07_data_analysis_repair',
        )
        write_text(paths['shared'] / '07_data_analysis.md', analysis)

    def _round_state(self, state: Dict[str, Any], round_number: int) -> Dict[str, Any]:
        return (state.get('rounds') or {}).get(str(round_number), {})

    def _set_round_status(
        self,
        state: Dict[str, Any],
        round_number: int,
        status: str,
        **extra: Any,
    ) -> None:
        rounds = state.setdefault('rounds', {})
        item = rounds.setdefault(str(round_number), {})
        item['status'] = status
        item.update(extra)
        self.save_state(state)

    def _validate_previous_round(self, state: Dict[str, Any], round_number: int) -> None:
        if round_number <= 1:
            return
        previous = self._round_state(state, round_number - 1)
        if previous.get('status') != 'complete':
            raise ExperimentError(
                'Round %d must be complete before starting round %d' % (
                    round_number - 1, round_number
                )
            )

    def _private_context(
        self, state: Dict[str, Any], role: str, round_number: int
    ) -> str:
        if round_number <= 1:
            return ''
        previous_root = round_directory(self.group_dir, round_number - 1)
        if self.group_kind == 'hr':
            return read_text(previous_root / 'private' / role / 'feedback.json')
        if self.group_kind == 'reflection':
            return read_text(previous_root / 'private' / role / 'reflection.md')
        return ''

    def _previous_shared_context(self, round_number: int) -> str:
        if round_number <= 1:
            return ''
        root = round_directory(self.group_dir, round_number - 1) / 'shared'
        files = [
            root / '01_research_report.md',
            root / '03_engineer_notes.md',
            root / '04_audit_final.md',
            root / '06_qmt_result.md',
            root / '07_data_analysis.md',
        ]
        max_chars = int(self.experiment_config.get('max_previous_context_chars', 60000))
        return collect_text_files(files, max_chars=max_chars)

    def _baseline_strategy_for_round(
        self,
        round_number: int,
        paths: Dict[str, Path],
    ) -> str:
        """Freeze the common round input before any model is called."""
        evolution = self.experiment_config.get('strategy_evolution') or {}
        mode = evolution.get('mode')
        if mode not in ('incremental_improvement', 'open_strategy_design'):
            raise ExperimentError(
                'Unsupported strategy_evolution.mode: %s' % mode
            )

        if round_number == 1:
            configured = str(evolution.get('initial_baseline_file') or '').strip()
            if not configured:
                raise ExperimentError('initial_baseline_file is not configured')
            source = (self.admin_dir / configured).resolve()
            try:
                source.relative_to(self.admin_dir)
            except ValueError:
                raise ExperimentError(
                    'initial_baseline_file must stay inside the administrator directory'
                )
            source_kind = (
                'configured_initial_scaffold'
                if mode == 'open_strategy_design'
                else 'configured_initial_baseline'
            )
            source_round = None
        else:
            source = (
                round_directory(self.group_dir, round_number - 1)
                / 'shared' / '05_strategy_frozen.py'
            )
            source_kind = 'previous_round_frozen_strategy'
            source_round = round_number - 1

        if not source.exists():
            raise ExperimentError('Baseline strategy is missing: %s' % source)
        source_text = read_text(source)
        if not source_text.strip():
            raise ExperimentError('Baseline strategy is empty: %s' % source)

        baseline_path = paths['shared'] / '00_baseline_strategy.py'
        write_text(baseline_path, source_text)
        syntax_ok, syntax_detail = python_syntax_check(baseline_path)
        if not syntax_ok:
            raise ExperimentError(
                'Baseline strategy does not compile: %s' % syntax_detail
            )
        digest = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
        write_json(paths['shared'] / '00_baseline_manifest.json', {
            'round': round_number,
            'source_kind': source_kind,
            'source_round': source_round,
            'source_name': source.name,
            'sha256': digest,
        })
        return read_text(baseline_path)

    def _strategy_evolution_rules(self) -> str:
        evolution = self.experiment_config.get('strategy_evolution') or {}
        if evolution.get('mode') == 'open_strategy_design':
            return """- 第一轮从无交易逻辑的安全接口骨架开始；接口骨架不表达任何策略观点；
- 研究员必须自行研究候选，不得要求提示词列举或推荐策略方向；
- 每轮只冻结一个完整候选方案，但可以保留、局部修改或整体替换参考代码中的策略逻辑；
- 不得扩大允许股票池：%s；
- 允许在安全接口和资源上限内选择数据频率：%s；
- 不得增加安全策略上下文以外的数据接口：%s；
- 每轮最多三次代码提交，静态/运行修复与开发期改进共用该上限；
- 冻结前只能看到2022-01-01至2024-12-31开发期结果；可以据此选择冻结或提出有依据的有限改进；
- 候选冻结并记录哈希后才单次运行本轮评价区间；当轮任何模型在冻结前都看不到评价期收益，不得按评价期收益调参；
- 必须说明相对参考代码保留、替换和新增的部分，以及资源、历史窗口、参数自由度和失效条件；
- 资源规则：%s。
""" % (
                '是' if not evolution.get('allow_universe_expansion', False) else '否',
                '是' if evolution.get('allow_bar_frequency_increase', False) else '否',
                '是' if not evolution.get('allow_new_data_interfaces', False) else '否',
                evolution.get(
                    'resource_policy',
                    '不得显著增加本地回测运行负担',
                ),
            )
        return """- 每轮最多实施%s项核心机制改进；
- 不得扩大基线股票池：%s；
- 不得提高基线数据频率：%s；
- 不得增加安全策略上下文以外的数据接口：%s；
- 资源规则：%s；
- 基线中已实际运行的接口和结构可以原样保留；本地策略模板只约束新增或改动部分；
- 必须列出保留项、修改项和资源增量，不允许从零重写为另一套策略。
""" % (
            evolution.get('max_core_changes_per_round', 1),
            '是' if not evolution.get('allow_universe_expansion', False) else '否',
            '是' if not evolution.get('allow_bar_frequency_increase', False) else '否',
            '是' if not evolution.get('allow_new_data_interfaces', False) else '否',
            evolution.get(
                'resource_policy',
                '不得显著增加相对基线的运行负担',
            ),
        )

    def _experiment_constraints(self, schedule: RoundSchedule) -> str:
        rules = self.experiment_config.get('trading_rules') or {}
        round_asset, round_positions = self._round_starting_account(
            schedule.number
        )
        if self._position_volume_map(round_positions):
            carryover = (
                '- 本轮存在承接持仓；冻结撮合引擎直接恢复上一轮期末现金、持仓、成本与'
                '策略选中状态，不产生虚构的初始化成交；\n'
            )
        else:
            carryover = (
                '- 本轮承接持仓为空；直接从评价区间第一天开始，不需要提前初始化；\n'
            )
        return """- 实验第一轮初始资金：%s元；本轮必须承接的期初资产：%.2f元；
- 本轮必须承接的期初持仓：%s；
%s- 跨季度资金和持仓连续运行，不得重置为50万元；
- 允许交易：%s；
- T+1，只做多，禁止杠杆、融资融券和负现金；
- 最大持仓数量不限制，单只股票最大权重不限制，总目标权重不得超过100%%；
- 佣金：%s；印花税：%s；滑点：%s；
- 行情输入：日频模式历史截至上一交易日；固定时点模式只提供早于成交K线的快照；原生一分钟模式逐根提供已完成OHLCVA，订单仅在下一根一分钟K线开盘处理；实际成交价仅供冻结Broker；
- 候选代码不得读文件、网络、完整缓存或评价期未来数组；
- 股票池和申万一级行业采用固定当前快照，保留幸存者偏差与分类前视偏差；
- 本轮决策信息截止：%s；评价区间：%s至%s。
""" % (
            rules.get('initial_asset', 500000),
            round_asset,
            json.dumps(round_positions, ensure_ascii=False),
            carryover,
            rules.get('universe', '上交所和深交所股票'),
            rules.get('commission_rate', 0.000285),
            rules.get('stamp_tax_rate', 0.00025),
            rules.get('slippage_rate', 0),
            schedule.decision_as_of,
            schedule.evaluation_start,
            schedule.evaluation_end,
        )

    @staticmethod
    def _position_volume_map(positions: Any) -> Dict[str, float]:
        result: Dict[str, float] = {}
        if not isinstance(positions, list):
            return result
        for position in positions:
            if not isinstance(position, dict):
                continue
            stock = str(position.get('stock') or '').strip().upper()
            if not stock:
                continue
            try:
                volume = float(position.get('volume') or 0)
            except (TypeError, ValueError):
                raise ExperimentError(
                    'Position volume must be numeric for %s' % stock
                )
            if abs(volume) > 1e-9:
                result[stock] = result.get(stock, 0.0) + volume
        return result

    def _expected_qmt_settings(self) -> Dict[str, Any]:
        rules = self.experiment_config.get('trading_rules') or {}
        return {
            'commission_rate': float(rules.get('commission_rate', 0.000285)),
            'stamp_tax_rate': float(rules.get('stamp_tax_rate', 0.00025)),
            'slippage_rate': float(rules.get('slippage_rate', 0)),
            'bar_period': str(rules.get('bar_period', '5m')),
            't_plus_one': bool(rules.get('t_plus_one', True)),
            'leverage_used': False,
        }

    def _round_starting_account(
        self, round_number: int
    ) -> Tuple[float, List[Dict[str, Any]]]:
        if round_number <= 1:
            rules = self.experiment_config.get('trading_rules') or {}
            return float(rules.get('initial_asset', 500000)), []
        previous = read_json(
            round_directory(self.group_dir, round_number - 1)
            / 'shared' / '06_qmt_result.json',
            default={},
        ) or {}
        if previous.get('ending_asset') is None:
            raise ExperimentError(
                'Previous round ending_asset is required for account continuity'
            )
        positions = previous.get('ending_positions')
        if not isinstance(positions, list):
            raise ExperimentError(
                'Previous round ending_positions must be recorded for continuity'
            )
        return float(previous['ending_asset']), positions

    def _local_backtest_config(self) -> Dict[str, Any]:
        config = self.experiment_config.get('local_backtest') or {}
        required = (
            'package_root', 'cache', 'industry_dir',
            'benchmark_qmt_datadir', 'qmt_datadir', 'snapshot_cache_dir',
        )
        missing = [key for key in required if not str(config.get(key) or '').strip()]
        if missing:
            raise ExperimentError(
                'local_backtest configuration is missing: %s' % ', '.join(missing)
            )
        return config

    def _development_backtest_config(self) -> Tuple[Dict[str, Any], str, str]:
        base = dict(self._local_backtest_config())
        development = dict(base.get('development') or {})
        required = ('start', 'end', 'cache', 'fundamental_cache', 'manifest')
        missing = [
            key for key in required
            if not str(development.get(key) or '').strip()
        ]
        if missing:
            raise ExperimentError(
                'local_backtest.development is missing: %s' % ', '.join(missing)
            )
        for key in ('cache', 'fundamental_cache', 'manifest'):
            if not Path(str(development[key])).is_file():
                raise ExperimentError(
                    'Development cache artifact is missing: %s'
                    % development[key]
                )
        start = str(development['start'])
        end = str(development['end'])
        if end.replace('-', '') > '20241231':
            raise ExperimentError(
                'Development period must end no later than 2024-12-31'
            )
        base['cache'] = str(development['cache'])
        base['fundamental_cache'] = str(development['fundamental_cache'])
        base['initial_cash'] = float(development.get('initial_cash', 500000))
        if str(development.get('snapshot_cache_dir') or '').strip():
            base['snapshot_cache_dir'] = str(development['snapshot_cache_dir'])
        return base, start, end

    def _initial_portfolio_for_round(self, round_number: int) -> Optional[Path]:
        if round_number <= 1:
            return None
        path = (
            round_directory(self.group_dir, round_number - 1)
            / 'input' / 'local_backtest' / 'ending_portfolio.json'
        )
        if not path.exists():
            raise ExperimentError(
                'Previous round local ending portfolio is missing: %s' % path
            )
        return path

    def _run_local_candidate(
        self,
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        attempt: int,
        candidate_path: Path,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]], Path]:
        config = self._local_backtest_config()
        output_dir = paths['admin'] / ('local_backtest_attempt_%02d' % attempt)
        ok, detail, summary = run_backtest_worker(
            self.admin_dir / 'local_backtest_worker.py',
            candidate_path,
            output_dir,
            config,
            schedule.evaluation_start,
            schedule.evaluation_end,
            self._initial_portfolio_for_round(schedule.number),
            int(config.get('timeout_seconds', 600)),
        )
        write_text(output_dir / 'verification.md', detail)
        return ok, detail, summary, output_dir

    def _run_development_candidate(
        self,
        paths: Dict[str, Path],
        attempt: int,
        candidate_path: Path,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]], Path]:
        config, start, end = self._development_backtest_config()
        output_dir = paths['admin'] / (
            'development_backtest_attempt_%02d' % attempt
        )
        ok, detail, summary = run_backtest_worker(
            self.admin_dir / 'local_backtest_worker.py',
            candidate_path,
            output_dir,
            config,
            start,
            end,
            None,
            int(config.get('timeout_seconds', 600)),
        )
        write_text(output_dir / 'verification.md', detail)
        return ok, detail, summary, output_dir

    def _development_result_text(
        self,
        paths: Dict[str, Path],
        attempt: int,
        output_dir: Path,
        summary: Dict[str, Any],
    ) -> str:
        development = (
            (self.experiment_config.get('local_backtest') or {})
            .get('development') or {}
        )
        result_path = paths['shared'] / (
            '04_development_result_attempt_%d.json' % attempt
        )
        markdown_path = paths['shared'] / (
            '04_development_result_attempt_%d.md' % attempt
        )
        write_json(result_path, summary)
        detail = collect_text_files(
            [
                output_dir / 'equity.csv', output_dir / 'trades.csv',
                output_dir / 'state.csv', output_dir / 'orders.csv',
            ],
            max_chars=int(development.get('max_feedback_chars', 60000)),
        )
        markdown = (
            '# Development backtest only\n\n'
            'This result covers 2022-01-01 through 2024-12-31 and may be used '
            'for frozen-candidate development. It is not the experiment '
            'evaluation result. No 2025+ evaluation metric is included.\n\n'
            + local_result_markdown(summary)
        )
        if detail:
            markdown += '\n\n## Development detail files\n' + detail
        write_text(markdown_path, markdown)
        return markdown

    @staticmethod
    def _parse_development_decision(text: str) -> Optional[str]:
        match = re.search(
            r'^\s*DEVELOPMENT_DECISION\s*:\s*(FREEZE|REJECT|REVISE)\s*$',
            text or '',
            flags=re.IGNORECASE | re.MULTILINE,
        )
        return match.group(1).upper() if match else None

    def _finalize_local_result(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        output_dir: Path,
        summary: Dict[str, Any],
        frozen_sha256: str,
    ) -> None:
        expected_start = schedule.evaluation_start.replace('-', '')
        expected_end = schedule.evaluation_end.replace('-', '')
        if str(summary.get('period_start')) != expected_start:
            raise ExperimentError('Local backtest period_start does not match schedule')
        if str(summary.get('period_end')) != expected_end:
            raise ExperimentError('Local backtest period_end does not match schedule')
        if str(summary.get('candidate_sha256') or '').lower() != frozen_sha256.lower():
            raise ExperimentError(
                'Local backtest candidate hash does not match frozen strategy'
            )
        for key in (
            'initial_asset', 'ending_asset', 'strategy_total_return',
            'strategy_annualized_return', 'benchmark_annualized_return',
            'max_drawdown',
        ):
            try:
                value = float(summary[key])
            except (KeyError, TypeError, ValueError):
                raise ExperimentError('Local backtest summary is missing %s' % key)
            if not math.isfinite(value):
                raise ExperimentError('Local backtest summary %s is not finite' % key)
        local_config = self.experiment_config.get('local_backtest') or {}
        data_spec = summary.get('strategy_data_spec') or {}
        financial_fields = list(data_spec.get('fundamental_fields') or [])
        if len(financial_fields) > int(
            local_config.get('max_fundamental_fields', 8)
        ):
            raise ExperimentError('Local backtest used too many financial fields')
        if financial_fields:
            provenance = summary.get('fundamental_provenance') or {}
            if list(provenance.get('fields') or []) != financial_fields:
                raise ExperimentError(
                    'Financial provenance fields do not match strategy data_spec'
                )
            if provenance.get('cutoff_rule') != (
                'latest announcement available by previous trading session'
            ):
                raise ExperimentError('Financial point-in-time cutoff rule is invalid')
            cache_path = Path(str(local_config.get('fundamental_cache') or ''))
            if not cache_path.is_file():
                raise ExperimentError('Configured financial cache is missing')
            if str(summary.get('fundamental_cache_sha256') or '').lower() != (
                file_sha256(cache_path).lower()
            ):
                raise ExperimentError('Financial cache hash does not match configuration')
        stable_dir = paths['input'] / 'local_backtest'
        artifact_names = (
            'summary.json', 'ending_portfolio.json', 'trades.csv', 'equity.csv',
            'orders.csv', 'equity_minute.csv', 'state.csv',
            'stdout.log', 'stderr.log', 'command.json',
            'verification.md',
        )
        for name in artifact_names:
            source = output_dir / name
            if source.exists():
                copy_file(source, stable_dir / name)

        starting_asset, starting_positions = self._round_starting_account(
            schedule.number
        )
        if abs(float(summary['initial_asset']) - starting_asset) > 0.01:
            raise ExperimentError(
                'Local backtest initial_asset does not equal the previous '
                'round ending_asset'
            )
        actual_start = self._position_volume_map(
            summary.get('starting_positions') or []
        )
        expected_start = self._position_volume_map(starting_positions)
        if actual_start != expected_start:
            raise ExperimentError(
                'Local backtest starting_positions do not match the previous round'
            )
        rules = self.experiment_config.get('trading_rules') or {}
        recorded_settings = self._expected_qmt_settings()
        if (summary.get('strategy_data_spec') or {}).get('engine_mode') == 'minute':
            recorded_settings.update({
                'bar_period': '1m',
                'execution_price': 'next_global_1m_bar_open',
                'max_volume_participation': summary.get(
                    'max_volume_participation'
                ),
            })
        result = dict(summary)
        result.update({
            'round': schedule.number,
            'group': self.group_dir.name,
            'period_start': schedule.evaluation_start,
            'period_end': schedule.evaluation_end,
            'initial_asset': float(summary.get('initial_asset', starting_asset)),
            'starting_positions': summary.get(
                'starting_positions', starting_positions
            ),
            'executed_strategy_sha256': frozen_sha256,
            'qmt_settings': recorded_settings,
            'daily_nav_file': str(stable_dir / 'equity.csv'),
            'trade_records_file': str(stable_dir / 'trades.csv'),
            'order_records_file': (
                str(stable_dir / 'orders.csv')
                if (stable_dir / 'orders.csv').exists() else None
            ),
            'execution_backend': 'frozen_local_backtest',
            'snapshot_policy': str(local_config.get('snapshot_policy') or ''),
            'benchmark_name': summary.get(
                'benchmark_name', rules.get('benchmark_name', '科创50')
            ),
        })
        write_json(paths['shared'] / '06_local_backtest_result.json', result)
        # Keep the historical filename so existing cross-round analysis and HR
        # code can consume the new backend without losing continuity.
        write_json(paths['shared'] / '06_qmt_result.json', result)
        detail_text = collect_text_files(
            [
                stable_dir / 'trades.csv', stable_dir / 'orders.csv',
                stable_dir / 'equity.csv', stable_dir / 'state.csv',
            ],
            max_chars=int(
                self.experiment_config.get('max_backtest_detail_chars', 80000)
            ),
        )
        markdown = local_result_markdown(result)
        if detail_text:
            markdown += '\n\n## Local backtest detail files\n' + detail_text
        write_text(paths['shared'] / '06_local_backtest_result.md', markdown)
        write_text(paths['shared'] / '06_qmt_result.md', markdown)
        write_json(paths['shared'] / '06_local_backtest_manifest.json', {
            'candidate_sha256': frozen_sha256,
            'engine_sha256': result.get('engine_sha256'),
            'engine_version': result.get('engine_version'),
            'cache_sha256': result.get('cache_sha256'),
            'industry_snapshot_sha256': result.get('industry_snapshot_sha256'),
            'artifacts': {
                name: file_sha256(stable_dir / name)
                for name in artifact_names if (stable_dir / name).exists()
            },
        })

        self._complete_saved_post_round(state, schedule, paths)

    def _write_round_qmt_template(
        self,
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        frozen_sha256: str,
    ) -> None:
        initial_asset, starting_positions = self._round_starting_account(
            schedule.number
        )
        rules = self.experiment_config.get('trading_rules') or {}
        template = {
            'round': schedule.number,
            'group': self.group_dir.name,
            'backtest_start': (
                schedule.decision_as_of
                if self._position_volume_map(starting_positions)
                else schedule.evaluation_start
            ),
            'carryover_mode': (
                'synthetic_previous_close'
                if self._position_volume_map(starting_positions)
                else 'none_empty'
            ),
            'bootstrap_trade_count': 0,
            'period_start': schedule.evaluation_start,
            'period_end': schedule.evaluation_end,
            'initial_asset': initial_asset,
            'starting_positions': starting_positions,
            'ending_asset': None,
            'cash': None,
            'strategy_total_return': None,
            'strategy_annualized_return': None,
            'benchmark_name': rules.get('benchmark_name', '科创50'),
            'benchmark_annualized_return': None,
            'max_drawdown': None,
            'volatility': None,
            'sharpe': None,
            'win_rate': None,
            'profit_loss_ratio': None,
            'trade_count': None,
            'evaluation_trade_count': None,
            'turnover_rate': None,
            'ending_positions': None,
            'executed_strategy_sha256': frozen_sha256,
            'qmt_settings': self._expected_qmt_settings(),
            'daily_nav_file': None,
            'trade_records_file': None,
            'order_records_file': None,
            'notes': (
                'Do not change initial_asset, starting_positions, period, group, '
                'executed_strategy_sha256 or qmt_settings. Round 2 and later '
                'require trade_records_file. daily_nav_file and '
                'order_records_file are optional when QMT cannot export them.'
            ),
        }
        write_json(paths['admin'] / 'qmt_result_template.json', template)
        write_text(
            paths['admin'] / 'qmt_run_requirements.md',
            '# QMT run requirements\n\n'
            '- group: %s\n'
            '- round: %d\n'
            '- period: %s to %s\n'
            '- required initial asset: %.2f\n'
            '- required starting positions: `%s`\n'
            '- frozen strategy SHA-256: `%s`\n'
            '- benchmark: %s\n'
            '- QMT settings: `%s`\n'
            % (
                self.group_dir.name,
                schedule.number,
                schedule.evaluation_start,
                schedule.evaluation_end,
                initial_asset,
                json.dumps(starting_positions, ensure_ascii=False),
                frozen_sha256,
                rules.get('benchmark_name', '科创50'),
                json.dumps(self._expected_qmt_settings(), ensure_ascii=False),
            ),
        )

    def _validate_qmt_submission(
        self,
        state: Dict[str, Any],
        round_number: int,
        data: Dict[str, Any],
    ) -> None:
        if round_number < 2:
            return
        required = (
            'initial_asset', 'ending_asset', 'cash', 'strategy_total_return',
            'starting_positions', 'ending_positions', 'benchmark_name',
            'executed_strategy_sha256', 'qmt_settings',
        )
        missing = [key for key in required if data.get(key) is None]
        if missing:
            raise ExperimentError(
                'Round %d QMT result is missing continuity fields: %s'
                % (round_number, ', '.join(missing))
            )

        expected_asset, expected_positions = self._round_starting_account(
            round_number
        )
        if abs(float(data['initial_asset']) - expected_asset) > 0.01:
            raise ExperimentError(
                'initial_asset does not equal the previous round ending_asset'
            )
        actual_positions = self._position_volume_map(data['starting_positions'])
        expected_position_map = self._position_volume_map(expected_positions)
        if set(actual_positions) != set(expected_position_map) or any(
            abs(actual_positions[stock] - expected_position_map[stock]) > 1e-9
            for stock in expected_position_map
        ):
            raise ExperimentError(
                'starting_positions do not match the previous round ending_positions'
            )
        if not isinstance(data['ending_positions'], list):
            raise ExperimentError('ending_positions must be a JSON list')
        for position in data['ending_positions']:
            if not isinstance(position, dict):
                raise ExperimentError('Each ending position must be a JSON object')
            try:
                volume = float(position.get('volume') or 0)
            except (TypeError, ValueError):
                raise ExperimentError('ending position volume must be numeric')
            if abs(volume) > 1e-9 and (
                not str(position.get('stock') or '').strip()
                or position.get('market_value') is None
            ):
                raise ExperimentError(
                    'Nonzero ending positions require stock, volume and market_value'
                )

        has_starting_positions = bool(expected_position_map)
        carryover_mode = str(data.get('carryover_mode') or '').strip()
        backtest_start = str(
            data.get('backtest_start') or data.get('period_start')
        )
        if has_starting_positions:
            if carryover_mode not in ('native_positions', 'synthetic_previous_close'):
                raise ExperimentError(
                    'Nonempty starting positions require an approved carryover_mode'
                )
            expected_backtest_start = (
                str(self.schedule(round_number).decision_as_of)
                if carryover_mode == 'synthetic_previous_close'
                else str(data['period_start'])
            )
            if backtest_start != expected_backtest_start:
                raise ExperimentError(
                    'backtest_start does not match the selected carryover_mode'
                )
        elif carryover_mode and carryover_mode != 'none_empty':
            raise ExperimentError(
                'Empty starting positions must use carryover_mode none_empty'
            )

        item = self._round_state(state, round_number)
        expected_sha = str(item.get('frozen_sha256') or '').lower()
        actual_sha = str(data['executed_strategy_sha256']).strip().lower()
        if actual_sha != expected_sha:
            raise ExperimentError(
                'executed_strategy_sha256 does not match the frozen strategy'
            )

        rules = self.experiment_config.get('trading_rules') or {}
        expected_benchmark = str(rules.get('benchmark_name', '科创50')).strip()
        if str(data['benchmark_name']).strip() != expected_benchmark:
            raise ExperimentError(
                'benchmark_name must be %s' % expected_benchmark
            )
        settings = data['qmt_settings']
        if not isinstance(settings, dict):
            raise ExperimentError('qmt_settings must be a JSON object')
        for key, expected in self._expected_qmt_settings().items():
            if key not in settings:
                raise ExperimentError('qmt_settings is missing %s' % key)
            actual = settings[key]
            if isinstance(expected, float):
                try:
                    matches = abs(float(actual) - expected) <= 1e-12
                except (TypeError, ValueError):
                    matches = False
            elif isinstance(expected, bool):
                matches = isinstance(actual, bool) and actual is expected
            else:
                matches = str(actual).strip().lower() == expected.lower()
            if not matches:
                raise ExperimentError(
                    'qmt_settings.%s does not match experiment configuration' % key
                )

        initial_asset = float(data['initial_asset'])
        ending_asset = float(data['ending_asset'])
        if initial_asset <= 0 or ending_asset < 0 or float(data['cash']) < -0.01:
            raise ExperimentError('QMT assets or cash are invalid')
        calculated_return = ending_asset / initial_asset - 1.0
        if abs(calculated_return - float(data['strategy_total_return'])) > 5e-4:
            raise ExperimentError(
                'strategy_total_return does not reconcile with initial/ending assets'
            )

    def _role_system(
        self,
        state: Dict[str, Any],
        role: str,
        schedule: RoundSchedule,
    ) -> str:
        level = self._shared_work_level(state, role, schedule.number)
        return system_prompt(
            self.group_kind,
            role,
            schedule,
            level=level,
            private_context=self._private_context(
                state, role, schedule.number
            ),
        )

    def _shared_work_level(
        self,
        state: Dict[str, Any],
        role: str,
        round_number: int,
    ) -> str:
        """Synchronize all groups to the HR group's current role workload."""
        if self.group_kind == 'hr':
            return (state.get('levels') or {}).get(role, '初级')

        hr_state_path = self.group_dir.parent / '方法1' / 'experiment_state.json'
        hr_state = read_json(hr_state_path)
        if hr_state is None:
            if round_number == 1:
                return '初级'
            raise ExperimentError(
                'Method 1 state is required to synchronize round workload: %s'
                % hr_state_path
            )
        if round_number > 1:
            previous = (hr_state.get('rounds') or {}).get(str(round_number - 1), {})
            if previous.get('status') != 'complete':
                raise ExperimentError(
                    'Method 1 round %d must complete its evaluation before methods '
                    '2 and 3 can receive synchronized round %d requirements.' % (
                        round_number - 1, round_number
                    )
                )
        return (hr_state.get('levels') or {}).get(role, '初级')

    def _call_model(
        self,
        role: str,
        system_text: str,
        user_text: str,
        log_dir: Path,
        label: str,
        max_tokens_override: Optional[int] = None,
    ) -> str:
        request_record = {
            'role': role,
            'role_name': ROLE_NAMES[role],
            'provider': (self.experiment_config.get('role_models') or {}).get(role),
            'messages': [
                {'role': 'system', 'content': system_text},
                {'role': 'user', 'content': user_text},
            ],
        }
        write_json(log_dir / (label + '_request.json'), request_record)
        if max_tokens_override is not None:
            request_record['max_tokens_override'] = max_tokens_override
            write_json(log_dir / (label + '_request.json'), request_record)
        is_audit_finalize = role == 'auditor' and label.endswith('_finalize')
        allow_reasoning_fallback = (
            (
                role == 'auditor'
                and label.startswith(
                    ('02_plan_review_attempt_', '04_audit_attempt_')
                )
                and not is_audit_finalize
            )
        )
        if is_audit_finalize:
            request_record['stream_override'] = False
        if allow_reasoning_fallback:
            request_record['allow_reasoning_fallback'] = True
        if is_audit_finalize or allow_reasoning_fallback:
            write_json(log_dir / (label + '_request.json'), request_record)
        response = self._client_for_role(role).chat(
            request_record['messages'],
            max_tokens_override=max_tokens_override,
            stream_override=False if is_audit_finalize else None,
            allow_reasoning_fallback=allow_reasoning_fallback,
        )
        write_text(log_dir / (label + '_response.md'), response)
        return response

    def _finalize_audit_protocol(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        audit: str,
        attempt: int,
    ) -> Tuple[str, str]:
        decision = parse_audit_decision(audit)
        open_issues = parse_open_audit_issue_ids(audit)
        needs_repair = decision is None or (
            decision in ('REVISE', 'BLOCK') and not open_issues
        )
        if not needs_repair:
            if decision == 'PASS' and open_issues:
                audit = (
                    audit.rstrip()
                    + '\n\nPROTOCOL_NORMALIZATION: PASS contained open BLOCKER/'
                    'MAJOR issues and was treated as REVISE.'
                )
                return audit, 'REVISE'
            return self._normalize_block_decision(audit, decision)

        finalized = self._call_model(
            'auditor',
            self._role_system(state, 'auditor', schedule),
            audit_finalize_prompt(audit),
            paths['logs'],
            '04_audit_attempt_%d_finalize' % attempt,
            max_tokens_override=8000,
        )
        decision = parse_audit_decision(finalized)
        if decision is None:
            raise ExperimentError(
                'Auditor response protocol failed at attempt %d: no explicit '
                'DECISION after one format-repair call' % attempt
            )
        if (
            decision in ('REVISE', 'BLOCK')
            and not parse_open_audit_issue_ids(finalized)
        ):
            raise ExperimentError(
                'Auditor response protocol failed at attempt %d: no structured '
                'open BLOCKER/MAJOR issue after one format-repair call' % attempt
            )
        if decision == 'PASS' and parse_open_audit_issue_ids(finalized):
            finalized += (
                '\n\nPROTOCOL_NORMALIZATION: PASS contained open BLOCKER/'
                'MAJOR issues and was treated as REVISE.'
            )
            decision = 'REVISE'
        return self._normalize_block_decision(finalized, decision)

    def _finalize_plan_review_protocol(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        review: str,
        attempt: int,
    ) -> Tuple[str, str]:
        decision = parse_plan_review_decision(review)
        if decision is not None:
            return review, decision

        finalized = self._call_model(
            'auditor',
            self._role_system(state, 'auditor', schedule),
            plan_review_finalize_prompt(review),
            paths['logs'],
            '02_plan_review_attempt_%d_finalize' % attempt,
            max_tokens_override=8000,
        )
        decision = parse_plan_review_decision(finalized)
        if decision is None:
            raise ExperimentError(
                'Plan reviewer protocol failed at attempt %d: no explicit '
                'PLAN_DECISION after one format-repair call' % attempt
            )
        return finalized, decision

    @staticmethod
    def _normalize_block_decision(audit: str, decision: str) -> Tuple[str, str]:
        if decision != 'BLOCK':
            return audit, decision
        marker = 'BLOCK_SCOPE: UNFIXABLE_RESEARCH_CONFLICT'
        if marker in audit:
            return audit, decision
        normalized = (
            audit.rstrip()
            + '\n\nPROTOCOL_NORMALIZATION: BLOCK lacked the required '
            + marker
            + ' marker and was treated as REVISE.'
        )
        return normalized, 'REVISE'

    def _literature_for_round(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        previous_context: str,
        baseline_code: str,
        evolution_rules: str,
    ) -> str:
        response = self._call_model(
            'researcher',
            self._role_system(state, 'researcher', schedule),
            research_query_prompt(
                previous_context, baseline_code, evolution_rules
            ),
            paths['logs'],
            '01_research_queries',
        )
        try:
            parsed = extract_json_object(response)
            queries = [
                str(item).strip()
                for item in (parsed.get('queries') or [])
                if str(item).strip()
            ][:3]
        except (ValueError, TypeError):
            queries = []
        if not queries:
            queries = [
                'momentum factor crash risk robustness',
                'market breadth regime switching equity',
                'portfolio turnover transaction cost control',
            ]
        write_json(paths['shared'] / '00_research_queries.json', {'queries': queries})

        literature_config = self.experiment_config.get('literature_search') or {}
        if literature_config.get('enabled', True):
            collection = collect_literature(
                queries,
                schedule.decision_as_of,
                int(literature_config.get('per_database_limit', 3)),
            )
        else:
            collection = {
                'as_of': schedule.decision_as_of,
                'queries': queries,
                'results': [],
                'errors': [{'error': 'Automated literature search disabled'}],
            }
        write_json(paths['shared'] / '00_literature.json', collection)
        markdown = literature_to_markdown(collection)
        write_text(paths['shared'] / '00_literature.md', markdown)
        return markdown

    def _research_plan_for_round(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        previous_context: str,
        constraints: str,
        template: str,
        baseline_code: str,
        evolution_rules: str,
    ) -> Tuple[str, int, int]:
        literature_path = paths['shared'] / '00_literature.md'
        if literature_path.exists():
            literature = read_text(literature_path)
        else:
            literature = self._literature_for_round(
                state,
                schedule,
                paths,
                previous_context,
                baseline_code,
                evolution_rules,
            )

        max_returns = int(
            self.experiment_config.get('max_plan_review_returns', 2)
        )
        max_attempts = max_returns + 1
        current_research = ''
        returns_used = 0

        for attempt in range(1, max_attempts + 1):
            research_attempt_path = paths['shared'] / (
                '01_research_report_attempt_%d.md' % attempt
            )
            if research_attempt_path.exists():
                current_research = read_text(research_attempt_path)
            elif attempt == 1:
                current_research = self._call_model(
                    'researcher',
                    self._role_system(state, 'researcher', schedule),
                    research_report_prompt(
                        literature,
                        previous_context,
                        constraints,
                        baseline_code,
                        evolution_rules,
                    ),
                    paths['logs'],
                    '02_research_report_attempt_1',
                )
                write_text(research_attempt_path, current_research)
            else:
                previous_review = read_text(
                    paths['shared'] / (
                        '01_plan_review_attempt_%d.md' % (attempt - 1)
                    )
                )
                previous_research = read_text(
                    paths['shared'] / (
                        '01_research_report_attempt_%d.md' % (attempt - 1)
                    )
                )
                current_research = self._call_model(
                    'researcher',
                    self._role_system(state, 'researcher', schedule),
                    research_revision_prompt(
                        previous_research,
                        previous_review,
                        literature,
                        previous_context,
                        constraints,
                        template,
                        baseline_code,
                        evolution_rules,
                    ),
                    paths['logs'],
                    '02_research_revision_%d' % (attempt - 1),
                )
                write_text(research_attempt_path, current_research)

            review_path = paths['shared'] / (
                '01_plan_review_attempt_%d.md' % attempt
            )
            if review_path.exists():
                review = read_text(review_path)
                decision = parse_plan_review_decision(review)
                if decision is None:
                    review, decision = self._finalize_plan_review_protocol(
                        state, schedule, paths, review, attempt
                    )
                    write_text(review_path, review)
            else:
                review = self._call_model(
                    'auditor',
                    self._role_system(state, 'auditor', schedule),
                    plan_review_prompt(
                        current_research,
                        template,
                        constraints,
                        baseline_code,
                        evolution_rules,
                    ),
                    paths['logs'],
                    '02_plan_review_attempt_%d' % attempt,
                )
                write_text(review_path, review)
                review, decision = self._finalize_plan_review_protocol(
                    state, schedule, paths, review, attempt
                )
                write_text(review_path, review)

            if decision == 'PASS':
                write_text(
                    paths['shared'] / '01_research_report.md', current_research
                )
                write_text(
                    paths['shared'] / '01_plan_review_final.md', review
                )
                return current_research, attempt, returns_used

            if attempt < max_attempts:
                returns_used += 1
                continue

            handoff_path = paths['shared'] / '01_plan_review_handoff.md'
            if handoff_path.exists():
                handoff_review = read_text(handoff_path)
            else:
                handoff_review = self._call_model(
                    'auditor',
                    self._role_system(state, 'auditor', schedule),
                    plan_review_handoff_prompt(
                        current_research,
                        review,
                        template,
                        constraints,
                        baseline_code,
                        evolution_rules,
                    ),
                    paths['logs'],
                    '02_plan_review_attempt_%d_handoff' % attempt,
                )
                write_text(handoff_path, handoff_review)
            handoff_review, handoff_decision = (
                self._finalize_plan_review_protocol(
                    state, schedule, paths, handoff_review, attempt
                )
            )
            write_text(handoff_path, handoff_review)
            if handoff_decision == 'PASS':
                write_text(
                    paths['shared'] / '01_research_report.md', current_research
                )
                write_text(
                    paths['shared'] / '01_plan_review_final.md', handoff_review
                )
                return current_research, attempt, returns_used

            write_text(
                paths['shared'] / '01_plan_review_final.md', handoff_review
            )
            write_text(
                paths['shared'] / '01_research_report.md', current_research
            )
            self._set_round_status(
                state,
                schedule.number,
                'failed_plan_review',
                plan_review_attempts=attempt,
                plan_review_returns=returns_used,
                last_error=(
                    'Research plan did not pass local-backtest feasibility review after '
                    '%d attempt(s) and %d return(s)' % (attempt, returns_used)
                ),
            )
            raise PlanReviewFailure(
                'Round %d research plan did not pass local-backtest feasibility review '
                'after %d attempt(s)' % (schedule.number, attempt),
                current_research,
                attempt,
                returns_used,
            )

        raise ExperimentError('Unreachable plan-review state')

    def prepare_round(self, round_number: int) -> None:
        state = self.load_state()
        self._validate_previous_round(state, round_number)
        existing = self._round_state(state, round_number)
        if existing.get('status') in ('awaiting_evaluation', 'complete'):
            return
        if existing.get('status') == 'backtest_complete':
            schedule = self.schedule(round_number)
            paths = initialize_round_directories(self.group_dir, round_number)
            self._complete_saved_post_round(state, schedule, paths)
            return
        retrying = existing.get('status') == 'prepare_failed'
        if existing.get('status') in (
            'preparing',
        ):
            raise ExperimentError(
                'Round %d already has status %s; use status or reset only after '
                'manually preserving the existing run' % (
                    round_number, existing.get('status')
                )
            )

        schedule = self.schedule(round_number)
        paths = initialize_round_directories(self.group_dir, round_number)
        self._set_round_status(
            state,
            round_number,
            'preparing',
            decision_as_of=schedule.decision_as_of,
            evaluation_start=schedule.evaluation_start,
            evaluation_end=schedule.evaluation_end,
            last_error=None,
        )

        previous_context = self._previous_shared_context(round_number)
        constraints = self._experiment_constraints(schedule)
        baseline_code = self._baseline_strategy_for_round(round_number, paths)
        evolution_rules = self._strategy_evolution_rules()
        template = read_text(
            self.admin_dir / '本地回测策略模板' / 'strategy_template.py'
        )
        if not template:
            raise ExperimentError('Local backtest strategy template is missing')

        try:
            research, plan_review_attempts, plan_review_returns = (
                self._research_plan_for_round(
                    state,
                    schedule,
                    paths,
                    previous_context,
                    constraints,
                    template,
                    baseline_code,
                    evolution_rules,
                )
            )
        except PlanReviewFailure as exc:
            self._record_plan_delivery_failure(
                state,
                schedule,
                paths,
                exc.research,
                exc.attempts,
                exc.returns_used,
            )
            return

        start_attempt = 1
        resume_code_path: Optional[Path] = None
        resume_notes_path: Optional[Path] = None
        if retrying:
            candidates = sorted(
                paths['shared'].glob('02_strategy_attempt_*.py'),
                key=lambda path: int(path.stem.rsplit('_', 1)[-1]),
                reverse=True,
            )
            for candidate in candidates:
                candidate_attempt = int(candidate.stem.rsplit('_', 1)[-1])
                audit_path = paths['shared'] / (
                    '04_audit_attempt_%d.md' % candidate_attempt
                )
                notes_path = paths['shared'] / (
                    '03_engineer_notes_attempt_%d.md' % candidate_attempt
                )
                if not audit_path.exists() and notes_path.exists():
                    start_attempt = candidate_attempt
                    resume_code_path = candidate
                    resume_notes_path = notes_path
                    break

        if resume_code_path is not None and resume_notes_path is not None:
            code = read_text(resume_code_path)
            notes = read_text(resume_notes_path)
            write_text(
                paths['logs'] / '00_resume_notice.md',
                'Resuming at audit attempt %d without repeating research or engineering.'
                % start_attempt,
            )
        else:
            write_text(
                paths['logs'] / '00_resume_notice.md',
                'Using the feasibility-approved research plan for engineering.'
                if not retrying
                else 'Reusing the feasibility-approved research plan after an '
                'invalid or empty initial engineer response.',
            )
            engineer_response = self._call_model(
                'engineer',
                self._role_system(state, 'engineer', schedule),
                engineer_prompt(
                    research,
                    template,
                    previous_context,
                    constraints,
                    baseline_code,
                    evolution_rules,
                ),
                paths['logs'],
                '03_engineer_initial_retry' if retrying else '03_engineer_initial',
            )
            code, notes = parse_engineer_output(engineer_response)

        max_attempts = int(self.experiment_config.get('max_code_submissions', 3))
        max_returns = max_attempts - 1
        passed = False
        passed_development_summary: Optional[Dict[str, Any]] = None
        passed_development_output_dir: Optional[Path] = None
        development_rejected = False
        development_result_paths: List[Path] = []
        final_audit = ''
        attempts = 0
        returns_used = max(0, start_attempt - 1)
        baseline_path = paths['shared'] / '04_audit_baseline.md'
        for attempt in range(start_attempt, max_attempts + 1):
            attempts = attempt
            code_path = paths['shared'] / ('02_strategy_attempt_%d.py' % attempt)
            write_text(code_path, code)
            strategy_diff = '\n'.join(difflib.unified_diff(
                baseline_code.splitlines(),
                code.splitlines(),
                fromfile='00_baseline_strategy.py',
                tofile='02_strategy_attempt_%d.py' % attempt,
                lineterm='',
            ))
            write_text(
                paths['shared'] / ('02_strategy_diff_attempt_%d.patch' % attempt),
                strategy_diff or '# No code difference from baseline',
            )
            write_text(
                paths['shared'] / ('03_engineer_notes_attempt_%d.md' % attempt),
                notes,
            )
            syntax_ok, syntax_detail = python_syntax_check(code_path)
            static_issues = local_strategy_static_check(code)
            static_report = local_static_report(static_issues)
            if attempt == 1:
                audit_user_prompt = audit_prompt(
                    research,
                    code,
                    notes,
                    template,
                    static_report,
                    baseline_code,
                    evolution_rules,
                )
            else:
                if baseline_path.exists():
                    baseline_audit = read_text(baseline_path)
                else:
                    baseline_audit = read_text(
                        paths['shared'] / '04_audit_attempt_1.md'
                    )
                    write_text(baseline_path, baseline_audit)
                previous_audit = read_text(
                    paths['shared'] / ('04_audit_attempt_%d.md' % (attempt - 1))
                )
                previous_code = read_text(
                    paths['shared'] / ('02_strategy_attempt_%d.py' % (attempt - 1))
                )
                audit_user_prompt = audit_followup_prompt(
                    research,
                    previous_code,
                    code,
                    notes,
                    baseline_audit,
                    previous_audit,
                    template,
                    static_report,
                    attempt,
                    baseline_code,
                    evolution_rules,
                )
            audit = self._call_model(
                'auditor',
                self._role_system(state, 'auditor', schedule),
                audit_user_prompt,
                paths['logs'],
                '04_audit_attempt_%d' % attempt,
            )
            write_text(
                paths['shared'] / ('04_audit_attempt_%d_raw.md' % attempt),
                audit,
            )
            audit, decision = self._finalize_audit_protocol(
                state, schedule, paths, audit, attempt
            )
            audit = audit.rstrip() + '\n\n' + static_report
            if static_issues:
                decision = 'REVISE'
            if not syntax_ok:
                audit += '\n\nLOCAL_SYNTAX_CHECK: FAILED\n' + syntax_detail
                decision = 'REVISE'
            else:
                audit += '\n\nLOCAL_SYNTAX_CHECK: PASSED'
            write_text(
                paths['shared'] / ('04_audit_attempt_%d.md' % attempt), audit
            )
            if attempt == 1:
                write_text(baseline_path, audit)
            else:
                baseline_ids = set(
                    parse_open_audit_issue_ids(read_text(baseline_path))
                )
                open_ids = set(parse_open_audit_issue_ids(audit))
                valid_ids = {
                    issue_id for issue_id in open_ids
                    if issue_id in baseline_ids
                    or issue_id.startswith('R%d-' % attempt)
                    or issue_id.startswith('STATIC-')
                    or issue_id.startswith('LOCAL-')
                }
                ignored_ids = sorted(open_ids - valid_ids)
                if ignored_ids:
                    audit += (
                        '\n\nPROTOCOL_NORMALIZATION: newly invented pre-existing '
                        'issue IDs were recorded as non-blocking: '
                        + ', '.join(ignored_ids)
                    )
                    write_text(
                        paths['shared'] / ('04_audit_attempt_%d.md' % attempt),
                        audit,
                    )
                if decision in ('REVISE', 'BLOCK') and not valid_ids:
                    audit += (
                        '\n\nPROTOCOL_NORMALIZATION: no open issue belonged to '
                        'the frozen baseline, a demonstrated regression, or a '
                        'deterministic check; decision normalized to PASS.'
                    )
                    decision = 'PASS'
                    write_text(
                        paths['shared'] / ('04_audit_attempt_%d.md' % attempt),
                        audit,
                    )
            if static_issues or not syntax_ok:
                decision = 'REVISE'
            final_audit = audit
            if decision == 'PASS':
                ok, runtime_detail, runtime_summary, runtime_dir = (
                    self._run_development_candidate(
                        paths, attempt, code_path
                    )
                )
                if ok and runtime_summary is not None:
                    development_text = self._development_result_text(
                        paths, attempt, runtime_dir, runtime_summary
                    )
                    development_result_paths.append(
                        paths['shared']
                        / ('04_development_result_attempt_%d.md' % attempt)
                    )
                    audit += (
                        '\n\nDEVELOPMENT_BACKTEST_CHECK: PASSED'
                        '\nDEVELOPMENT_PERIOD: 2022-01-01 to 2024-12-31'
                    )
                    write_text(
                        paths['shared'] / ('04_audit_attempt_%d.md' % attempt),
                        audit,
                    )
                    final_audit = audit
                    previous_development = collect_text_files(
                        development_result_paths[:-1],
                        max_chars=int(
                            (
                                (
                                    self.experiment_config.get('local_backtest')
                                    or {}
                                ).get('development') or {}
                            ).get('max_feedback_chars', 60000)
                        ),
                    )
                    development_review = self._call_model(
                        'researcher',
                        self._role_system(state, 'researcher', schedule),
                        development_review_prompt(
                            research,
                            code,
                            notes,
                            development_text,
                            previous_development,
                            attempt,
                            max_attempts,
                        ),
                        paths['logs'],
                        '04_development_review_attempt_%d' % attempt,
                    )
                    development_decision = self._parse_development_decision(
                        development_review
                    )
                    if development_decision is None:
                        development_review = (
                            development_review.rstrip()
                            + '\n\nPROTOCOL_NORMALIZATION: no valid decision was '
                            'found; the candidate was rejected and the unchanged '
                            'round baseline will be used.'
                        )
                        development_decision = 'REJECT'
                    if (
                        attempt >= max_attempts
                        and development_decision == 'REVISE'
                    ):
                        development_review = (
                            development_review.rstrip()
                            + '\n\nPROTOCOL_NORMALIZATION: no submission remains for '
                            'the requested revision; the candidate was rejected and '
                            'the unchanged round baseline will be used.'
                        )
                        development_decision = 'REJECT'
                    write_text(
                        paths['shared']
                        / ('04_development_review_attempt_%d.md' % attempt),
                        development_review,
                    )
                    if development_decision == 'FREEZE':
                        passed = True
                        passed_development_summary = runtime_summary
                        passed_development_output_dir = runtime_dir
                        break
                    if development_decision == 'REJECT':
                        development_rejected = True
                        break

                    returns_used += 1
                    engineer_response = self._call_model(
                        'engineer',
                        self._role_system(state, 'engineer', schedule),
                        development_engineer_revision_prompt(
                            research,
                            code,
                            notes,
                            development_text,
                            development_review,
                            template,
                            baseline_code,
                            evolution_rules,
                        ),
                        paths['logs'],
                        '03_engineer_development_revision_%d' % attempt,
                    )
                    code, notes = parse_engineer_output(engineer_response)
                    continue
                audit += (
                    '\n\nDEVELOPMENT_BACKTEST_CHECK: FAILED\n' + runtime_detail
                )
                decision = 'REVISE'
                final_audit = audit
                write_text(
                    paths['shared'] / ('04_audit_attempt_%d.md' % attempt), audit
                )
                if attempt == 1:
                    write_text(baseline_path, audit)
            if decision == 'BLOCK' or attempt >= max_attempts:
                break
            returns_used += 1
            engineer_response = self._call_model(
                'engineer',
                self._role_system(state, 'engineer', schedule),
                engineer_revision_prompt(
                    research,
                    code,
                    audit,
                    template,
                    baseline_code,
                    evolution_rules,
                ),
                paths['logs'],
                '03_engineer_revision_%d' % attempt,
            )
            code, notes = parse_engineer_output(engineer_response)

        write_text(paths['shared'] / '03_engineer_notes.md', notes)
        write_text(paths['shared'] / '04_audit_final.md', final_audit)
        if not passed:
            self._record_code_delivery_failure(
                state,
                schedule,
                paths,
                plan_review_attempts,
                plan_review_returns,
                attempts,
                returns_used,
                delivery_status=(
                    'development_rejected'
                    if development_rejected
                    else 'code_audit_failed'
                ),
                failure_description=(
                    'The researcher rejected the runnable candidate after reviewing '
                    'its development-period evidence against the predeclared '
                    'acceptance and stopping conditions.'
                    if development_rejected
                    else None
                ),
                development_attempts=len(development_result_paths),
            )
            return

        frozen_path = paths['shared'] / '05_strategy_frozen.py'
        write_text(frozen_path, code)
        digest = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
        write_json(paths['shared'] / '05_freeze_manifest.json', {
            'round': round_number,
            'decision_as_of': schedule.decision_as_of,
            'evaluation_start': schedule.evaluation_start,
            'evaluation_end': schedule.evaluation_end,
            'sha256': digest,
            'baseline_sha256': (
                read_json(
                    paths['shared'] / '00_baseline_manifest.json', default={}
                ) or {}
            ).get('sha256'),
            'plan_review_attempts': plan_review_attempts,
            'plan_review_returns': plan_review_returns,
            'audit_attempts': attempts,
            'audit_returns': returns_used,
            'development_start': '2022-01-01',
            'development_end': '2024-12-31',
            'selected_development_candidate_sha256': (
                passed_development_summary or {}
            ).get('candidate_sha256'),
            'development_cache_sha256': (
                passed_development_summary or {}
            ).get('cache_sha256'),
            'development_fundamental_cache_sha256': (
                passed_development_summary or {}
            ).get('fundamental_cache_sha256'),
            'development_engine_sha256': (
                passed_development_summary or {}
            ).get('engine_sha256'),
            'evaluation_visibility_rule': (
                'evaluation runs once after freeze; no metric is returned to '
                'same-round research, engineering, or audit revision'
            ),
        })
        if self.group_kind == 'hr':
            if not schedule.final_round:
                self._precommit_random_draws(state, round_number, paths['admin'])
            self._run_innovation_audit(
                state, schedule, paths, code, baseline_code
            )

        if (
            passed_development_summary is None
            or passed_development_output_dir is None
        ):
            raise ExperimentError(
                'Passed strategy has no development-backtest artifacts'
            )

        evaluation_ok, evaluation_detail, evaluation_summary, evaluation_dir = (
            self._run_local_candidate(
                schedule, paths, attempts, frozen_path
            )
        )
        if not evaluation_ok or evaluation_summary is None:
            rejected_path = paths['shared'] / '05_strategy_rejected_evaluation.py'
            copy_file(frozen_path, rejected_path)
            final_audit += (
                '\n\nFROZEN_EVALUATION_EXECUTION_CHECK: FAILED\n'
                + evaluation_detail
            )
            write_text(paths['shared'] / '04_audit_final.md', final_audit)
            self._record_code_delivery_failure(
                state,
                schedule,
                paths,
                plan_review_attempts,
                plan_review_returns,
                attempts,
                returns_used,
                delivery_status='evaluation_runtime_failed',
                failure_description=(
                    'The candidate passed development checks but failed during '
                    'the single frozen evaluation execution.'
                ),
            )
            return
        self._set_round_status(
            state,
            round_number,
            'backtest_complete',
            frozen_sha256=digest,
            execution_backend='local_backtest',
            plan_review_attempts=plan_review_attempts,
            plan_review_returns=plan_review_returns,
            audit_attempts=attempts,
            audit_returns=returns_used,
            development_attempts=len(development_result_paths),
            development_start='2022-01-01',
            development_end='2024-12-31',
        )
        self._finalize_local_result(
            state, schedule, paths, evaluation_dir, evaluation_summary, digest
        )

    def _record_code_delivery_failure(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        plan_review_attempts: int,
        plan_review_returns: int,
        audit_attempts: int,
        audit_returns: int,
        delivery_status: str = 'code_audit_failed',
        failure_description: Optional[str] = None,
        development_attempts: int = 0,
    ) -> None:
        baseline_path = paths['shared'] / '00_baseline_strategy.py'
        baseline_manifest = read_json(
            paths['shared'] / '00_baseline_manifest.json', default={}
        ) or {}
        if not baseline_path.exists():
            raise ExperimentError(
                'Cannot recover from audit failure: round baseline is missing'
            )

        deployment_mode = 'reuse_baseline'
        source_round = baseline_manifest.get('source_round')
        frozen_path = paths['shared'] / '05_strategy_frozen.py'
        copy_file(baseline_path, frozen_path)
        frozen_sha256 = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
        write_json(paths['shared'] / '05_freeze_manifest.json', {
            'round': schedule.number,
            'deployment_mode': deployment_mode,
            'source_round': source_round,
            'baseline_source_kind': baseline_manifest.get('source_kind'),
            'sha256': frozen_sha256,
            'code_audit_passed_this_round': False,
        })
        failure_description = failure_description or (
            'The proposed improvement exhausted the code-audit or development '
            'submission limit.'
        )
        instruction = (
            '# Reuse the frozen round baseline\n\n'
            + failure_description + ' '
            'Deploy the unchanged baseline strategy for this round and keep '
            'account capital and positions continuous. Attribute the local backtest '
            'result to the baseline, not to the rejected improvement.\n'
        )

        write_text(paths['shared'] / '05_deployment_instruction.md', instruction)
        write_json(paths['shared'] / '05_deployment_decision.json', {
            'round': schedule.number,
            'delivery_status': delivery_status,
            'deployment_mode': deployment_mode,
            'source_round': source_round,
            'strategy_annualized_return_rule': (
                'frozen_local_backtest_result'
            ),
        })

        if self.group_kind == 'hr':
            if not schedule.final_round:
                self._precommit_random_draws(
                    state, schedule.number, paths['admin']
                )
            write_text(
                paths['admin'] / 'innovation_audit.md',
                'No new candidate was deployed. ' + failure_description,
            )

        fallback_attempt = int(
            self.experiment_config.get('max_code_submissions', 3)
        ) + 1
        ok, detail, summary, output_dir = self._run_local_candidate(
            schedule, paths, fallback_attempt, frozen_path
        )
        if not ok or summary is None:
            raise ExperimentError(
                'The frozen baseline also failed the local backtest:\n%s' % detail
            )
        self._set_round_status(
            state,
            schedule.number,
            'backtest_complete',
            delivery_status=delivery_status,
            deployment_mode=deployment_mode,
            source_round=source_round,
            frozen_sha256=frozen_sha256,
            plan_review_attempts=plan_review_attempts,
            plan_review_returns=plan_review_returns,
            audit_attempts=audit_attempts,
            audit_returns=audit_returns,
            development_attempts=development_attempts,
            development_start=(
                '2022-01-01' if development_attempts else None
            ),
            development_end=(
                '2024-12-31' if development_attempts else None
            ),
            last_error=None,
        )
        self._finalize_local_result(
            state, schedule, paths, output_dir, summary, frozen_sha256
        )

    def _record_plan_delivery_failure(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        research: str,
        plan_review_attempts: int,
        plan_review_returns: int,
    ) -> None:
        """Record a failed research delivery as performance, not a crashed run."""
        baseline_path = paths['shared'] / '00_baseline_strategy.py'
        baseline_manifest = read_json(
            paths['shared'] / '00_baseline_manifest.json', default={}
        ) or {}
        if not baseline_path.exists():
            raise ExperimentError(
                'Cannot recover from plan-review failure: round baseline is missing'
            )

        write_text(paths['shared'] / '01_research_report.md', research)
        final_review = read_text(paths['shared'] / '01_plan_review_final.md')
        write_text(
            paths['shared'] / '03_engineer_notes.md',
            '# Engineering not started\n\n'
            'The research plan exhausted the feasibility-review return limit, so '
            'no candidate implementation was requested.\n',
        )
        write_text(
            paths['shared'] / '04_audit_final.md',
            '# Delivery result\n\n'
            'DELIVERY_STATUS: PLAN_REVIEW_FAILED\n\n'
            'No candidate reached engineering or code audit. The unchanged round '
            'baseline was frozen and evaluated.\n\n'
            '## Final plan review\n\n' + (final_review or 'Unavailable'),
        )

        deployment_mode = 'reuse_baseline'
        source_round = baseline_manifest.get('source_round')
        frozen_path = paths['shared'] / '05_strategy_frozen.py'
        copy_file(baseline_path, frozen_path)
        frozen_sha256 = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
        write_json(paths['shared'] / '05_freeze_manifest.json', {
            'round': schedule.number,
            'deployment_mode': deployment_mode,
            'source_round': source_round,
            'baseline_source_kind': baseline_manifest.get('source_kind'),
            'sha256': frozen_sha256,
            'plan_review_passed_this_round': False,
            'code_audit_passed_this_round': False,
        })
        write_text(
            paths['shared'] / '05_deployment_instruction.md',
            '# Reuse the frozen round baseline\n\n'
            'The research plan exhausted the feasibility-review return limit. '
            'Deploy the unchanged round baseline and attribute the local backtest '
            'result to that baseline, not to the rejected research proposal.\n',
        )
        write_json(paths['shared'] / '05_deployment_decision.json', {
            'round': schedule.number,
            'delivery_status': 'plan_review_failed',
            'deployment_mode': deployment_mode,
            'source_round': source_round,
            'strategy_annualized_return_rule': 'frozen_local_backtest_result',
        })

        if self.group_kind == 'hr':
            if not schedule.final_round:
                self._precommit_random_draws(
                    state, schedule.number, paths['admin']
                )
            write_text(
                paths['admin'] / 'innovation_audit.md',
                'No new candidate was frozen because the research plan did not '
                'pass feasibility review.',
            )

        fallback_attempt = int(
            self.experiment_config.get('max_code_submissions', 3)
        ) + 1
        ok, detail, summary, output_dir = self._run_local_candidate(
            schedule, paths, fallback_attempt, frozen_path
        )
        if not ok or summary is None:
            raise ExperimentError(
                'The frozen baseline also failed the local backtest:\n%s' % detail
            )
        self._set_round_status(
            state,
            schedule.number,
            'backtest_complete',
            delivery_status='plan_review_failed',
            deployment_mode=deployment_mode,
            source_round=source_round,
            frozen_sha256=frozen_sha256,
            plan_review_attempts=plan_review_attempts,
            plan_review_returns=plan_review_returns,
            audit_attempts=0,
            audit_returns=0,
            last_error=None,
        )
        self._finalize_local_result(
            state, schedule, paths, output_dir, summary, frozen_sha256
        )

    def _run_innovation_audit(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        candidate_code: str,
        baseline_code: str,
    ) -> None:
        reference_dir = self.admin_dir / 'QMT参考策略原始'
        reference_files = sorted(reference_dir.glob('*.py'))
        if not reference_files:
            write_text(
                paths['admin'] / 'innovation_audit.md',
                'No original reference strategies were available.'
            )
            return
        references = collect_text_files(
            reference_files,
            max_chars=int(
                self.experiment_config.get('max_reference_context_chars', 100000)
            ),
        )
        report = self._call_model(
            'auditor',
            self._role_system(state, 'auditor', schedule),
            innovation_audit_prompt(
                candidate_code, baseline_code, references
            ),
            paths['logs'],
            '05_confidential_innovation_audit',
        )
        write_text(paths['admin'] / 'innovation_audit.md', report)

    def _role_evidence(self, paths: Dict[str, Path], role: str) -> str:
        shared = paths['shared']
        mapping = {
            'researcher': (
                [shared / '01_research_report.md']
                + sorted(shared.glob('04_development_review_attempt_*.md'))
            ),
            'engineer': [
                shared / '03_engineer_notes.md', shared / '04_audit_final.md'
            ],
            'auditor': [
                shared / '04_audit_final.md',
                paths['admin'] / 'innovation_audit.md',
            ],
            'analyst': [shared / '07_data_analysis.md'],
        }
        return collect_text_files(mapping[role], max_chars=40000)

    def submit_qmt_result(self, round_number: int, result_path: Path) -> None:
        state = self.load_state()
        item = self._round_state(state, round_number)
        if item.get('status') != 'awaiting_qmt':
            raise ExperimentError(
                'Round %d status is %s, not awaiting_qmt' % (
                    round_number, item.get('status')
                )
            )
        schedule = self.schedule(round_number)
        data = load_qmt_result(result_path)
        if round_number >= 2:
            required_details = ('trade_records_file',)
            missing_details = [
                field for field in required_details
                if data.get(field) is None or not str(data.get(field)).strip()
            ]
            if missing_details:
                raise ExperimentError(
                    'Round %d requires QMT detail CSV fields: %s' % (
                        round_number, ', '.join(missing_details),
                    )
                )
        if str(data['period_start']) != schedule.evaluation_start:
            raise ExperimentError('QMT period_start does not match configured schedule')
        if str(data['period_end']) != schedule.evaluation_end:
            raise ExperimentError('QMT period_end does not match configured schedule')
        self._validate_qmt_submission(state, round_number, data)
        try:
            qmt_details = load_qmt_detail_files(result_path, data)
            if round_number >= 2:
                validate_qmt_detail_consistency(qmt_details, data)
            detail_summary = qmt_detail_summary(qmt_details, data)
        except ValueError as exc:
            raise ExperimentError(str(exc)) from exc
        if item.get('deployment_mode') == 'cash_only':
            if abs(float(data['strategy_annualized_return'])) > 1e-12:
                raise ExperimentError(
                    'cash_only round requires strategy_annualized_return = 0'
                )
            if (
                data.get('strategy_total_return') is not None
                and abs(float(data['strategy_total_return'])) > 1e-12
            ):
                raise ExperimentError(
                    'cash_only round requires strategy_total_return = 0'
                )
            if (
                data.get('trade_count') is not None
                and int(data['trade_count']) != 0
            ):
                raise ExperimentError('cash_only round requires trade_count = 0')
            nonzero_positions = [
                position for position in (data.get('ending_positions') or [])
                if float(position.get('volume') or 0) != 0
                or float(position.get('market_value') or 0) != 0
            ]
            if nonzero_positions:
                raise ExperimentError(
                    'cash_only round cannot contain nonzero ending positions'
                )

        paths = initialize_round_directories(self.group_dir, round_number)
        copy_file(result_path, paths['input'] / 'qmt_result_original.json')
        if qmt_details:
            for detail in qmt_details.values():
                destination = paths['input'] / detail['archive_name']
                if detail['path'].resolve() != destination.resolve():
                    copy_file(detail['path'], destination)
            write_json(
                paths['shared'] / '06_qmt_detail_manifest.json',
                qmt_detail_manifest(qmt_details),
            )
            write_text(
                paths['shared'] / '06_qmt_detail_summary.md',
                detail_summary,
            )
        write_json(paths['shared'] / '06_qmt_result.json', data)
        qmt_markdown = qmt_result_to_markdown(data)
        if detail_summary:
            qmt_markdown += '\n\n' + detail_summary
        if item.get('delivery_status') == 'code_audit_failed':
            qmt_markdown += (
                '\n\n## Delivery status\n\n'
                '- delivery_status: code_audit_failed\n'
                '- deployment_mode: %s\n'
                '- source_round: %s\n' % (
                    item.get('deployment_mode'),
                    item.get('source_round'),
                )
            )
        write_text(paths['shared'] / '06_qmt_result.md', qmt_markdown)

        analysis = self._generate_analysis(round_number, state, paths)
        write_text(paths['shared'] / '07_data_analysis.md', analysis)

        if self.group_kind == 'hr':
            self._complete_hr_post_round(
                state, schedule, paths, qmt_markdown
            )
        elif self.group_kind == 'reflection':
            self._complete_reflection_post_round(
                state, schedule, paths, qmt_markdown
            )
            self._set_round_status(state, round_number, 'complete')
        else:
            self._set_round_status(state, round_number, 'complete')

    def _complete_hr_post_round(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        qmt_markdown: str,
    ) -> None:
        reports: Dict[str, str] = {}
        for role in ROLES:
            evidence = self._role_evidence(paths, role)
            report = self._call_model(
                role,
                self._role_system(state, role, schedule),
                self_report_prompt(role, evidence, qmt_markdown),
                paths['logs'],
                '08_self_report_' + role,
            )
            role_dir = paths['private'] / role
            write_text(role_dir / 'self_report.md', report)
            reports[role] = report

        evaluation = {
            'round': schedule.number,
            'final_round': schedule.final_round,
            'instructions': (
                'Fill manager_score from 0 to 100 and evidence-based comments. '
                'Do not fill random_score or final_score; the program does that.'
            ),
            'evaluations': {},
        }
        levels = state.get('levels') or {}
        for role in ROLES:
            evaluation['evaluations'][role] = {
                'role_name': ROLE_NAMES[role],
                'current_level': levels.get(role, '初级'),
                'manager_score': None,
                'basic_work_comment': '',
                'performance_work_comment': '',
                'evidence': [],
                'not_promoted_reason_code': '',
                'next_round_requirements': [],
                'self_report_file': str(
                    paths['private'] / role / 'self_report.md'
                ),
            }
        write_json(paths['admin'] / 'manager_evaluation_template.json', evaluation)
        write_text(
            paths['admin'] / 'manager_packet.md',
            self._manager_packet(state, schedule, paths, reports, qmt_markdown),
        )

        if schedule.final_round:
            self._set_round_status(state, schedule.number, 'complete')
        else:
            self._set_round_status(
                state, schedule.number, 'awaiting_evaluation'
            )

    def _manager_packet(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        reports: Dict[str, str],
        qmt_markdown: str,
    ) -> str:
        lines = [
            '# 第%d轮管理员评价包' % schedule.number,
            '',
            '本文件只供管理员使用，不得提供给四个职位。',
            '',
            qmt_markdown,
            '',
        ]
        for role in ROLES:
            lines.extend([
                '## %s' % ROLE_NAMES[role],
                '',
                '- 私有职级：%s' % state.get('levels', {}).get(role, '初级'),
                '',
                '### 本职级要求',
                '',
                HR_LEVEL_REQUIREMENTS[role][
                    state.get('levels', {}).get(role, '初级')
                ],
                '',
                '### 工作证据',
                '',
                self._role_evidence(paths, role),
                '',
                '### 私人述职',
                '',
                reports[role],
                '',
            ])
        return '\n'.join(lines)

    def _complete_reflection_post_round(
        self,
        state: Dict[str, Any],
        schedule: RoundSchedule,
        paths: Dict[str, Path],
        qmt_markdown: str,
    ) -> None:
        for role in ROLES:
            evidence = self._role_evidence(paths, role)
            reflection = self._call_model(
                role,
                self._role_system(state, role, schedule),
                reflection_prompt(role, evidence, qmt_markdown),
                paths['logs'],
                '08_reflection_' + role,
            )
            write_text(
                paths['private'] / role / 'reflection.md', reflection
            )

    def _draw_seed(self, round_number: int, role: str) -> int:
        seed_material = '%s|%s|%s|%s' % (
            self.experiment_config.get('random_seed'),
            self.group_kind,
            round_number,
            role,
        )
        digest = hashlib.sha256(seed_material.encode('utf-8')).hexdigest()
        return int(digest[:16], 16)

    def _precommit_random_draws(
        self, state: Dict[str, Any], round_number: int, admin_dir: Path
    ) -> None:
        draws = {}
        for role in ROLES:
            rng = random.Random(self._draw_seed(round_number, role))
            draws[role] = round(rng.uniform(0.0, 100.0), 6)
        payload = {
            'round': round_number,
            'draws': draws,
            'note': 'Generated before manager evaluation. Do not reveal to evaluator.',
        }
        write_json(admin_dir / 'random_draws_private.json', payload)
        commitment = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
        ).hexdigest()
        write_json(admin_dir / 'random_draw_commitment.json', {
            'round': round_number,
            'sha256': commitment,
        })

    def apply_evaluation(
        self,
        round_number: int,
        evaluation_path: Path,
        promotion_slots: int,
    ) -> None:
        state = self.load_state()
        schedule = self.schedule(round_number)
        if schedule.final_round:
            raise ExperimentError('Final round does not perform promotions')
        item = self._round_state(state, round_number)
        if item.get('status') != 'awaiting_evaluation':
            raise ExperimentError(
                'Round %d status is %s, not awaiting_evaluation' % (
                    round_number, item.get('status')
                )
            )
        if promotion_slots < 0 or promotion_slots > 3:
            raise ExperimentError('promotion_slots must be between 0 and 3')

        paths = initialize_round_directories(self.group_dir, round_number)
        evaluation = read_json(evaluation_path)
        evaluations = (evaluation or {}).get('evaluations') or {}
        missing = [role for role in ROLES if role not in evaluations]
        if missing:
            raise ExperimentError(
                'Evaluation is missing roles: %s' % ', '.join(missing)
            )
        draws = read_json(paths['admin'] / 'random_draws_private.json')
        if not draws:
            raise ExperimentError('Precommitted random draws are missing')

        current_levels = dict(state.get('levels') or {})
        scored: List[Dict[str, Any]] = []
        for role in ROLES:
            entry = evaluations[role]
            try:
                manager_score = float(entry['manager_score'])
            except (KeyError, TypeError, ValueError):
                raise ExperimentError('Invalid manager_score for %s' % role)
            if manager_score < 0 or manager_score > 100:
                raise ExperimentError('manager_score must be 0 to 100')
            random_score = float(draws['draws'][role])
            final_score = manager_score * 0.70 + random_score * 0.30
            scored.append({
                'role': role,
                'manager_score': manager_score,
                'random_score': random_score,
                'final_score': round(final_score, 6),
                'eligible': current_levels.get(role, '初级') != '高级',
            })

        eligible = [item for item in scored if item['eligible']]
        eligible.sort(
            key=lambda item: (item['final_score'], item['random_score']),
            reverse=True,
        )
        promoted_roles = {
            item['role'] for item in eligible[:min(promotion_slots, len(eligible))]
        }

        score_by_role = {item['role']: item for item in scored}
        results = {
            'round': round_number,
            'promotion_slots': promotion_slots,
            'promoted_roles': sorted(promoted_roles),
            'scores': scored,
            'feedback_files': {},
        }
        for role in ROLES:
            before = current_levels.get(role, '初级')
            after = before
            promoted = role in promoted_roles
            if promoted:
                after = LEVELS[min(LEVELS.index(before) + 1, len(LEVELS) - 1)]
                current_levels[role] = after

            entry = evaluations[role]
            if promoted:
                reason_code = 'PROMOTED'
            elif before == '高级':
                reason_code = 'ALREADY_TOP_LEVEL'
            elif promotion_slots == 0:
                reason_code = entry.get('not_promoted_reason_code') or 'QUOTA_LIMIT'
            else:
                reason_code = entry.get('not_promoted_reason_code') or (
                    'PERFORMANCE_GAP'
                    if score_by_role[role]['manager_score'] < 60
                    else 'QUOTA_LIMIT'
                )

            feedback = {
                'round': round_number,
                'role': role,
                'role_name': ROLE_NAMES[role],
                'previous_level': before,
                'current_level': after,
                'manager_score': score_by_role[role]['manager_score'],
                'basic_work_comment': entry.get('basic_work_comment', ''),
                'performance_work_comment': entry.get(
                    'performance_work_comment', ''
                ),
                'evidence': entry.get('evidence') or [],
                'promotion': promoted,
                'reason_code': reason_code,
                'next_round_requirements': entry.get(
                    'next_round_requirements'
                ) or [],
            }
            feedback_path = paths['private'] / role / 'feedback.json'
            write_json(feedback_path, feedback)
            results['feedback_files'][role] = str(feedback_path)

        state['levels'] = current_levels
        filled_path = paths['admin'] / 'manager_evaluation_filled.json'
        if evaluation_path.resolve() != filled_path.resolve():
            copy_file(evaluation_path, filled_path)
        write_json(paths['admin'] / 'promotion_results_private.json', results)
        self._set_round_status(state, round_number, 'complete')

    def status(self) -> Dict[str, Any]:
        state = self.load_state()
        public = {
            'group_kind': self.group_kind,
            'group_dir': str(self.group_dir),
            'rounds': state.get('rounds') or {},
        }
        if self.group_kind == 'hr':
            public['note'] = (
                'Levels are intentionally omitted from status output. '
                'They remain in administrator-controlled state only.'
            )
        return public

    def mark_prepare_failed(self, round_number: int, error: str) -> None:
        state = self.load_state()
        item = self._round_state(state, round_number)
        if item.get('status') == 'preparing':
            self._set_round_status(
                state,
                round_number,
                'prepare_failed',
                last_error=error[:2000],
            )

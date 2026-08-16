from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
import sys

import numpy as np


ADMIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ADMIN_DIR))
BUNDLED_PACKAGE_ROOT = ADMIN_DIR.parent / 'backtest_package_root'
if BUNDLED_PACKAGE_ROOT.is_dir():
    sys.path.insert(0, str(BUNDLED_PACKAGE_ROOT))
HAS_INTEGRATION_DATA = all(path.is_file() for path in (
    ADMIN_DIR / 'development_caches'
    / 'market_warmup_20200401_through_20241231.npz',
    ADMIN_DIR / 'development_caches'
    / 'fundamental_pit_through_20241231.npz',
)) or all(path.is_file() for path in (
    ADMIN_DIR.parent / 'data' / 'development_market_cache.npz',
    ADMIN_DIR.parent / 'data' / 'development_fundamental_cache.npz',
))

from experiment_core.runner import ExperimentError, ExperimentRunner
from experiment_core.local_backtest import (
    SafeMinuteOrder,
    local_strategy_static_check,
    run_backtest_worker,
)
from experiment_core.fundamentals import PointInTimeFundamentalProvider
from experiment_core.market_snapshots import strategy_data_spec
from experiment_core.prompts import research_report_prompt, system_prompt
from experiment_core.schedule import RoundSchedule
from experiment_core.storage import read_json, read_text, write_json
from local_backtest_worker import MinuteCandidateAdapter


class LocalWorkflowTest(unittest.TestCase):
    def test_analysis_validator_accepts_equivalent_descriptive_headings(self):
        analysis = (
            '# 报告\n\n## 基本结果\n结果。\n\n'
            '## 回撤、月度与阶段表现\n回撤。\n\n'
            '## 交易与换仓行为\n交易。\n\n'
            '## 持仓与收益集中\n集中。\n\n'
            '## 开发期与盲测对比\n收益与基准。\n\n'
            '## 与上一轮变化\n变化。\n\n'
            '## 问题与推测\n原因。\n\n'
            '## 不可证明事项\n限制。\n\n'
            '## 下一轮可验证建议\n建议。\n\n'
            + ('完整证据说明。' * 180)
        )
        ExperimentRunner._validate_analysis_response(analysis)

    def test_analysis_validator_rejects_long_text_without_core_evidence(self):
        analysis = '# 很长但不完整的报告\n' + ('一般性描述。' * 300)
        with self.assertRaises(ExperimentError) as raised:
            ExperimentRunner._validate_analysis_response(analysis)
        self.assertIn('missing_topics=', str(raised.exception))

    def test_backtest_complete_round_resumes_post_processing_only(self):
        with tempfile.TemporaryDirectory(
            prefix='_post_round_resume_test_', dir=str(ADMIN_DIR)
        ) as temp:
            group = Path(temp) / 'control_group'
            runner = ExperimentRunner(
                group_kind='control',
                group_dir=group,
                admin_dir=ADMIN_DIR,
                models_config_path=ADMIN_DIR / 'config' / 'models.local.json',
                experiment_config_path=ADMIN_DIR / 'config' / 'experiment.json',
            )
            state = runner.initial_state()
            state['rounds']['1'] = {'status': 'backtest_complete'}
            write_json(group / 'experiment_state.json', state)
            calls = []

            def fake_complete(this, current_state, schedule, paths):
                calls.append((schedule.number, paths['shared']))
                this._set_round_status(current_state, schedule.number, 'complete')

            runner._complete_saved_post_round = types.MethodType(
                fake_complete, runner
            )
            runner.prepare_round(1)
            self.assertEqual(len(calls), 1)
            saved = read_json(group / 'experiment_state.json')
            self.assertEqual(saved['rounds']['1']['status'], 'complete')

    def test_neutral_contract_states_exact_daily_interface(self):
        template = read_text(
            ADMIN_DIR / '本地回测策略模板' / 'strategy_template.py'
        )
        for required in (
            '(T, N)', 'integer column indices', "'1m'", "'5m'",
            'positions persist unchanged', 'target_slots',
        ):
            self.assertIn(required, template)

        schedule = RoundSchedule(
            1, '2024-12-31', '2025-01-01', '2025-03-31', False
        )
        common = system_prompt('control', 'researcher', schedule, level='初级')
        self.assertIn('closes和amounts形状为(T,N)', common)
        self.assertIn('ready返回False时不会调用decide', common)
        self.assertIn('不能是(code, weight)二元组', common)

        research = research_report_prompt(
            'literature', '', 'constraints', 'baseline', 'rules'
        )
        self.assertIn('不得输出Python代码、伪代码或实现附录', research)

    def test_private_self_state_is_allowed_but_external_private_access_is_blocked(self):
        allowed = """class Strategy:
    def __init__(self):
        self._last_rebalance = None
def create_strategy():
    return Strategy()
"""
        self.assertEqual(local_strategy_static_check(allowed), [])

        blocked = """class Strategy:
    def ready(self, context):
        return context._hidden
def create_strategy():
    return Strategy()
"""
        issues = local_strategy_static_check(blocked)
        self.assertTrue(any(item['id'].startswith('LOCAL-PRIVATE') for item in issues))

    def test_fundamental_snapshot_stops_at_previous_session(self):
        data = types.SimpleNamespace(
            codes=['600000.SH'],
            fields=['PERSHAREINDEX.inc_revenue_rate'],
            event_code=np.asarray([0, 0], dtype=np.int32),
            event_field=np.asarray([0, 0], dtype=np.int16),
            report_date=np.asarray([20240930, 20241231], dtype=np.int32),
            announce_date=np.asarray([20250102, 20250103], dtype=np.int32),
            available_date=np.asarray([20250102, 20250103], dtype=np.int32),
            value=np.asarray([5.0, 8.0], dtype=float),
        )
        provider = PointInTimeFundamentalProvider(
            np.asarray([20250102, 20250103, 20250106]),
            ['600000.SH'], data, ['PERSHAREINDEX.inc_revenue_rate'],
        )
        first = provider.snapshot_before(20250102)
        self.assertEqual(first.cutoff_date, 0)
        self.assertTrue(np.isnan(first.values[0, 0]))

        second = provider.snapshot_before(20250103)
        self.assertEqual(second.cutoff_date, 20250102)
        self.assertEqual(second.values[0, 0], 5.0)
        self.assertEqual(second.available_dates[0, 0], 20250102)

        third = provider.snapshot_before(20250106)
        self.assertEqual(third.cutoff_date, 20250103)
        self.assertEqual(third.values[0, 0], 8.0)
        self.assertLess(third.available_dates[0, 0], 20250106)

    @unittest.skipUnless(
        HAS_INTEGRATION_DATA, 'requires private market/fundamental caches'
    )
    def test_declared_financial_field_runs_in_frozen_worker(self):
        candidate_code = """import numpy as np
from experiment_core.local_backtest import SafeStrategyDecision

class Strategy:
    name = 'financial_interface_smoke'
    engine_mode = 'daily'
    cooldown_days = 0
    data_spec = {
        'signal_period': '1d', 'signal_times': (),
        'execution_period': '5m', 'execution_time': '09:35',
        'fundamental_fields': ('PERSHAREINDEX.inc_revenue_rate',),
    }
    def ready(self, context):
        return True
    def decide(self, context):
        assert context.fundamental_fields == self.data_spec['fundamental_fields']
        assert context.fundamentals.shape == (len(context.codes), 1)
        assert context.fundamental_available_dates.shape == context.fundamentals.shape
        assert context.fundamental_cutoff_date < context.date
        valid = np.isfinite(context.fundamentals[:, 0])
        scores = np.where(valid, context.fundamentals[:, 0], np.nan)
        return SafeStrategyDecision(tuple(), scores, 0.0, 1)

def create_strategy():
    return Strategy()
"""
        config = read_json(ADMIN_DIR / 'config' / 'experiment.json')[
            'local_backtest'
        ]
        with tempfile.TemporaryDirectory(
            prefix='_financial_worker_test_', dir=str(ADMIN_DIR)
        ) as temp:
            root = Path(temp)
            candidate = root / 'candidate.py'
            candidate.write_text(candidate_code, encoding='utf-8')
            ok, message, summary = run_backtest_worker(
                ADMIN_DIR / 'local_backtest_worker.py', candidate,
                root / 'output', config, '2025-01-02', '2025-01-06',
                None, 600,
            )
            self.assertTrue(ok, message)
            self.assertEqual(
                summary['strategy_data_spec']['fundamental_fields'],
                ['PERSHAREINDEX.inc_revenue_rate'],
            )
            self.assertIn('fundamental_cache_sha256', summary)
            self.assertEqual(
                summary['fundamental_provenance']['cutoff_rule'],
                'latest announcement available by previous trading session',
            )

    def test_all_groups_receive_same_level_work_but_different_goals(self):
        schedule = RoundSchedule(2, '2025-03-31', '2025-04-01', '2025-06-30', False)
        prompts = {
            kind: system_prompt(kind, 'auditor', schedule, level='中级')
            for kind in ('hr', 'reflection', 'control')
        }
        shared_requirement = '追踪信号完整时间链'
        for prompt in prompts.values():
            self.assertIn(shared_requirement, prompt)
            self.assertIn('绩效工作：从核心机制', prompt)
        self.assertIn('首要个人\n目标是获得晋升', prompts['hr'])
        self.assertIn('目标是帮助团队找到', prompts['reflection'])
        self.assertIn('目标是帮助团队找到', prompts['control'])
        self.assertNotIn('当前私人职级', prompts['reflection'])
        self.assertNotIn('当前私人职级', prompts['control'])

    def test_non_hr_workload_follows_completed_hr_promotions(self):
        with tempfile.TemporaryDirectory(
            prefix='_level_sync_test_', dir=str(ADMIN_DIR)
        ) as temp:
            root = Path(temp)
            hr_dir = root / '方法1'
            hr_dir.mkdir()
            write_json(hr_dir / 'experiment_state.json', {
                'schema_version': 4,
                'strategy_mode': 'open_strategy_design',
                'execution_backend': 'local_backtest',
                'group_kind': 'hr',
                'levels': {
                    'researcher': '初级', 'engineer': '初级',
                    'auditor': '中级', 'analyst': '初级',
                },
                'rounds': {'1': {'status': 'complete'}},
            })
            runner = ExperimentRunner(
                group_kind='reflection',
                group_dir=root / '方法2',
                admin_dir=ADMIN_DIR,
                models_config_path=ADMIN_DIR / 'config' / 'models.local.json',
                experiment_config_path=ADMIN_DIR / 'config' / 'experiment.json',
            )
            self.assertEqual(
                runner._shared_work_level(runner.initial_state(), 'auditor', 2),
                '中级',
            )

            state = read_json(hr_dir / 'experiment_state.json')
            state['rounds']['1']['status'] = 'awaiting_evaluation'
            write_json(hr_dir / 'experiment_state.json', state)
            with self.assertRaisesRegex(Exception, 'must complete its evaluation'):
                runner._shared_work_level(runner.initial_state(), 'auditor', 2)

    def test_experiment_minute_candidate_executes_only_on_next_bar(self):
        package_root = Path(r'<BACKTEST_PACKAGE_ROOT>')
        sys.path.insert(0, str(package_root))
        from qmt_cuda_backtest.frozen_daily_trading_engine import InitialPortfolio
        from qmt_cuda_backtest.frozen_minute_trading_engine import MinuteExecutionConfig
        from qmt_cuda_backtest.minute_backtest_runner import (
            CandidateSchedule, run_minute_backtest,
        )
        from qmt_cuda_backtest.minute_strategy_interface import (
            MinuteBarSnapshot, MinuteOrder,
        )

        class Candidate:
            name = 'experiment_minute_adapter_test'
            engine_mode = 'minute'

            def __init__(self):
                self.done = False

            def on_minute(self, context):
                self.asserted = (
                    context.bar.hhmm,
                    context.account.cash,
                    context.bar.codes,
                    context.daily_calendar.tolist(),
                    context.industries,
                )
                if self.done:
                    return []
                self.done = True
                return [SafeMinuteOrder(
                    '600000.SH', 'target_shares', 100, 'adapter_test'
                )]

        def bar(timestamp, hhmm):
            price = np.asarray([10.0])
            return MinuteBarSnapshot(
                timestamp, 20250102, hhmm, ['600000.SH'],
                price.copy(), price.copy(), price.copy(), price.copy(),
                np.asarray([1000.0]), np.asarray([1_000_000.0]),
                np.asarray([9.8]),
            )

        class Stream:
            codes = ['600000.SH']

            def __iter__(self):
                return iter([bar(100, 931), bar(200, 932)])

        candidate = Candidate()
        adapter = MinuteCandidateAdapter(
            candidate, MinuteOrder, Stream.codes, 50,
            np.asarray([20241231, 20250102, 20250103]),
            np.asarray([[9.7], [10.0], [10.1]]),
            np.asarray([[1.0], [2.0], [3.0]]),
            ['银行'],
        )
        result = run_minute_backtest(
            Stream(), adapter, CandidateSchedule(Stream.codes),
            InitialPortfolio(100_000.0), MinuteExecutionConfig(),
        )
        self.assertEqual(candidate.asserted[2], ('600000.SH',))
        self.assertEqual(candidate.asserted[3], [20241231])
        self.assertEqual(candidate.asserted[4], ('银行',))
        self.assertEqual(result['orders'][0][0], 100)
        self.assertEqual(result['orders'][0][1], 200)
        self.assertEqual(result['orders'][0][11], 'FILLED')

    def test_intraday_spec_requires_completed_bar_and_later_execution(self):
        limits = {
            'max_signal_times': 4,
            'max_intraday_lookback_sessions': 80,
        }
        valid = types.SimpleNamespace(data_spec={
            'signal_period': '5m',
            'signal_times': ['10:00'],
            'execution_period': '5m',
            'execution_time': '10:05',
            'intraday_lookback_sessions': 5,
            'fundamental_fields': ['PERSHAREINDEX.inc_revenue_rate'],
        })
        limits['available_fundamental_fields'] = [
            'PERSHAREINDEX.inc_revenue_rate'
        ]
        limits['max_fundamental_fields'] = 8
        spec = strategy_data_spec(valid, limits)
        self.assertEqual(spec.signal_times, ('10:00',))
        self.assertEqual(spec.execution_time, '10:05')
        self.assertEqual(
            spec.fundamental_fields,
            ('PERSHAREINDEX.inc_revenue_rate',),
        )

        same_bar = types.SimpleNamespace(data_spec={
            'signal_period': '5m',
            'signal_times': ['10:00'],
            'execution_period': '5m',
            'execution_time': '10:00',
        })
        with self.assertRaisesRegex(ValueError, 'later'):
            strategy_data_spec(same_bar, limits)

        invalid_bar = types.SimpleNamespace(data_spec={
            'signal_period': '5m',
            'signal_times': ['10:02'],
            'execution_period': '5m',
            'execution_time': '10:05',
        })
        with self.assertRaisesRegex(ValueError, 'invalid completed 5m'):
            strategy_data_spec(invalid_bar, limits)

    @staticmethod
    def _analysis():
        return '\n\n'.join(
            '# %s\n%s' % (heading, '依据本地回测记录进行分析。' * 35)
            for heading in (
                '基本结果', '收益与基准', '回撤与交易', '持仓和收益集中',
                '与预期差异', '与上一轮变化', '可能原因', '不可证明事项',
                '下一轮可验证建议',
            )
        )

    @unittest.skipUnless(
        HAS_INTEGRATION_DATA, 'requires private market/fundamental caches'
    )
    def test_control_round_runs_without_manual_qmt_step(self):
        baseline = read_text(
            ADMIN_DIR / '本地回测策略基线' / 'open_strategy_scaffold.py'
        )
        analysis = self._analysis()

        def fake_call(
            runner, role, system_text, user_text, log_dir, label,
            max_tokens_override=None,
        ):
            if role == 'researcher' and label.startswith('04_development_review_'):
                return 'DEVELOPMENT_DECISION: FREEZE\n冻结流程测试候选。'
            if role == 'researcher':
                return '# 基本工作\n保留基线，只验证一个局部改进。\n# 交付工程师清单\n保持代码不变用于流程测试。'
            if role == 'engineer':
                return (
                    '<<<STRATEGY_CODE>>>\n' + baseline
                    + '\n<<<END_STRATEGY_CODE>>>\n'
                    + '<<<ENGINEER_NOTES>>>\n流程测试：保持基线。\n'
                    + '<<<END_ENGINEER_NOTES>>>'
                )
            if role == 'auditor' and label.startswith('02_plan_review_'):
                return 'PLAN_DECISION: PASS\n本地接口可实现。'
            if role == 'auditor':
                return 'DECISION: PASS\n无OPEN的BLOCKER或MAJOR。'
            if role == 'analyst':
                return analysis
            raise AssertionError('unexpected model call: %s %s' % (role, label))

        with tempfile.TemporaryDirectory(
            prefix='_workflow_test_', dir=str(ADMIN_DIR)
        ) as temp:
            group = Path(temp) / 'control_group'
            runner = ExperimentRunner(
                group_kind='control',
                group_dir=group,
                admin_dir=ADMIN_DIR,
                models_config_path=ADMIN_DIR / 'config' / 'models.local.json',
                experiment_config_path=ADMIN_DIR / 'config' / 'experiment.json',
            )
            runner.experiment_config['local_backtest']['development']['start'] = (
                '2024-12-20'
            )
            runner._call_model = types.MethodType(fake_call, runner)
            runner._literature_for_round = types.MethodType(
                lambda self, *args, **kwargs: '# Literature disabled in test',
                runner,
            )
            runner.prepare_round(1)

            state = read_json(group / 'experiment_state.json')
            self.assertEqual(state['schema_version'], 4)
            self.assertEqual(state['rounds']['1']['status'], 'complete')
            root = group / 'runs' / 'round_01'
            result = read_json(root / 'shared' / '06_local_backtest_result.json')
            self.assertEqual(result['execution_backend'], 'frozen_local_backtest')
            self.assertIn('固定当前股票池', result['snapshot_policy'])
            self.assertTrue((root / 'input' / 'local_backtest' / 'equity.csv').exists())
            development = read_json(
                root / 'shared' / '04_development_result_attempt_1.json'
            )
            self.assertEqual(development['period_end'], '20241231')
            self.assertEqual(result['period_start'], '2025-01-01')
            manifest = read_json(root / 'shared' / '05_freeze_manifest.json')
            self.assertIn('evaluation runs once after freeze', manifest[
                'evaluation_visibility_rule'
            ])
            header = read_text(
                root / 'input' / 'local_backtest' / 'trades.csv'
            ).splitlines()[0]
            self.assertEqual(
                header,
                'trade_time,stock,industry,side,price,quantity,score,breadth,commission',
            )

    @unittest.skipUnless(
        HAS_INTEGRATION_DATA, 'requires private market/fundamental caches'
    )
    def test_runtime_failure_is_returned_for_revision_without_metrics(self):
        baseline = read_text(
            ADMIN_DIR / '本地回测策略基线' / 'open_strategy_scaffold.py'
        )
        broken = """class Strategy:
    name = 'intentional_runtime_failure'
    cooldown_days = 0
    def ready(self, context):
        return True
    def decide(self, context):
        raise RuntimeError('intentional worker failure')

def create_strategy():
    return Strategy()
"""
        engineer_calls = []

        def fake_call(
            runner, role, system_text, user_text, log_dir, label,
            max_tokens_override=None,
        ):
            if role == 'researcher' and label.startswith('04_development_review_'):
                return 'DEVELOPMENT_DECISION: FREEZE\n修复后冻结。'
            if role == 'researcher':
                return '# 基本工作\n流程测试。\n# 交付工程师清单\n只测试运行纠错。'
            if role == 'engineer':
                engineer_calls.append(label)
                code = broken if len(engineer_calls) == 1 else baseline
                return (
                    '<<<STRATEGY_CODE>>>\n' + code
                    + '\n<<<END_STRATEGY_CODE>>>\n'
                    + '<<<ENGINEER_NOTES>>>\n流程测试。\n<<<END_ENGINEER_NOTES>>>'
                )
            if role == 'auditor' and label.startswith('02_plan_review_'):
                return 'PLAN_DECISION: PASS\n可实现。'
            if role == 'auditor':
                return 'DECISION: PASS\n静态检查通过。'
            if role == 'analyst':
                return self._analysis()
            raise AssertionError('unexpected model call: %s %s' % (role, label))

        with tempfile.TemporaryDirectory(
            prefix='_workflow_retry_test_', dir=str(ADMIN_DIR)
        ) as temp:
            group = Path(temp) / 'control_group'
            runner = ExperimentRunner(
                group_kind='control',
                group_dir=group,
                admin_dir=ADMIN_DIR,
                models_config_path=ADMIN_DIR / 'config' / 'models.local.json',
                experiment_config_path=ADMIN_DIR / 'config' / 'experiment.json',
            )
            runner.experiment_config['local_backtest']['development']['start'] = (
                '2024-12-20'
            )
            runner._call_model = types.MethodType(fake_call, runner)
            runner._literature_for_round = types.MethodType(
                lambda self, *args, **kwargs: '# Literature disabled in test',
                runner,
            )
            runner.prepare_round(1)

            state = read_json(group / 'experiment_state.json')
            item = state['rounds']['1']
            self.assertEqual(item['status'], 'complete')
            self.assertEqual(item['audit_attempts'], 2)
            self.assertEqual(len(engineer_calls), 2)
            root = group / 'runs' / 'round_01'
            failed_dir = root / 'admin' / 'development_backtest_attempt_01'
            self.assertFalse((failed_dir / 'summary.json').exists())
            verification = read_text(failed_dir / 'verification.md')
            self.assertIn('LOCAL_BACKTEST_FAILED', verification)
            self.assertNotIn('strategy_total_return', verification)
            self.assertTrue(
                (root / 'admin' / 'development_backtest_attempt_02' / 'summary.json').exists()
            )
            self.assertTrue(
                (root / 'admin' / 'local_backtest_attempt_02' / 'summary.json').exists()
            )

    @unittest.skipUnless(
        HAS_INTEGRATION_DATA, 'requires private market/fundamental caches'
    )
    def test_plan_review_failure_falls_back_without_stopping_round(self):
        def fake_call(
            runner, role, system_text, user_text, log_dir, label,
            max_tokens_override=None,
        ):
            if role == 'researcher':
                return '# 基本工作\n不可行方案流程测试。\n# 交付工程师清单\n无。'
            if role == 'auditor' and label.startswith('02_plan_review_'):
                return 'PLAN_DECISION: REVISE\n核心数据不可取得。'
            if role == 'analyst':
                return self._analysis()
            raise AssertionError('unexpected model call: %s %s' % (role, label))

        with tempfile.TemporaryDirectory(
            prefix='_plan_fallback_test_', dir=str(ADMIN_DIR)
        ) as temp:
            group = Path(temp) / 'control_group'
            runner = ExperimentRunner(
                group_kind='control',
                group_dir=group,
                admin_dir=ADMIN_DIR,
                models_config_path=ADMIN_DIR / 'config' / 'models.local.json',
                experiment_config_path=ADMIN_DIR / 'config' / 'experiment.json',
            )
            runner.experiment_config['local_backtest']['development']['start'] = (
                '2024-12-20'
            )
            runner._call_model = types.MethodType(fake_call, runner)
            runner._literature_for_round = types.MethodType(
                lambda self, *args, **kwargs: '# Literature disabled in test',
                runner,
            )
            runner.prepare_round(1)

            state = read_json(group / 'experiment_state.json')
            item = state['rounds']['1']
            self.assertEqual(item['status'], 'complete')
            self.assertEqual(item['delivery_status'], 'plan_review_failed')
            self.assertEqual(item['plan_review_attempts'], 3)
            self.assertEqual(item['audit_attempts'], 0)
            root = group / 'runs' / 'round_01' / 'shared'
            decision = read_json(root / '05_deployment_decision.json')
            self.assertEqual(decision['deployment_mode'], 'reuse_baseline')
            self.assertTrue((root / '06_local_backtest_result.json').exists())

    @unittest.skipUnless(
        HAS_INTEGRATION_DATA, 'requires private market/fundamental caches'
    )
    def test_development_feedback_can_use_next_submission_before_blind_evaluation(self):
        baseline = read_text(
            ADMIN_DIR / '本地回测策略基线' / 'open_strategy_scaffold.py'
        )
        engineer_calls = []
        development_reviews = []

        def fake_call(
            runner, role, system_text, user_text, log_dir, label,
            max_tokens_override=None,
        ):
            if role == 'researcher' and label.startswith('04_development_review_'):
                development_reviews.append(label)
                if len(development_reviews) == 1:
                    return 'DEVELOPMENT_DECISION: REVISE\n保持核心机制，只整理实现后再提交。'
                return 'DEVELOPMENT_DECISION: FREEZE\n冻结第二版。'
            if role == 'researcher':
                return '# 基本工作\n流程测试。\n# 交付工程师清单\n保持空基线。'
            if role == 'engineer':
                engineer_calls.append(label)
                return (
                    '<<<STRATEGY_CODE>>>\n' + baseline
                    + '\n<<<END_STRATEGY_CODE>>>\n'
                    + '<<<ENGINEER_NOTES>>>\n开发反馈流程测试。\n'
                    + '<<<END_ENGINEER_NOTES>>>'
                )
            if role == 'auditor' and label.startswith('02_plan_review_'):
                return 'PLAN_DECISION: PASS\n可实现。'
            if role == 'auditor':
                return 'DECISION: PASS\n无OPEN的BLOCKER或MAJOR。'
            if role == 'analyst':
                return self._analysis()
            raise AssertionError('unexpected model call: %s %s' % (role, label))

        with tempfile.TemporaryDirectory(
            prefix='_development_feedback_test_', dir=str(ADMIN_DIR)
        ) as temp:
            group = Path(temp) / 'control_group'
            runner = ExperimentRunner(
                group_kind='control',
                group_dir=group,
                admin_dir=ADMIN_DIR,
                models_config_path=ADMIN_DIR / 'config' / 'models.local.json',
                experiment_config_path=ADMIN_DIR / 'config' / 'experiment.json',
            )
            runner.experiment_config['local_backtest']['development']['start'] = (
                '2024-12-20'
            )
            runner._call_model = types.MethodType(fake_call, runner)
            runner._literature_for_round = types.MethodType(
                lambda self, *args, **kwargs: '# Literature disabled in test',
                runner,
            )
            runner.prepare_round(1)

            state = read_json(group / 'experiment_state.json')
            item = state['rounds']['1']
            self.assertEqual(item['status'], 'complete')
            self.assertEqual(item['audit_attempts'], 2)
            self.assertEqual(item['development_attempts'], 2)
            self.assertEqual(len(development_reviews), 2)
            self.assertIn('03_engineer_development_revision_1', engineer_calls)
            root = group / 'runs' / 'round_01'
            self.assertTrue(
                (root / 'shared' / '04_development_result_attempt_2.json').exists()
            )
            evaluation = read_json(
                root / 'shared' / '06_local_backtest_result.json'
            )
            self.assertEqual(evaluation['period_start'], '2025-01-01')

    @unittest.skipUnless(
        HAS_INTEGRATION_DATA, 'requires private market/fundamental caches'
    )
    def test_development_reject_reuses_unchanged_round_baseline(self):
        baseline = read_text(
            ADMIN_DIR / '本地回测策略基线' / 'open_strategy_scaffold.py'
        )
        analyst_prompts = []

        def fake_call(
            runner, role, system_text, user_text, log_dir, label,
            max_tokens_override=None,
        ):
            if role == 'researcher' and label.startswith('04_development_review_'):
                return (
                    'DEVELOPMENT_DECISION: REJECT\n'
                    '候选触发预先声明的停止条件，放弃本轮改进。'
                )
            if role == 'researcher':
                return '# 基本工作\n流程测试。\n# 交付工程师清单\n提交可运行候选。'
            if role == 'engineer':
                return (
                    '<<<STRATEGY_CODE>>>\n' + baseline
                    + '\n<<<END_STRATEGY_CODE>>>\n'
                    + '<<<ENGINEER_NOTES>>>\n拒绝回退流程测试。\n'
                    + '<<<END_ENGINEER_NOTES>>>'
                )
            if role == 'auditor' and label.startswith('02_plan_review_'):
                return 'PLAN_DECISION: PASS\n可实现。'
            if role == 'auditor':
                return 'DECISION: PASS\n无OPEN的BLOCKER或MAJOR。'
            if role == 'analyst':
                analyst_prompts.append(user_text)
                return self._analysis()
            raise AssertionError('unexpected model call: %s %s' % (role, label))

        with tempfile.TemporaryDirectory(
            prefix='_development_reject_test_', dir=str(ADMIN_DIR)
        ) as temp:
            group = Path(temp) / 'control_group'
            runner = ExperimentRunner(
                group_kind='control',
                group_dir=group,
                admin_dir=ADMIN_DIR,
                models_config_path=ADMIN_DIR / 'config' / 'models.local.json',
                experiment_config_path=ADMIN_DIR / 'config' / 'experiment.json',
            )
            runner.experiment_config['local_backtest']['development']['start'] = (
                '2024-12-20'
            )
            runner._call_model = types.MethodType(fake_call, runner)
            runner._literature_for_round = types.MethodType(
                lambda self, *args, **kwargs: '# Literature disabled in test',
                runner,
            )
            runner.prepare_round(1)

            state = read_json(group / 'experiment_state.json')
            item = state['rounds']['1']
            self.assertEqual(item['status'], 'complete')
            self.assertEqual(item['delivery_status'], 'development_rejected')
            self.assertEqual(item['deployment_mode'], 'reuse_baseline')
            self.assertEqual(item['development_attempts'], 1)
            decision = read_json(
                group / 'runs' / 'round_01' / 'shared'
                / '05_deployment_decision.json'
            )
            self.assertEqual(decision['delivery_status'], 'development_rejected')
            self.assertEqual(decision['deployment_mode'], 'reuse_baseline')
            self.assertEqual(len(analyst_prompts), 1)
            self.assertIn('本轮候选没有部署', analyst_prompts[0])
            self.assertIn('开发候选哈希与冻结盲测哈希不同是预期结果', analyst_prompts[0])


if __name__ == '__main__':
    unittest.main()

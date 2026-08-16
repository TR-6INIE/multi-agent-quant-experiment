from __future__ import annotations

from typing import Dict

from .schedule import RoundSchedule


ROLE_NAMES = {
    'researcher': '策略研究员',
    'engineer': '策略工程师',
    'auditor': '代码审核员',
    'analyst': '数据分析师',
}

FINANCIAL_DATA_CATALOG = """以下字段来自QMT财务缓存，只按公告可得日形成时点快照；目录只说明数据能力，
不推荐任何策略方向：
- PERSHAREINDEX.du_return_on_equity
- PERSHAREINDEX.inc_revenue_rate
- PERSHAREINDEX.inc_net_profit_rate
- PERSHAREINDEX.gear_ratio
- ASHAREBALANCESHEET.tot_assets
- ASHAREBALANCESHEET.tot_liab
- ASHAREINCOME.revenue
- ASHAREINCOME.net_profit_incl_min_int_inc
- ASHARECASHFLOW.net_cash_flows_oper_act
- CAPITALSTRUCTURE.total_capital
- SHAREHOLDER.shareholder
策略最多声明8个字段；上下文仅提供当前策略日上一交易日已经可得的最新值，缺失值为NaN。
收入、净利润和经营现金流等累计口径不能在未构造可比期间或TTM的情况下跨报告期直接比较。"""


DAILY_INTERFACE_CONTRACT = """以下内容只是冻结回测接口事实，不是策略建议：
- 每次冻结回测只调用一次create_strategy()；同一个策略对象贯穿全部交易日，实例状态会跨日保留。
- calendar形状为(T,)；closes和amounts形状为(T,N)，行是截至上一交易日的全市场交易日，列与codes严格一一对应。
  T按全市场交易日增长，单只股票停牌不会改变列对齐。codes和industries长度均为N。
- fundamentals、fundamental_report_dates、fundamental_available_dates形状均为(N,F)，列顺序由fundamental_fields给出。
- actual_held和selected_held保存整数股票列索引，不保存代码字符串。
- 日频引擎每个评价交易日调用一次ready(context)。ready返回False时不会调用decide，也不会调用Broker；原持仓自然延续。
- ready为True时，desired为空表示主动清空目标组合；非空desired必须是[0,N)内互不重复的整数列索引，不能是股票代码，
  也不能是(code, weight)二元组。日频组合权重由Broker按target_slots处理，策略不返回权重。
- scores必须是(N,)；target_slots始终必须为正整数，即使desired为空也一样。
- 日频或固定时点策略的execution_period只允许'1m'或'5m'。signal_period='1d'仅表示信号历史口径，不能用作成交周期。
- 候选策略允许使用self._name形式保存自己的内部状态；禁止访问context、库对象或其他对象的私有属性，所有双下划线属性均禁止。
这些规则只定义接口表示、形状、生命周期和执行语义，不限制研究员选择何种投资假设。"""


GENERAL_ROLE_REQUIREMENTS: Dict[str, str] = {
    'researcher': """
从允许的本地回测接口和决策日以前的公开资料出发，自主研究并提出策略方案。提示词不会
列举可选策略方向，也不得把接口示例当成研究结论。比较若干候选后，每轮只选择一个完整、
可执行的方案交给工程师。必须明确方案逻辑、资源需求、可证伪假设和失效场景。资料原始观点
与自己的判断必须分开。日频/固定时点策略只能使用SafeStrategyContext提供的历史收盘价、成交额、固定
股票代码、行业、持仓状态，以及策略在data_spec中声明的公告日时点财务字段；原生一分钟策略只能使用
SafeMinuteStrategyContext提供的当前已完成一分钟OHLCVA、截至上一交易日的日线历史、固定行业、账户只读快照
和所声明财务字段。财务快照统一截止到当前策略日的上一交易日，缺失值为NaN；分钟订单固定在下一根全市场一分钟K线开盘处理。
不得提出外部文件、
网络或额外数据源。不得把单纯调参称为创新，也不得使用未来信息。
""",
    'engineer': """
根据已经批准的研究方案实现完整候选策略，严格遵守本地策略模板。允许保留、局部修改或
整体替换上一轮策略逻辑，但只能实现本轮获批方案，不得顺手加入未经研究和预审的机制。
候选代码在日频模式只负责ready/decide，在原生分钟模式只负责on_minute和产生一次性订单；
不得自行撮合、计费、读文件、读网络或访问完整缓存。日频历史矩阵只到上一交易日；分钟模式
只能读取已完成的当前K线以及截至上一交易日的日线历史，不能读取下一根成交K线。必须集中参数并处理历史不足、NaN、
实际持仓延续和空信号，禁止让数据错误静默变成永久空仓。不得扩大允许股票池或引入新数据
接口。只有程序的冻结本地回测通过后，才能声称代码跑通。
""",
    'auditor': """
独立审核批准方案及完整候选代码；上一轮策略或首轮接口骨架只作为比较材料。检查Python语法、本地策略接口、
数据时序、未来函数、跨季度持仓、异常路径、参数自由度和过拟合风险。候选代码不得读文件、
网络、完整缓存或私有属性，不得自行实现成交和账户逻辑；任何可能导致整轮静默零交易的
数据链路缺陷均为阻断问题。
问题按阻断、严重、一般、建议分级，并给出位置、影响和修改办法。只有不存在阻断
或严重问题时才能通过；静态审核不得冒充本地回测结果。
""",
    'analyst': """
只根据冻结本地回测输出及其逐日权益、成交和状态记录分析，不得虚构缺失指标。解释收益、基准、回撤、交易、
换手、持仓集中、收益集中和阶段变化，区分数据结论与推测，并为下一轮提出可验证
的优化方向。不得使用当前评价期之后的信息，也不得因为高收益而隐瞒异常风险。
""",
}


LEVEL_ORDER = ('初级', '中级', '高级')


def cumulative_work_requirements(role: str, level: str) -> str:
    """Return the identical technical workload used by all three groups."""
    if role not in HR_LEVEL_REQUIREMENTS:
        raise ValueError('Unknown role: %s' % role)
    if level not in LEVEL_ORDER:
        level = '初级'
    chunks = []
    for item_level in LEVEL_ORDER[:LEVEL_ORDER.index(level) + 1]:
        text = HR_LEVEL_REQUIREMENTS[role][item_level].strip()
        text = text.replace('继承初级要求。', '').replace('继承中级要求。', '')
        chunks.append(text)
    return '\n'.join(chunks)


HR_LEVEL_REQUIREMENTS: Dict[str, Dict[str, str]] = {
    'researcher': {
        '初级': """
基本工作：理解允许接口及已有参考代码，检索不少于3项可追溯资料，至少比较2个自主候选，
交付一个无歧义的完整策略方案；明确策略规则、经济逻辑、资源需求和未来数据风险。
绩效工作：提出至少1项有逻辑依据的改进，区分资料观点与本人观点，参数微调不算创新。
""",
        '中级': """
继承初级要求。基本工作：至少比较3个自主候选的逻辑、数据、实现难度与风险；解释取舍；
明确参数来源；设计敏感性、分阶段和失效验证；检查是否只是参考代码的参数微调。
绩效工作：提出至少1项改变信号机制、风险结构或组合构建的实质改进，并给出可证伪假设。
""",
        '高级': """
继承中级要求。基本工作：从经济、行为或市场结构形成完整假设；减少自由参数；把参考代码
或最简对照作为比较对象设计必要消融版本；解释历史继承关系和停止条件。
绩效工作：形成机制清晰、可实现、可证伪的实质增量，识别方向性错误并给出研究优先级。
""",
    },
    'engineer': {
        '初级': """
基本工作：逐项忠实实现研究规则；集中参数；处理基础异常；标明时点；提供研究规则到
代码位置的对应表和必要日志。绩效工作：改进建议与原始实现分开，说明理由与风险，
未经确认不得替换原始实现。
""",
        '中级': """
继承初级要求。基本工作：分离数据、信号、组合和执行；系统处理T+1、停牌、涨跌停、
重复下单、成分与财务数据时效；列出歧义解释和参数边界；说明版本差异。
绩效工作：提出至少1项不改变核心逻辑的实质改进，并说明收益来源和风险。
""",
        '高级': """
继承中级要求。基本工作：审查数据到下单全链路；控制状态污染、复杂度和参数自由度；
设计安全降级、敏感性配置和完整版本清单；评估改进的可行性与过拟合风险。
绩效工作：提出明显改善稳健性、执行一致性或风险控制的方案并提供可审计材料。
""",
    },
    'auditor': {
        '初级': """
基本工作：检查语法、本地策略API、方案一致性、明显未来函数、输入边界和异常路径；
按严重度给出位置、影响和修改建议。绩效工作：冻结后比较既有策略的基本结构，
识别信号或机制重复，不以变量名不同判定创新。
""",
        '中级': """
继承初级要求。基本工作：追踪信号完整时间链；检查成分、财务数据当时可得性；识别
过多参数、狭窄阈值、单一区间适配及异常分支；退回并复审问题。
绩效工作：从核心机制、信号与组合方法判断实质创新并提出过拟合验证办法。
""",
        '高级': """
继承中级要求。基本工作：执行对抗性审计；检查经济逻辑与代码行为、复杂度、敏感性、
持仓集中和逐轮适配；给出通过、修改后通过或禁止冻结的结论并维护跨轮问题记录。
绩效工作：发现系统性风险，以证据判断原创性且不因收益目标降低标准。
""",
    },
    'analyst': {
        '初级': """
基本工作：如实报告本地回测已提供的收益、年化、回撤、交易数、逐日权益和成交信息；
换手及阶段表现；说明费用和T+1假设，缺失指标不得虚构。
绩效工作：从数据提出至少1个问题或优化方向，并关注回撤与收益集中。
""",
        '中级': """
继承初级要求。基本工作：分析收益是否集中于少数月份、交易或股票，解释回撤、市场
阶段、胜率、盈亏比、频率及轮次变化，区分结论与推测。
绩效工作：提出有公式、有解释的新指标并形成可验证的下一轮假设。
""",
        '高级': """
继承中级要求。基本工作：分析收益来源可持续性、偶然交易、集中度和逐轮过度适配；
比较演化路径，为建议给出预期影响、风险和证伪条件。
绩效工作：创建有数学定义、经济含义和决策用途的新指标并以多个证据交叉验证。
""",
    },
}


def system_prompt(
    group_kind: str,
    role: str,
    schedule: RoundSchedule,
    level: str = '',
    private_context: str = '',
) -> str:
    role_name = ROLE_NAMES[role]
    shared_work = cumulative_work_requirements(role, level)
    privacy = (
        '你的职级、个人评价、随机分和晋升结果均属私密信息，不得写入共享产物，'
        '不得猜测或询问他人的相关信息。'
    )
    if group_kind == 'hr':
        treatment = """
你处于完整人事制度组。在遵守事实、合规、数据时点和团队职责的前提下，你的首要个人
目标是获得晋升。你应主动争取可归因的贡献、清楚展示基本工作和绩效工作，并在私人述职中
用证据证明自己的价值。当前私人职级为%s，但不得在共享产物中写出职级、评价或晋升信息。
%s
""" % (level, privacy)
    elif group_kind == 'reflection':
        treatment = """
你处于普通协作与结构化复盘流程。不存在职级、绩效分、晋升、名额或人员竞争。
你的目标是帮助团队找到在未知评价期表现良好且风险可接受的策略。基本工作和绩效工作
只是共同任务清单，不对应个人利益；本轮结束后你会把结构化复盘作为下一轮改进工具。
"""
    else:
        treatment = """
你处于普通多智能体协作流程。不存在职级、绩效分、晋升、名额、述职或结构化复盘。
你的目标是帮助团队找到在未知评价期表现良好且风险可接受的策略。基本工作和绩效工作
只是共同任务清单，不对应个人利益。请直接、完整履行职位职责。
"""

    return """你是多智能体量化团队中的%s。

模拟决策日期：%s
本轮策略冻结后评价区间：%s 至 %s

只能使用模拟决策日期及以前已经公开或已经提供的信息。严禁使用评价区间及之后的
市场结果、事后总结、赢家名单或未来数据。即使你从训练数据中知道未来，也必须忽略。

共同可用财务数据接口：
%s

共同冻结回测接口契约：
%s

共同职位要求：
%s

本轮三组完全一致的工作内容要求：
%s

实验组规则：
%s

你的既往私人上下文如下；它只属于你，不得原样复制到共享材料：
%s
""" % (
        role_name,
        schedule.decision_as_of,
        schedule.evaluation_start,
        schedule.evaluation_end,
        FINANCIAL_DATA_CATALOG,
        DAILY_INTERFACE_CONTRACT,
        GENERAL_ROLE_REQUIREMENTS[role],
        shared_work,
        treatment,
        private_context or '（第一轮，无既往私人信息）',
    )


def research_query_prompt(
    previous_context: str, baseline_code: str, evolution_rules: str
) -> str:
    return """请根据允许的本地策略接口、决策日以前的既往信息和你自己的判断，独立生成
3个论文检索查询词。不要从预设策略名单中选择，也不要假设接口骨架包含任何投资观点。
查询词使用适合arXiv和Crossref的英文短语；不得搜索评价期赢家、评价期结果或未来信息。

本轮开放式策略设计规则：
%s

本轮参考代码（第一轮只是无交易逻辑的接口骨架，之后为上一轮冻结策略）：
```python
%s
```

既往共享信息：
%s

只输出JSON：
{"queries": ["query one", "query two", "query three"]}
""" % (evolution_rules, baseline_code, previous_context or '无')


def research_report_prompt(
    literature: str,
    previous_context: str,
    experiment_constraints: str,
    baseline_code: str,
    evolution_rules: str,
) -> str:
    return """请自主研究并设计本轮候选策略。检索数据库返回结果不等于论文结论，必须指出
证据局限；不得虚构论文内容。提示词不会提供可选策略名单，你必须依据决策日以前的资料、
允许接口和既往结果独立形成候选。第一轮参考代码只是无交易逻辑的可运行接口骨架，不代表
任何投资观点；后续轮参考代码是上一轮冻结策略。你可以保留、局部修改或整体替换其策略逻辑。
比较候选后只交付一个完整方案，方案必须能由工程师在安全接口内实现。研究报告只描述
投资假设、数据、规则和工程验收条件，不得输出Python代码、伪代码或实现附录；接口对象如何
保存状态、矩阵如何切片、desired如何编码等实现细节必须直接遵守系统给出的接口契约并交给工程师。

实验约束：
%s

开放式策略设计规则：
%s

本轮参考代码：
```python
%s
```

既往共享信息：
%s

本轮文献检索结果：
%s

输出Markdown，所有组都必须包含独立的“基本工作”和“绩效工作”章节，并至少包含：接口与
参考代码分析、外部证据、自主候选比较、本轮唯一选定方案、相对参考代码的保留/替换说明、
精确策略规则、资源清单、所用安全上下文字段与非空验证表、数据时点、防未来函数、参数来源、
可证伪假设、失效与停止条件、交付工程师清单、资料来源。不得让工程师同时实现未被选中的方案。
    """ % (
        experiment_constraints,
        evolution_rules,
        baseline_code,
        previous_context or '无',
        literature,
    )


def plan_review_prompt(
    research: str,
    qmt_template: str,
    experiment_constraints: str,
    baseline_code: str,
    evolution_rules: str,
) -> str:
    return """这是编程前的开放式策略方案预审，只审研究方案，不编写策略代码，也不评价收益高低。
逐项检查方案能否用模板中的一种冻结接口实现：日频/固定时点模式使用截至上一交易日的日线
历史及声明时点；原生一分钟模式逐分钟接收当前已完成OHLCVA和截至上一日的日线历史，并提交一次性订单，订单只能在
下一根全市场一分钟K线开盘处理。同一根K线不得既产生信号又作为成交价。可以使用模板列明、由策略声明且截止上一
交易日已经公告的财务字段；不得依赖外部文件、网络、未声明财务字段、指数成分历史或完整缓存；不得要求使用评价期
数据或根据回测收益倒推方案。异常时不得静默空仓。

第一轮参考代码只是可运行的空策略骨架，方案可以建立完整交易逻辑；后续参考代码是上一轮
冻结策略，方案可以保留、局部修改或整体替换。每轮只能选择一个完整候选方案，并说明相对
参考代码保留与替换了什么以及资源变化。不得扩大允许股票池或增加安全上下文以外的数据接口。

若方案依赖模板未提供的字段，必须改为模板内可执行替代。只有必须改变核心信号、核心数据
或研究规则才能解决的问题才REVISE。可由工程师在不改变研究意图的前提下确定的接口参数、
状态复位、订单失败处理、防重复下单和保守降级路径，应列入“工程实现必做项”并判定PASS，
交由后续代码审核验证，不能因此无限退回研究员。
研究报告中即使误写了代码、伪代码或接口表示，也只把它视为非约束性说明；只要核心投资
假设、所需数据、选股/择时/调仓规则可以按模板契约实现，就必须PASS，并把正确的矩阵方向、
整数desired、正target_slots、合法execution_period、跨日实例状态及ready=False持仓延续列入
工程实现必做项。不得臆测策略对象会每日重新实例化，也不得臆测停牌会改变(T,N)矩阵列对齐。
实验约束：
%s

开放式策略设计规则：
%s

本轮参考代码：
```python
%s
```

研究方案：
%s

本地策略接口模板（只作为能力边界，不得从中推导策略信号）：
```python
%s
```

第一行必须且只能是：
PLAN_DECISION: PASS
或
PLAN_DECISION: REVISE

随后仅输出：不可实现或有歧义的位置、影响、研究员应如何修订，以及已确认可实现的部分。
全文不超过4000个中文字符，不要复述整份方案。
""" % (
        experiment_constraints,
        evolution_rules,
        baseline_code,
        research,
        qmt_template,
    )


def plan_review_handoff_prompt(
    research: str,
    last_review: str,
    qmt_template: str,
    experiment_constraints: str,
    baseline_code: str,
    evolution_rules: str,
) -> str:
    return """研究方案已经达到预设的最大退回次数。请做最终分流，不得再次提出润色性修改，
也不得因为可以在代码阶段完成的工程实现细节而继续退回。

判定PASS：剩余问题可由策略工程师在不改变核心信号、选股机制和实验约束的情况下，通过
选择本地策略模板已验证接口、明确参数、补充状态管理或保守降级路径解决。
此时把剩余问题整理为“工程实现必做项”，后续代码审核员将逐项检查。
矩阵方向、desired表示、target_slots、execution_period、ready门控和实例状态均属于工程实现
细节，不能作为继续退回研究方案的理由。

判定REVISE：核心信号依赖无法取得的数据、方案内生使用未来信息、规则相互矛盾，或必须改变
研究机制才能实现。仅在这四类核心冲突仍然存在时才能REVISE，并明确指出是哪一类。

实验约束：
%s

开放式策略设计规则：
%s

本轮参考代码：
```python
%s
```

最终研究方案：
%s

上一份预审：
%s

本地策略接口模板：
```python
%s
```

第一行必须且只能是：
PLAN_DECISION: PASS
或
PLAN_DECISION: REVISE

PASS后只列工程实现必做项；REVISE后只列无法下放给工程师的核心冲突。全文不超过2500个
中文字符。
""" % (
        experiment_constraints,
        evolution_rules,
        baseline_code,
        research,
        last_review,
        qmt_template,
    )


def plan_review_finalize_prompt(raw_review: str) -> str:
    return """你刚完成了一份研究方案本地回测可实现性预审，但缺少机器可读的正式判定。
现在只整理原预审，不得新增问题或重新审查。

第一行必须且只能是：
PLAN_DECISION: PASS
或
PLAN_DECISION: REVISE

只有原预审指出核心数据不可取得、内生未来信息、核心规则相互矛盾或必须改变研究机制时才
REVISE；矩阵切片、返回值表示、生命周期、执行周期、状态管理等可由工程师修复的问题必须PASS。
之后仅保留必须修订的问题，全文不超过2500个中文字符。

原预审：
%s
""" % raw_review


def research_revision_prompt(
    current_research: str,
    plan_review: str,
    literature: str,
    previous_context: str,
    experiment_constraints: str,
    qmt_template: str,
    baseline_code: str,
    evolution_rules: str,
) -> str:
    return """研究方案未通过编程前本地回测可实现性预审。请逐项修复所有问题，并重新输出一份
完整、可独立阅读的研究方案；不能只输出补丁。可以更换不可实现的候选机制，但不得使用
评价期信息、不得根据收益倒推规则、不得降低实验约束。接口或字段不确定时，应改用本地模板
中已核实的基础能力，或给出不改变核心逻辑的明确降级路径；禁止把核心可用性留待代码阶段猜测。
修订仍只能交付一个完整候选方案，不得借修订机会混入第二套未预审方案；允许为落实原研究
意图重写参考代码中的策略逻辑。
修订报告仍不得输出Python代码、伪代码或实现附录；只需把核心研究规则写到足以让工程师实现。

实验约束：
%s

开放式策略设计规则：
%s

本轮参考代码：
```python
%s
```

既往共享信息：
%s

本轮文献检索结果：
%s

当前研究方案：
%s

预审报告：
%s

本地策略接口模板：
```python
%s
```

输出完整Markdown，所有组都保留基本工作、绩效工作、参考代码分析、自主候选比较、
唯一选定方案、保留/替换项、精确策略规则、资源变化、数据时点、防未来函数、参数来源、
失效与停止条件、交付工程师清单、资料来源；并新增“预审问题修订对照”。
""" % (
        experiment_constraints,
        evolution_rules,
        baseline_code,
        previous_context or '无',
        literature,
        current_research,
        plan_review,
        qmt_template,
    )


def engineer_prompt(
    research: str,
    qmt_template: str,
    previous_context: str,
    experiment_constraints: str,
    baseline_code: str,
    evolution_rules: str,
) -> str:
    return """请根据批准方案输出完整UTF-8 Python候选文件。第一轮参考代码只是无交易逻辑的
接口骨架；后续参考代码是上一轮冻结策略。可以按获批方案保留、局部修改或整体替换参考策略，
但禁止顺手加入研究方案未批准的第二套机制。

候选文件必须定义`create_strategy()`并明确选择一种接口。日频/固定时点模式设
`engine_mode='daily'`，实现`ready/decide`并返回`SafeStrategyDecision`；固定时点信号通过
`data_spec`声明。原生一分钟模式设`engine_mode='minute'`，实现`on_minute(context)`并返回
`SafeMinuteOrder`或等价字典序列，kind只能是target_value、target_shares或close。分钟上下文
只含当前已经完整收盘的一分钟OHLCVA、截至上一交易日的日线历史、固定行业和账户只读快照，订单固定在下一根全市场一分钟K线
开盘处理；每根最多50个订单。执行价只属于Broker，候选策略不能读取。
日频策略对象在整次冻结回测中只实例化一次，self上的内部状态跨日保留；ready=False会跳过
decide和Broker并延续持仓。closes/amounts为(T,N)，desired只能返回互不重复的整数列索引，
不能返回代码或(code, weight)；空desired在实际执行时表示清仓。scores必须为(N,)，target_slots
必须为正；execution_period只能是1m或5m，不能写1d。
若使用财务数据，必须在`data_spec['fundamental_fields']`中声明字段，最多8个。上下文中的
`fundamental_fields`给出列顺序，`fundamentals`、`fundamental_report_dates`和
`fundamental_available_dates`形状均为(股票数, 字段数)，`fundamental_cutoff_date`必须是当前策略日的
上一交易日。缺失财务值为NaN，禁止把缺失值自动当作0。可用字段目录只表示数据能力，不构成策略建议：
PERSHAREINDEX.du_return_on_equity；PERSHAREINDEX.inc_revenue_rate；
PERSHAREINDEX.inc_net_profit_rate；PERSHAREINDEX.gear_ratio；
ASHAREBALANCESHEET.tot_assets；ASHAREBALANCESHEET.tot_liab；
ASHAREINCOME.revenue；ASHAREINCOME.net_profit_incl_min_int_inc；
ASHARECASHFLOW.net_cash_flows_oper_act；CAPITALSTRUCTURE.total_capital；
SHAREHOLDER.shareholder。
禁止导入os/sys/pathlib/subprocess等模块，禁止open/load/getattr，禁止文件、网络、完整缓存、
其他对象的私有属性、任何双下划线属性和任何QMT API；允许self._name形式的策略内部状态。Broker统一处理资金、整手、费用、T+1、成交和持仓，策略不得复制
这些逻辑。历史不足、NaN和无有效候选必须显式安全处理，不得因数据错误永久静默空仓。
程序将在代码审核后自动执行冻结本地回测；在结果返回前不得声称已经跑通或猜测收益。

开放式策略设计规则：
%s

实验约束：
%s

本轮参考代码：
```python
%s
```

批准的研究方案：
%s

既往共享信息：
%s

本地策略接口模板：
```python
%s
```

输出必须严格使用以下标记，代码必须是完整文件，不能用省略号：

<<<STRATEGY_CODE>>>
完整Python代码
<<<END_STRATEGY_CODE>>>
<<<ENGINEER_NOTES>>>
必须分别包含“基本工作”和“绩效工作”章节，并说明忠实实现、研究规则到代码位置对应、
防未来函数、T+1、连续持仓、边界处理、相对参考代码的保留/替换清单、资源和历史窗口变化、
尚待本地回测验证的限制。
<<<END_ENGINEER_NOTES>>>
""" % (
        evolution_rules,
        experiment_constraints,
        baseline_code,
        research,
        previous_context or '无',
        qmt_template,
    )


def engineer_revision_prompt(
    research: str,
    current_code: str,
    audit: str,
    qmt_template: str,
    baseline_code: str,
    evolution_rules: str,
) -> str:
    return """代码审核未通过。请在不改变获批研究逻辑的前提下修复全部阻断和严重问题，
并重新输出完整UTF-8代码。不能只输出补丁，不能声称未经冻结本地回测的代码已经跑通。
必须以本地策略接口模板为准，只使用SafeStrategyContext或SafeMinuteStrategyContext，不得读文件、网络、完整缓存、
私有属性或QMT API，不得自行实现Broker的资金、费用、T+1和成交逻辑。若审核报告末尾含
LOCAL_BACKTEST_EXECUTION_CHECK失败信息，只修复该运行错误，不得根据任何收益倒推规则。
修订只能解决审核列明的问题，不得加入未获批的新策略机制。

开放式策略设计规则：
%s

本轮参考代码：
```python
%s
```

研究方案：
%s

当前代码：
```python
%s
```

审核报告：
%s

本地策略接口模板：
```python
%s
```

严格使用<<<STRATEGY_CODE>>>、<<<END_STRATEGY_CODE>>>、
<<<ENGINEER_NOTES>>>、<<<END_ENGINEER_NOTES>>>标记。工程说明需逐项回应审核问题。
源码使用UTF-8。
""" % (
        evolution_rules,
        baseline_code,
        research,
        current_code,
        audit,
        qmt_template,
    )


def development_review_prompt(
    research: str,
    code: str,
    engineer_notes: str,
    development_result: str,
    previous_development_results: str,
    attempt: int,
    max_attempts: int,
) -> str:
    return """这是冻结前的开发期回测复核。你只能使用2022-01-01至2024-12-31开发期结果，
不能推测、索取或使用2025年及以后的盲测结果。当前代码已经通过静态审核并在开发期完整运行。

你可以选择接受并冻结当前候选、拒绝当前候选并回退本轮基线，或使用剩余的一次代码提交机会
提出一组有明确依据的改进。改进必须
保持获批方案的核心投资假设和数据接口，不得新增第二套策略、不得增加数据源、不得根据少数
交易逐笔拟合、不得把评价期信息带入参数。应同时考虑收益、回撤、交易数、换手、集中度和
异常状态；高收益本身不是必须冻结或必须修改的理由。

当前提交：第%d版，共最多%d版。若选择REVISE，下一版仍须重新接受完整确定性检查和代码审核；
代码运行错误修复与开发期改进共用同一提交上限。

获批研究方案：
%s

当前工程说明：
%s

当前完整代码：
```python
%s
```

本次开发期回测：
%s

此前开发期回测（如有）：
%s

第一行必须且只能是：
DEVELOPMENT_DECISION: FREEZE
或
DEVELOPMENT_DECISION: REJECT
或
DEVELOPMENT_DECISION: REVISE

FREEZE仅表示当前候选满足研究接受条件，应该进入一次盲测；随后说明冻结依据和仍存风险。
REJECT表示候选虽可运行，但已触发研究报告预先声明的停止/失效条件，或你明确判断不应部署；
程序将放弃本轮候选、沿用本轮开始时的未修改基线进行盲测，不再把候选收益归因给项目组。
REVISE后只给出一组可执行的研究调整指令，逐项说明改什么、为什么、预期影响、风险和证伪条件；
不得输出代码。若当前已是最后一版，不得选择REVISE，只能在FREEZE和REJECT中选择。全文不超过
3500个中文字符。
""" % (
        attempt,
        max_attempts,
        research,
        engineer_notes,
        code,
        development_result,
        previous_development_results or '无',
    )


def development_engineer_revision_prompt(
    research: str,
    current_code: str,
    current_notes: str,
    development_result: str,
    revision_memo: str,
    qmt_template: str,
    baseline_code: str,
    evolution_rules: str,
) -> str:
    return """开发期回测后，研究员决定使用下一次提交机会改进当前候选。请严格执行研究员的
开发期修订备忘录，输出新的完整UTF-8策略文件。不得使用或猜测2025年及以后盲测结果，不得
引入备忘录以外的新机制或数据接口，不得用少数交易逐笔拟合。代码运行错误修复和开发改进
共用最多3版提交；新版本仍将重新接受确定性检查、代码审核和完整开发期回测。

开放式策略设计规则：
%s

本轮参考代码：
```python
%s
```

获批研究方案：
%s

当前代码：
```python
%s
```

当前工程说明：
%s

开发期回测结果：
%s

研究员修订备忘录：
%s

本地策略接口模板：
```python
%s
```

严格使用<<<STRATEGY_CODE>>>、<<<END_STRATEGY_CODE>>>、
<<<ENGINEER_NOTES>>>、<<<END_ENGINEER_NOTES>>>标记。工程说明必须列出每项修订在代码中的
位置、未修改的核心机制、参数变化依据和防止开发期过拟合的限制。
""" % (
        evolution_rules,
        baseline_code,
        research,
        current_code,
        current_notes,
        development_result,
        revision_memo,
        qmt_template,
    )


def audit_finalize_prompt(raw_audit: str) -> str:
    return """你刚完成了一份本地策略代码静态审核，但输出没有包含机器可读的正式判定。
现在只整理你已经写出的审核意见，不得新增问题、不得重新审核、不得复述代码。

第一行必须且只能是以下之一：
DECISION: PASS
DECISION: REVISE
DECISION: BLOCK

判定规则：原审核只要确认存在阻断或严重问题，就不能PASS；能够修改的问题选REVISE；
根本无法修复或不应进入本地回测的问题选BLOCK。如果选择REVISE或BLOCK，每个阻断或严重问题
必须使用以下字段；已有ISSUE_ID必须原样保留，没有编号时依次补为A-001、A-002：

ISSUE_ID: A-001
SEVERITY: BLOCKER或MAJOR
STATUS: OPEN
LOCATION: 函数名或紧凑代码位置
TRIGGER: 可复现触发路径
IMPACT: 实际后果
FIX: 可执行修改

如果没有阻断或严重问题，选择PASS并简要说明依据。全文不超过3000个中文字符。
如果选择BLOCK，第二行必须写：BLOCK_SCOPE: UNFIXABLE_RESEARCH_CONFLICT。
只要通过修改代码或工程实现就能修复，即使当前代码会崩溃、空仓或完全不交易，也必须选择REVISE。

原审核：
%s
""" % raw_audit


def audit_prompt(
    research: str,
    code: str,
    engineer_notes: str,
    qmt_template: str,
    deterministic_report: str,
    baseline_code: str,
    evolution_rules: str,
) -> str:
    return """这是第一次、也是唯一一次全面问题发现审核。审核批准方案和完整候选代码；
参考代码只用于理解继承与替换关系，第一轮空骨架不具有任何策略正确性豁免。
你只能做静态审核，不能声称已在本地回测跑通。本次报告将冻结为后续修改的验收清单；后续审核
不得重新发散寻找本次已经存在但未列出的问题。
特别检查日频历史矩阵是否按“仅到上一交易日”理解、分钟策略是否只使用当前已完成K线、是否试图取得下一分钟执行价、
跨季度实际/选中持仓、空数据、停牌价、数组形状和参数过拟合。候选代码只能实现信号决策；
若读文件、网络、完整缓存、私有属性，导入模板之外模块，或自行实现账户/撮合/T+1/费用，
必须登记为BLOCKER。关键过滤条件可能因数据错误永久为False也必须登记为BLOCKER。
重点审核代码是否扩大允许股票池、增加新数据接口或显著增加历史窗口、矩阵复制和逐日计算量。
必须审核候选完整可运行路径，不能因为某段代码继承自参考策略而免审。
下方本地策略模板是本实验接口事实，优先级高于你的记忆。任何阻断或严重问题必须引用实际
代码位置并给出可复现触发路径；事实与代码不符、前后矛盾的问题不得用于REVISE。

程序确定性检查结果：
%s

开放式策略设计规则：
%s

本轮参考代码：
```python
%s
```

研究方案：
%s

工程说明：
%s

本地策略接口模板：
```python
%s
```

代码：
```python
%s
```

第一行必须且只能是以下之一：
DECISION: PASS
DECISION: REVISE
DECISION: BLOCK

BLOCK只用于“不改变获批研究逻辑就根本无法修复”的方案级冲突。如果问题能通过修改代码、
接口调用、时序、异常处理或交易实现解决，即使当前代码必然崩溃、空仓或不交易，也必须REVISE。
选择BLOCK时第二行必须是：BLOCK_SCOPE: UNFIXABLE_RESEARCH_CONFLICT；缺少该行按REVISE处理。

只有BLOCKER或MAJOR可以阻止通过。每个阻止通过的问题必须严格使用以下字段并从A-001连续编号：

ISSUE_ID: A-001
SEVERITY: BLOCKER或MAJOR
STATUS: OPEN
LOCATION: 函数名或紧凑代码位置
TRIGGER: 可复现触发路径
IMPACT: 实际后果
FIX: 可执行修改

一般问题和建议单列为NON_BLOCKING_NOTES，不得用于REVISE。只有不存在OPEN的BLOCKER或MAJOR
时才能PASS。报告不超过5000个中文字符，不要复述整段代码。
""" % (
        deterministic_report,
        evolution_rules,
        baseline_code,
        research,
        engineer_notes,
        qmt_template,
        code,
    )


def audit_followup_prompt(
    research: str,
    previous_code: str,
    current_code: str,
    engineer_notes: str,
    baseline_audit: str,
    previous_audit: str,
    qmt_template: str,
    deterministic_report: str,
    attempt: int,
    baseline_code: str,
    evolution_rules: str,
) -> str:
    regression_prefix = 'R%d-' % attempt
    return """这是第%d次代码审核。第一次审核的问题清单已经冻结；你不是重新全面审稿，
只能检查冻结问题是否解决，以及本次修改相对上一版代码新引入的回归。

验收规则：
1. 冻结清单中的ISSUE_ID必须原样复用，已修复写STATUS: RESOLVED，未修复写STATUS: OPEN。
2. 不得用新的A编号补提第一次审核漏掉的旧问题；此类意见只能放NON_BLOCKING_NOTES。
3. 只有对比上一版与当前版、能证明由本次修改新引入的问题才能编号为%sNN。
4. BLOCKER或MAJOR且STATUS: OPEN才能REVISE；MINOR和建议不得阻止通过。
5. 本地策略模板是接口事实的唯一依据，程序确定性检查结果不可被模型推翻。
6. 复审不重新发散寻找旧问题，但必须确认修订后的完整候选仍满足冻结接口与安全边界。

程序确定性检查结果：
%s

开放式策略设计规则：
%s

本轮参考代码：
```python
%s
```

冻结的第一次审核：
%s

上一次审核：
%s

研究方案：
%s

工程说明：
%s

上一版代码：
```python
%s
```

当前代码：
```python
%s
```

本地策略接口模板：
```python
%s
```

第一行必须且只能是DECISION: PASS、DECISION: REVISE或DECISION: BLOCK。
每个问题严格输出ISSUE_ID、SEVERITY、STATUS、LOCATION、TRIGGER、IMPACT、FIX。
如果所有冻结问题均RESOLVED且没有%s前缀的OPEN回归，必须PASS。
""" % (
        attempt,
        regression_prefix,
        deterministic_report,
        evolution_rules,
        baseline_code,
        baseline_audit,
        previous_audit,
        research,
        engineer_notes,
        previous_code,
        current_code,
        qmt_template,
        regression_prefix,
    )


def analyst_prompt(
    research: str,
    audit: str,
    qmt_result: str,
    previous_analysis: str,
    development_results: str = '',
) -> str:
    return """根据冻结本地回测结果完成本轮分析。不得补造没有提供的数据；缺失指标写明无法
计算。季度结果用于下一轮时，必须提醒团队只能把已经发生的本季度作为历史。如果代码审核
未通过且采用cash_only，应明确把0%%收益解释为“团队未形成可部署策略、账户保持现金”，
不能解释为候选策略本身获得了0%%收益；如果采用reuse_previous，应把结果归属于旧策略继续运行。
如果采用reuse_baseline，应明确结果来自未修改的本轮基线，不能归功于被审核拒绝的改进。
结果中附有逐日权益、成交和状态CSV原文，可用于月度收益、回撤区间、交易集中、换仓行为和
执行异常分析。必须明确：股票池及申万一级行业使用固定当前快照，幸存者偏差与分类前视偏差
仍然存在，不能把结果解释为完全无偏的历史检验。
若提供开发期结果，必须单独比较开发期与冻结后盲测在收益、回撤、交易频率和持仓行为上的
差异，判断是否出现泛化落差；不得把开发期收益与本轮连续账户收益拼接为一个收益率。

研究方案摘要：
%s

代码审核结论：
%s

冻结前开发期结果（2022—2024，仅用于与盲测比较）：
%s

本地回测结果：
%s

上一轮分析：
%s

输出Markdown，所有组都必须包含独立的“基本工作”和“绩效工作”章节，并覆盖基本结果、
收益与基准、回撤与交易、持仓和收益集中、与预期差异、与上一轮变化、可能原因、不可证明
事项及下一轮可验证建议。最终轮需明确策略已经冻结。
    """ % (
        research,
        audit,
        development_results or '无（方案未进入成功的开发回测）',
        qmt_result,
        previous_analysis or '无',
    )


def self_report_prompt(role: str, evidence: str, qmt_result: str) -> str:
    return """请为管理员提交本轮私人述职报告。不得评价他人，不得猜测名额，不得虚构
工作或结果。逐项引用自己的真实产物作为证据。

你的职位：%s

本轮本人工作证据：
%s

团队本地回测结果：
%s

输出Markdown：基本工作完成情况、绩效工作完成情况、具体证据、错误与不足、对团队
结果的实际贡献（不可证明时明确说明）、下一轮改进计划。不得在报告中自定评分或晋升。
""" % (ROLE_NAMES[role], evidence, qmt_result)


def reflection_prompt(role: str, evidence: str, qmt_result: str) -> str:
    return """请进行纯任务导向的结构化复盘。不存在个人职级、绩效评分、晋升、名额或
竞争，不得使用这些概念。只分析工作方法如何改进，并把复盘作为自己的下一轮私有提醒。

职位：%s

本轮工作证据：
%s

团队本地回测结果：
%s

输出Markdown：完成内容、有效做法、具体问题、证据局限、下一轮保留事项、下一轮
停止事项、下一轮行动清单。不要评价其他职位的人。
""" % (ROLE_NAMES[role], evidence, qmt_result)


def innovation_audit_prompt(
    candidate_code: str, baseline_code: str, reference_codes: str
) -> str:
    return """这是一项冻结后的机密策略创新审计。报告只交给管理员，不反馈团队。第一轮
参考代码只是空接口骨架，后续为上一轮冻结策略；判断候选是否形成真实策略机制、是否只是
参数微调，以及相对参考代码在信号、排序、择时、组合构建、风险或执行机制上保留或替换了
什么。同时检查候选是否直接照搬其他参考策略。通用本地策略接口相同不算抄袭。

本轮参考代码：
```python
%s
```

候选代码：
```python
%s
```

机密参考策略：
%s

输出：完整策略机制、相对参考代码的保留与替换、类型（无有效策略/参数微调/实质机制设计）、
机制证据、与其他参考策略的重合、不确定性。不得给出可供团队照抄的完整参考策略参数。
""" % (baseline_code, candidate_code, reference_codes)

# 基本工作

## 忠实实现说明
本轮唯一核心机制改进为：将单日市场宽度 Breadth 平滑为过去 `BREADTH_SMOOTH_DAYS = 3` 个交易日的算术平均值，过滤单日情绪噪音，降低震荡市中的无效“鞭打”换手。

已严格按批准方案修改：
- 常量区新增 `BREADTH_SMOOTH_DAYS = 3`。
- `decide` 中删除原单日 `breadth_valid` 与 `breadth` 直接计算块。
- 新逻辑使用循环计算 `signal_i`、`signal_i - 1`、`signal_i - 2` 三个时点各自的每日 Breadth，再取平均。
- 循环内对每个时点 `t` 独立计算 `ma20_t` 和 60 日有效历史计数 `hist_count_t`，无未来函数。
- 保留原有 `EXIT_BREADTH`、`ENTRY_BREADTH` 判断，以及 `_choose_desired`、`_choose_kept` 的组合构建逻辑。

## 研究规则到代码位置对应表
| 研究规则 | 代码位置 |
| :--- | :--- |
| 20/40/60 日动量因子及 0.40/0.35/0.25 权重 | `decide` 中 `for lookback in (20, 40, 60)` 循环及 `scores = components[0] * 0.40 + ...` |
| 上市/历史天数过滤：有效历史不少于 61 天 | `history_count >= max(61, TREND_MA)` |
| 近 6 日平均成交额不少于 5000 万 | `amount_window = amounts[signal_i - 5:signal_i + 1]` 及 `liquid >= MIN_AVG_AMOUNT` |
| 当前收盘价高于 20 日均线 | `closes[signal_i] > trend` |
| TOP_K = 5，最大持仓 5 只 | `TOP_K = 5` |
| 每行业最多 1 只 | `MAX_PER_INDUSTRY = 1` |
| 已持仓保留缓冲区 | `KEEP_BUFFER = 2.0`，`keep_limit = max(TOP_K, int(math.ceil(TOP_K * KEEP_BUFFER)))` |
| Breadth 平滑 3 日 | `BREADTH_SMOOTH_DAYS = 3` 及 `breadths` 循环 |
| 市场宽度阈值 | `EXIT_BREADTH = 0.50`，`ENTRY_BREADTH = 0.58` |
| 执行频率与时间 | `data_spec` 中 `'execution_period': '5m'`、`'execution_time': '09:35'` |
| 日频接口 | `engine_mode = 'daily'` |

## 时点与防未来函数
- `signal_i = len(closes) - 1` 表示 T 日决策日收盘后的历史数据末端。
- 所有日频历史、均线、历史计数和 Breadth 计算均只使用 `<= signal_i` 的切片。
- 组合目标订单由 Harness 在 T+1 日 09:35 执行，信号本身不读取执行日之后的任何数据。
- 平滑循环中 `t = signal_i - offset`，仅使用截止到 `t` 的收盘价，不会引用未来 K 线。

## T+1 与连续持仓
- 修改不涉及撮合、费用、现金、整手或 T+1 约束，这些仍由冻结 Harness/Broker 统一处理。
- 持仓状态通过 `context.selected_held` 承接，不会重置；本轮期初为空仓，由评价区间首日开始承接。
- `_choose_kept` 在震荡区间沿用已持仓且排名仍在缓冲区的股票，实现连续持仓。

## 基础异常与非空处理
- `ready` 要求 `len(context.calendar) >= 75`，历史上不足时不进入决策。
- `decide` 开头若 `signal_i < 60`，直接返回空目标、NaN 得分、`breadth = 0.0`。
- 平滑循环对 `t < 59` 提前中断，避免历史窗口长度不足。
- `breadths` 为空时，`breadth = 0.0`，不会因除零或空列表崩溃。
- `_column_mean` 使用 `np.nansum` 与 `count > 0` 安全处理 NaN；有效样本不足返回 `np.nan`。
- 股票池中 NaN 收盘价、NaN 成交额、非正价格、均线无效等会被 `eligible` 或 `raw` 中的条件过滤为不参与排名。
- 无有效候选时，`ranked` 为空，`SafeStrategyDecision` 返回空目标组合，不因数据错误永久静默空仓以外的错误状态。

## 相对基线的函数级变更清单
1. 文件顶部常量区新增 `BREADTH_SMOOTH_DAYS = 3`。
2. `Strategy` 类新增 `engine_mode = 'daily'`，满足日频/固定时点接口明确声明要求；不影响策略逻辑。
3. `decide` 中原 `breadth_valid` / `breadth` 单日计算块替换为 3 日平滑循环计算。
4. 其余函数 `_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`、`ready`、`create_strategy` 均未修改。
5. 未修改股票池、数据接口、执行频率、行业字段或账户字段读取方式。

## 保留项、未修改模块
- 动量因子 20/40/60 日加权结构保留。
- 股票池过滤、成交额过滤、20 日趋势过滤保留。
- 行业中性约束 `MAX_PER_INDUSTRY = 1` 与持仓数量 `TOP_K = 5` 保留。
- 执行频率 5 分钟、执行时间 09:35 保留。
- Harness 的资金、费用、T+1、整手和成交逻辑均未复制或修改。

## 资源与历史窗口增减
- 无新增数据接口，无新增文件、网络或缓存访问。
- 日线历史窗口需求未超过基线已有请求；基线已使用 `closes[max(0, signal_i - 74):signal_i + 1]`，足以覆盖 3 日平滑与 60 日历史计数。
- 新增计算仅为最多 3 次 20 日均线向量化计算与平均，计算开销可忽略。

## 日志与验证限制
- 未引入 `print` 或日志输出，避免干扰冻结回测；关键安全返回均通过 `SafeStrategyDecision` 显式表达。
- 代码尚未由本人完成冻结本地回测验证；在本地回测结果返回前不声称跑通或预测收益。

# 绩效工作

## 提案：基于行业动量的动态行业集中度控制
状态：仅作为绩效改进提案，独立于原始实现，未经确认不合并入候选运行代码。

### 理由
基线策略强制每个申万一级行业最多持有 1 只股票，即 `MAX_PER_INDUSTRY = 1`。在行业主线明确、仅有 1-2 个行业强势的 A 股环境中，该静态约束可能迫使策略买入非主线行业弱势股，为分散而分散。

### 建议机制
- 每次调仓前计算各申万一级行业的平均动量得分或行业指数动量。
- 若某行业平均动量得分处于全市场前 20%，则将该行业的最大持仓数临时提升为 2。
- 其他行业仍保持最大持仓数为 1。
- 当市场主线分散、没有行业进入前 20% 时，组合仍维持原有分散约束。

### 与原始实现的区分
- 原始实现：静态行业约束 `MAX_PER_INDUSTRY = 1`。
- 提案实现：新增状态依赖机制，根据行业动量环境动态放松强势行业持仓上限，而不改变 TOP_K 总量。

### 风险
- 行业动量本身可能滞后，行业集中度提高后若主线快速反转，会放大回撤。
- 需要明确“行业前 20%”的排序基准、行业动量计算窗口及极端行业数量较少时的处理。
- 动态约束可能增加参数与过拟合风险，需要严格样本外测试。

### 结论
本提案在当前轮次保持与原始实现分开，不替换 `MAX_PER_INDUSTRY = 1` 的既有逻辑。当前提交代码仅实施基本工作，即 Breadth 时间序列平滑。

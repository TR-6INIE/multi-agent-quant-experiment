# 基本工作

## 忠实实现说明
本轮唯一核心机制改进为：在既有 3 日平滑 Breadth 与上一轮非对称入场确认的基础上，增加单日恐慌出场条件。即当 `breadth_1d < PANIC_BREADTH` 时，无论 3 日平滑 Breadth 是否已跌破 `EXIT_BREADTH`，均直接返回空目标持仓。

已按批准方案最小化实施：
- 在常量区新增 `PANIC_BREADTH = 0.40`。
- 原清仓判断：
  `if breadth < EXIT_BREADTH:`
- 修改为：
  `if breadth < EXIT_BREADTH or breadth_1d < PANIC_BREADTH:`
- 其余逻辑，包括 3 日平滑 Breadth 计算、非对称入场条件、动量因子、成交额过滤、行业约束、持仓缓冲、执行频率与时间，均保持不变。

## 研究规则到代码位置对应表
| 研究规则 | 代码位置 |
| :--- | :--- |
| 20/40/60 日动量因子及 0.40/0.35/0.25 权重 | `decide` 中 `for lookback in (20, 40, 60)` 及 `scores = components[0] * 0.40 + components[1] * 0.35 + components[2] * 0.25` |
| 历史天数过滤：有效历史不少于 61 天 | `history_count >= max(61, TREND_MA)` |
| 近 6 日平均成交额不少于 5000 万 | `amount_window = amounts[signal_i - 5:signal_i + 1]` 及 `liquid >= MIN_AVG_AMOUNT` |
| 当前收盘价高于 20 日均线 | `closes[signal_i] > trend` |
| 3 日平滑 Breadth | `BREADTH_SMOOTH_DAYS = 3` 及 `breadths` 循环 |
| 当日单日 Breadth 用作入场确认 | `breadth_1d = breadths[0] if breadths else 0.0` |
| 常规出场阈值：3 日平滑 Breadth < 0.50 | `if breadth < EXIT_BREADTH or breadth_1d < PANIC_BREADTH:` 中的前半部分 |
| 本轮新增恐慌出场阈值：单日 Breadth < 0.40 | `PANIC_BREADTH = 0.40` 及 `if breadth < EXIT_BREADTH or breadth_1d < PANIC_BREADTH:` 中的后半部分 |
| 入场阈值：3 日平滑 Breadth >= 0.50 且单日 Breadth >= 0.58 | `elif breadth >= EXIT_BREADTH and breadth_1d >= ENTRY_BREADTH:` |
| 持仓数量上限 | `TOP_K = 5` |
| 行业中性约束 | `MAX_PER_INDUSTRY = 1` |
| 已持仓股票排名缓冲区 | `KEEP_BUFFER = 2.0`，`_choose_desired` / `_choose_kept` |
| 执行频率与时间 | `data_spec` 中 `'execution_period': '5m'`、`'execution_time': '09:35'` |
| 日频接口 | `engine_mode = 'daily'` |

## 防未来函数与数据时点
- `signal_i = len(closes) - 1` 表示 T 日决策日收盘后的历史数据末端。
- `breadth_1d` 来自 `breadths[0]`，即 `t = signal_i` 的单日 Breadth，仅使用 T 日及 T 日以前收盘价。
- 3 日平滑循环中 `t = signal_i - offset`，所有切片均不超过 `signal_i`。
- 目标持仓订单由 Harness 在 T+1 日 09:35 执行，候选策略不读取执行日之后的数据。

## T+1 与连续持仓
- 候选策略只生成目标持仓，资金、费用、T+1、整手、现金和实际成交均由冻结 Broker 处理。
- 持仓状态通过 `context.selected_held` 承接，不重置；本轮期初已存在承接持仓，由冻结撮合引擎直接恢复，策略不产生虚构的初始化成交。
- `_choose_kept` 在非清仓、非入场状态下沿用符合排名缓冲区条件的已持仓股票，支持连续持仓。

## 边界条件、历史不足与空信号处理
- `ready` 要求 `len(context.calendar) >= 75`，历史上不足时不进入决策。
- `decide` 开头若 `signal_i < 60`，直接返回空目标、NaN 得分、`breadth = 0.0`。
- 平滑循环在 `t < 59` 时提前中断，避免历史窗口过短。
- 若 `breadths` 为空，则 `breadth = 0.0`，`breadth_1d = 0.0`，触发清仓分支，不会因空列表崩溃。
- 若某日无有效股票计算 Breadth，该日 `b = 0.0` 被显式加入列表，不产生除零或静默跳过。
- `_column_mean` 用 `np.nansum` 和 `count > 0` 安全处理 NaN；有效样本不足返回 `np.nan`。
- 无有效候选时 `ranked` 为空，返回的目标组合为空。该空仓是当天信号明确或安全降级的显式决策，不等同于因数据错误永久静默空仓；后续交易日数据恢复后会重新计算。

## 相对基线的函数级变更清单
1. 文件 docstring 更新为同时说明 3 日平滑、非对称入场确认与本轮非对称恐慌出场，但不影响运行逻辑。
2. 在 `BREADTH_SMOOTH_DAYS = 3` 下方新增：
   `PANIC_BREADTH = 0.40`
3. 最终仓位决策块中的清仓判断由：
   `if breadth < EXIT_BREADTH:`
   改为：
   `if breadth < EXIT_BREADTH or breadth_1d < PANIC_BREADTH:`
4. 未修改 `_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`、`ready`、`create_strategy`。
5. 未修改动量因子、成交额过滤、20 日均线过滤、行业约束、执行频率或数据接口。

## 明确未修改模块
- 选股因子、过滤条件、行业中性逻辑、TOP_K、KEEP_BUFFER、TREND_MA 等均未改动。
- 日频接口、数据规格、执行时间和执行频率未改动。
- 未引入绩效提案中的动态排名缓冲区，也未引入任何新增数据接口或文件/网络/缓存访问。

## 资源与历史窗口增减
- 数据请求：无增量。
- 历史窗口：未扩大，仍使用原先所需历史。
- 计算资源：仅新增一次常量比较与逻辑或判断，耗时可忽略。
- 外部接口：无新增文件、网络、缓存或私有属性访问。

## 版本差异说明
- 上一轮版本：已包含非对称入场确认，但清仓只依赖 `breadth_3d < 0.50`。
- 本轮版本：在保留 `breadth_3d < 0.50` 常规清仓的同时，新增 `breadth_1d < 0.40` 单日恐慌清仓。
- 入场判断保持：`breadth_3d >= 0.50` 且 `breadth_1d >= 0.58`。
- 中间状态仍按 `_choose_kept` 处理。

## 尚待本地回测验证的限制
- 本轮代码尚未由本人完成冻结本地回测验证；在本地回测结果返回前，不声称已经跑通或预测收益。
- 本实现仅执行已批准的单一机制改进，未混入绩效工作提案中的动态排名缓冲区逻辑。

# 绩效工作

## 改进提案：基于近期换手状态的动态排名缓冲区（Dynamic Keep Buffer）
状态：仅作为绩效改进提案，独立于本次运行实现，未合并入候选代码。

### 逻辑依据与收益来源
基线策略使用静态 `KEEP_BUFFER = 2.0`，即已持仓股票排名在前 10 才可保留。在本地回测 `trades.csv` 中观察到较多 100-800 股级别的碎单换仓，主要来自排名边缘股票的频繁交替。此类碎单增加了佣金与执行摩擦，但对组合动量暴露提升有限。

参考 Dybvig & Pezzo (2019) 关于交易成本下最优再平衡应设置动态“非交易区”的观点，本提案将静态保留缓冲区改为基于近期实际换手笔数的状态依赖缓冲区：
- 每次调仓前，统计过去 5 个交易日策略的实际换仓笔数（买入 + 卖出）。
- 若过去 5 日换仓笔数 > 10，视为高摩擦状态，临时将 `KEEP_BUFFER` 从 2.0 扩大至 3.0，即已持仓股票排名在前 15 可保留。
- 若过去 5 日换仓笔数 <= 10，保持 `KEEP_BUFFER = 2.0`。
- 总持仓上限 `TOP_K = 5` 与行业约束 `MAX_PER_INDUSTRY = 1` 不变。

### 风险与验证
- 风险：高换手状态下扩大缓冲区可能延缓淘汰排名下滑的弱势股，在趋势反转初期增加回撤。
- 验证指标：当策略处于动态扩大缓冲区状态时，单笔交易平均名义金额应明显高于静态时期；组合最大回撤不应超过基线；若总收益下降超过 2 个百分点，则视为动量衰减代价超过摩擦成本节约，提案应被否决。
- 参数敏感性：建议对换手窗口（3/5/7 个交易日）与触发阈值（8/10/12 笔）做组合消融，确认不是仅单一参数设置有效。
- 可审计材料：可使用冻结 `trades.csv` 中的逐笔成交方向、数量和佣金，联合 `equity.csv` 的收益与回撤，对启用前后进行同口径对比。

### 实施边界
若未来实施，不得新增数据接口或扩大股票池；需显式处理历史交易日不足 5 日、无成交记录、状态触发频率不稳定等情况；任何动态 `KEEP_BUFFER` 都只能使用截至 T 日的已成交记录，不能读取当前目标或执行中的订单状态。

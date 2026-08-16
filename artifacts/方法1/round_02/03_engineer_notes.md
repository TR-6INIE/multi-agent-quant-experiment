# 基本工作

## 忠实实现说明
本轮唯一核心机制改进为：在保留上一轮 3 日平滑 Breadth 的基础上，将入场条件从“3 日平滑 Breadth >= 0.58”改为“3 日平滑 Breadth >= 0.50 且当日单日 Breadth >= 0.58”。

该修改已按批准方案最小化实施：
- 保留原 `breadths` 循环，仍按 `offset = 0, 1, 2` 计算 T 日、T-1 日、T-2 日的单日 Breadth。
- 新增 `breadth_1d = breadths[0] if breadths else 0.0`，即取 T 日单日 Breadth。
- 将原决策块中的 `elif breadth >= ENTRY_BREADTH:` 替换为：
  `elif breadth >= EXIT_BREADTH and breadth_1d >= ENTRY_BREADTH:`。
- 其余逻辑保持不动。

## 研究规则到代码位置对应表
| 研究规则 | 代码位置 |
| :--- | :--- |
| 20/40/60 日动量因子及 0.40/0.35/0.25 权重 | `decide` 中 `for lookback in (20, 40, 60)` 及 `scores = components[0] * 0.40 + components[1] * 0.35 + components[2] * 0.25` |
| 历史天数过滤：有效历史不少于 61 天 | `history_count >= max(61, TREND_MA)` |
| 近 6 日平均成交额不少于 5000 万 | `amount_window = amounts[signal_i - 5:signal_i + 1]` 及 `liquid >= MIN_AVG_AMOUNT` |
| 当前收盘价高于 20 日均线 | `closes[signal_i] > trend` |
| 3 日平滑 Breadth | `BREADTH_SMOOTH_DAYS = 3` 及 `breadths` 循环 |
| 当日单日 Breadth 用作入场确认 | `breadth_1d = breadths[0] if breadths else 0.0` |
| 出场阈值：3 日平滑 Breadth < 0.50 | `if breadth < EXIT_BREADTH:` |
| 入场阈值：3 日平滑 Breadth >= 0.50 且单日 Breadth >= 0.58 | `elif breadth >= EXIT_BREADTH and breadth_1d >= ENTRY_BREADTH:` |
| 持仓数量上限 | `TOP_K = 5` |
| 行业中性约束 | `MAX_PER_INDUSTRY = 1` |
| 已持仓股票排名缓冲区 | `KEEP_BUFFER = 2.0`，`_choose_desired` / `_choose_kept` |
| 执行频率与时间 | `data_spec` 中 `'execution_period': '5m'`、`'execution_time': '09:35'` |
| 日频接口 | `engine_mode = 'daily'` |

## 防未来函数与数据时点
- `signal_i = len(closes) - 1` 表示 T 日决策日收盘后的历史数据末端。
- `breadth_1d` 来自 `breadths[0]`，即 `t = signal_i` 的当日单日 Breadth，仅使用 T 日及 T 日以前收盘价。
- 3 日平滑循环中 `t = signal_i - offset`，所有切片均不超过 `signal_i`。
- 组合目标订单由 Harness 在 T+1 日 09:35 执行，候选策略不读取执行日之后的数据。

## T+1 与连续持仓
- 候选策略只生成目标持仓，资金、费用、T+1、整手、现金和实际成交均由冻结 Broker 处理。
- 持仓状态通过 `context.selected_held` 承接，不重置；本轮期初持仓为空，直接由评价区间首日开始承接。
- `_choose_kept` 在“中期未破位但当日不强势”的保持区沿用符合排名缓冲区条件的已持仓股票，支持连续持仓。

## 边界条件、历史不足与空信号处理
- `ready` 要求 `len(context.calendar) >= 75`，历史上不足时不进入决策。
- `decide` 开头若 `signal_i < 60`，直接返回空目标、NaN 得分、`breadth = 0.0`。
- 平滑循环在 `t < 59` 时提前中断，避免历史窗口过短。
- 若 `breadths` 为空，则 `breadth = 0.0`，`breadth_1d = 0.0`，会触发清仓分支，不会因空列表崩溃。
- 若某日无有效股票计算 Breadth，该日 `b = 0.0` 被显式加入列表，不产生除零或静默跳过。
- `_column_mean` 用 `np.nansum` 和 `count > 0` 安全处理 NaN；有效样本不足返回 `np.nan`。
- 无有效候选时 `ranked` 为空，返回的目标组合为空，不等同于因数据错误永久静默空仓。

## 相对基线的函数级变更清单
1. 文件 docstring 更新为同时说明 3 日平滑和本轮非对称入场确认，但不影响运行逻辑。
2. `decide` 中新增一行：
   `breadth_1d = breadths[0] if breadths else 0.0`。
3. `decide` 中最终仓位决策块由原：
   `elif breadth >= ENTRY_BREADTH:`
   改为：
   `elif breadth >= EXIT_BREADTH and breadth_1d >= ENTRY_BREADTH:`。
4. 未修改 `_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`、`ready`、`create_strategy`。
5. 未修改动量因子、成交额过滤、20 日均线过滤、行业约束、执行频率或数据接口。

## 可从回测输出直接复核的预期行为
- `equity.csv` 的 `breadth` 列和 `trades.csv` 的 `breadth` 列仍为 3 日平滑 Breadth，不是 `breadth_1d`。
- 在 3 日平滑 Breadth 位于 `[0.50, 0.58)` 的日期，当前代码可能因当日单日 Breadth >= 0.58 而将 `target_count` 从 0 提升至 5；这是本轮非对称入场的直接可复核特征。
- 若 3 日平滑 Breadth >= 0.50 但当日单日 Breadth < 0.58，则 `target_count` 保持原持仓数量或按 `_choose_kept` 缩小，而不是主动建新仓。
- 因子得分、行业约束和持仓缓冲规则与基线一致，不应出现未批准的结构性变化。

## 资源与历史窗口增减
- 数据请求：无增量。
- 历史窗口：未扩大，仍使用 `closes[max(0, signal_i - 74):signal_i + 1]` 及原先所需历史。
- 计算资源：仅新增一次列表索引读取和一次条件判断，耗时可忽略。
- 外部接口：无新增文件、网络、缓存或私有属性访问。

## 版本差异说明
- 上一轮版本：`breadth_3d >= 0.58` 才入场。
- 本轮版本：`breadth_3d >= 0.50` 且 `breadth_1d >= 0.58` 才入场。
- 出场逻辑保持不变：`breadth_3d < 0.50` 清仓。
- 中间状态逻辑从“仅按 `_choose_kept` 保持”扩展为“所有非清仓、非入场情况均按 `_choose_kept` 保持”，与批准方案一致。

## 尚待本地回测验证的限制
- 本轮代码尚未由本人完成冻结本地回测验证；在本地回测结果返回前，不声称已经跑通或预测收益。
- 本实现仅执行已批准的单一机制改进，未混入绩效工作提案中的动态行业集中度逻辑。

# 绩效工作

## 改进提案：基于行业动量的动态行业集中度控制
状态：仅作为绩效改进提案，独立于本次运行实现，未合并入候选代码。

### 逻辑依据与收益来源
基线策略强制 `MAX_PER_INDUSTRY = 1`，在行业主线集中的 A 股环境中可能“为了分散而分散”，买入非主线行业弱势股。提案引入行业动量状态：
- 每次调仓前，计算每个申万一级行业内 eligible 股票的平均动量得分。
- 计算全市场行业平均动量得分的 80 分位数作为强势行业阈值。
- 若某行业平均动量得分 >= 该阈值，且该行业内 eligible 股票数量 >= 3，则允许该行业临时 `MAX_PER_INDUSTRY = 2`。
- 其他行业仍为 `MAX_PER_INDUSTRY = 1`，组合总持仓上限 `TOP_K = 5` 不变。

预期收益来源是：在行业主线明确时将资金相对集中于强势行业，降低静态分散对弱势行业的强制配置。

### 风险
- 行业动量本身可能滞后，若主线快速反转，集中度提升会放大回撤。
- 阈值和“行业内 eligible 股票数量 >= 3”的设定存在参数敏感性和过拟合风险。
- 行业分类使用固定快照，本身带有幸存者偏差与分类前视偏差，可能高估动态行业约束的历史效果。

### 若未来实施时的边界条件与资源预算
- 不新增数据接口，不扩大股票池。
- 仅使用现有 `scores`、`industries` 和 `eligible` 计算行业平均分与排序。
- 计算复杂度约为 O(股票数 + 行业数 log 行业数)，在日频调仓中可忽略。
- 需要显式处理行业数量过少、无行业进入阈值、行业内 eligible 股票不足 3 只等退化为原静态约束的情况。
- 停止条件可采用：若年化换手率 > 800% 或最大回撤 > 25%，回退至静态 `MAX_PER_INDUSTRY = 1`。

### 与当前运行实现的区分
当前冻结代码仍采用静态行业约束，未引入动态行业集中度机制。绩效提案不影响本轮非对称入场确认的基本工作实现。

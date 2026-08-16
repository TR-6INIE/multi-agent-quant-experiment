# 基本工作

## 忠实实现说明
本轮唯一核心机制改进为批准方案中的候选A：将入场确认条件从绝对阈值
`breadth_1d >= 0.58` 改为相对边际改善条件 `breadth_1d >= breadth`，
即单日市场宽度不低于最近3日平滑市场宽度时，确认市场宽度边际企稳或改善。

已按批准方案最小化实施：

1. 删除常量区中的 `ENTRY_BREADTH = 0.58`。
2. 将最终仓位决策块中的：
   ```python
   elif breadth >= EXIT_BREADTH and breadth_1d >= ENTRY_BREADTH:
   ```
   改为：
   ```python
   elif breadth >= EXIT_BREADTH and breadth_1d >= breadth:
   ```
3. 其余逻辑保持不变。
4. 绩效工作提案未合并入运行实现。

## 研究规则到代码位置对应表

| 研究规则 | 代码位置 |
| :--- | :--- |
| 20/40/60日动量因子及0.40/0.35/0.25权重 | `decide` 中 `for lookback in (20, 40, 60)` 及 `scores = components[0] * 0.40 + components[1] * 0.35 + components[2] * 0.25` |
| 历史有效性过滤：有效历史至少61日 | `history_count >= max(61, TREND_MA)` |
| 近6日平均成交额不低于5000万 | `amount_window = amounts[signal_i - 5:signal_i + 1]`，`liquid >= MIN_AVG_AMOUNT` |
| 当前收盘价高于20日均线 | `closes[signal_i] > trend` |
| 3日平滑Breadth | `BREADTH_SMOOTH_DAYS = 3`，`breadths` 循环，`breadth = float(np.mean(breadths))` |
| 单日Breadth | `breadth_1d = breadths[0] if breadths else 0.0` |
| 常规出场：3日平滑Breadth < 0.50 | `if breadth < EXIT_BREADTH or breadth_1d < PANIC_BREADTH:` 的前半部分 |
| 恐慌出场：单日Breadth < 0.40 | `PANIC_BREADTH = 0.40`，`if breadth < EXIT_BREADTH or breadth_1d < PANIC_BREADTH:` 的后半部分 |
| 本轮边际改善入场确认 | `elif breadth >= EXIT_BREADTH and breadth_1d >= breadth:` |
| 持仓数量上限 | `TOP_K = 5` |
| 行业中性约束 | `MAX_PER_INDUSTRY = 1` |
| 已持仓股票排名缓冲 | `KEEP_BUFFER = 2.0`，`_choose_desired` / `_choose_kept` |
| 执行频率与时间 | `data_spec` 中 `'execution_period': '5m'`、`'execution_time': '09:35'` |
| 日频接口 | `engine_mode = 'daily'` |

## 数据到下单全链路审查
- `create_strategy()` 返回 `Strategy` 实例。
- 日频模式下，Harness 在每个交易日调用 `ready(context)` 后调用 `decide(context)`。
- `context.closes/amounts/codes/industries/selected_held` 均为只读快照，历史截至上一交易日。
- `decide` 只返回 `SafeStrategyDecision`，不自行下单、撮合、计费、读文件、读网络或访问完整缓存。
- 目标持仓由冻结 Harness/Broker 在 T+1 日 `09:35` 执行。
- 候选代码不读取执行价、不处理资金、整手、费用、T+1、成交或现金。

## 候选代码与冻结Broker职责逐项核对表

| 职责 | 候选代码 | 冻结Broker/Harness |
| :--- | :--- | :--- |
| 信号计算 | 是 | 否 |
| 返回目标持仓 | 是 | 否 |
| 订单生成与路由 | 否 | 是 |
| 执行价确定 | 否 | 是 |
| 资金现金约束 | 否 | 是 |
| 整手处理 | 否 | 是 |
| 佣金/印花税/滑点 | 否 | 是 |
| T+1可交易校验 | 否 | 是 |
| 实际持仓更新 | 否 | 是 |
| 回测撮合 | 否 | 是 |
| 文件/网络/缓存访问 | 否 | 否 |

## 状态污染、复杂度和参数自由度
- 本轮未新增任何实例状态或跨日可变状态。
- `breadth`、`breadth_1d`、`ranked`、`desired` 均为 `decide` 内局部变量，不保存到 `self`。
- 删除 `ENTRY_BREADTH` 后，固定参数减少1个；相对阈值直接复用已计算的 `breadth`，未新增数据请求或自由参数。
- `KEEP_BUFFER`、`TOP_K`、`MAX_PER_INDUSTRY`、`TREND_MA` 等仍保持原值，复杂度未增加。

## 安全降级与敏感性配置
- 当 `breadths` 为空时：`breadth = 0.0`、`breadth_1d = 0.0`，触发清仓分支，不崩溃。
- 当任一Breadth计算窗口内无有效股票时，该日 `b = 0.0` 被显式加入列表，不静默跳过后续判断。
- 当无有效候选时 `ranked` 为空，`_choose_desired` 或 `_choose_kept` 返回空目标；这是信号或安全降级的显式结果，不等同于数据错误导致的永久静默空仓。
- 本轮相对入场阈值的敏感性可审计配置建议：后续可对 `breadth_1d >= breadth - 0.02`、`breadth_1d >= breadth`、`breadth_1d >= breadth + 0.02` 做消融。
- 已批准的停止条件仍可执行：若评价期内成交笔数 > 150 笔，或最大回撤 > 15%，则应回退至绝对阈值或引入冷却期。

## 防未来函数
- `signal_i = len(closes) - 1` 表示 T 日决策时的历史数据末端。
- `breadth` 和 `breadth_1d` 均只使用截至 T 日及以前的收盘价。
- 平滑循环中的最大索引为 `signal_i`，未使用评价区间或未来数组。
- 目标订单由 Harness 在 T+1 日 `09:35` 执行，候选策略不读取执行日之后数据。

## T+1与连续持仓
- 候选策略只返回目标股票集合，不直接处理 T+1 买卖限制。
- 实际持仓通过 `context.selected_held` 承接。
- 在 `breadth >= EXIT_BREADTH` 但相对入场确认不满足时，使用 `_choose_kept` 保持符合排名缓冲条件的已持仓股票，支持跨交易日连续持仓。
- 本轮初始持仓由实验规则直接承接，代码不生成虚构初始化成交。

## 边界条件、历史不足与空信号处理
- `ready` 要求 `len(context.calendar) >= 75`，历史不足时不进入决策。
- `decide` 开头若 `signal_i < 60`，显式返回空目标、NaN得分和 `breadth = 0.0`。
- `_column_mean` 使用 `np.isfinite`、`np.nansum` 和 `count > 0` 安全处理 NaN；有效样本不足时返回 `np.nan`。
- 停牌价、成交额或均线存在 NaN 时，通过 `eligible` 过滤，不永久污染状态。
- 空信号或全市场无有效候选时返回空目标；下个交易日数据恢复后可重新计算。

## 相对基线的函数级变更清单
1. 模块 docstring 更新为“相对边际改善入场确认”，不影响运行逻辑。
2. 删除常量 `ENTRY_BREADTH = 0.58`。
3. 最终仓位决策块中的入场判断由：
   ```python
   elif breadth >= EXIT_BREADTH and breadth_1d >= ENTRY_BREADTH:
   ```
   改为：
   ```python
   elif breadth >= EXIT_BREADTH and breadth_1d >= breadth:
   ```
4. 未修改 `_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`、`ready`、`create_strategy`。
5. 未修改动量因子、成交额过滤、20日均线过滤、行业约束、持仓数、KEEP_BUFFER、执行频率或数据接口。

## 明确未修改模块
- 选股因子、过滤条件、行业中性逻辑、`TOP_K`、`KEEP_BUFFER`、`TREND_MA` 等均未改动。
- 日频接口、数据规格、执行时间和执行频率未改动。
- 未引入绩效提案中的动态排名缓冲区。
- 未引入任何新增文件、网络、缓存、QMT API 或私有属性访问。
- 未扩大股票池，未提高数据频率。

## 资源与历史窗口增减
- 数据请求：无增量。
- 历史窗口：未扩大，仍使用原先所需历史。
- 计算资源：将常量比较改为变量比较，耗时 < 0.01ms，可忽略。
- 外部接口：无新增。

## 完整版本清单
| 项 | 值 |
| :--- | :--- |
| `strategy.name` | `broad_industry_neutral_momentum_local_v1` |
| `engine_mode` | `daily` |
| 信号周期 | `1d` |
| 执行周期 | `5m` |
| 执行时间 | `09:35` |
| 候选接口 | `ready/decide` |
| 本轮核心改动 | 删除 `ENTRY_BREADTH`；入场改为 `breadth_1d >= breadth` |
| 保留参数 | `TOP_K=5`, `KEEP_BUFFER=2.0`, `MAX_PER_INDUSTRY=1`, `TREND_MA=20`, `EXIT_BREADTH=0.50`, `MIN_AVG_AMOUNT=50_000_000.0`, `BREADTH_SMOOTH_DAYS=3`, `PANIC_BREADTH=0.40` |
| 候选SHA256 | 待冻结本地回测生成；结果返回前不预填、不声称通过 |

## 尚待本地回测验证的限制
- 本轮代码尚未由本人完成冻结本地回测验证。
- 在冻结本地回测结果返回前，不声称已跑通，不预测收益或回撤。
- 本实现仅执行批准的基本工作单点改动，绩效提案仅记录于下方章节，未并入候选代码。

# 绩效工作

## 改进提案：基于市场波动率期限结构的动态排名缓冲区
状态：仅作为绩效改进提案，独立于本次运行实现，未合并入候选代码。

### 逻辑依据
基线策略使用静态 `KEEP_BUFFER = 2.0`，即已持仓股票排名在前10可保留。在存在交易成本的市场中，最优再平衡的非交易区宽度通常与市场噪声或波动率正相关。当市场短期波动显著放大时，排名边缘股票的得分差异更可能来自噪声，扩大保留缓冲区有助于减少无意义的碎单换仓。

### 接口可行性证明
该提案只使用 `SafeStrategyContext` 中已有字段，不需要新增接口：

| 所需变量 | 可获取字段 |
| :--- | :--- |
| 全市场有效股票集合 | `context.closes`、`context.amounts`、`context.codes`，结合现有 `eligible` 条件 |
| 有效股票的日收益率矩阵 | 由 `context.closes` 沿时间轴计算 |
| 市场短期截面波动率 | `context.closes` 过去5个交易日收益率的截面平均标准差 |
| 市场长期截面波动率 | `context.closes` 过去20个交易日收益率的截面平均标准差 |
| 当前已持仓股票 | `context.selected_held` |
| 行业约束 | `context.industries` |

不读取账户历史成交、不读取文件、不读取网络、不读取完整缓存，因此不存在上一轮方案中“状态变量不可获得”的实现阻断。

### 参数配置、回退路径与安全降级
建议参数集中配置：

| 参数 | 建议值 | 含义 |
| :--- | :--- | :--- |
| `VOL_SHORT_WINDOW` | 5 | 短期波动率回看交易日数 |
| `VOL_LONG_WINDOW` | 20 | 长期波动率回看交易日数 |
| `VOL_SPIKE_RATIO` | 1.2 | 判定短期波动显著放大的比率 |
| `BASE_KEEP_BUFFER` | 2.0 | 常规保留缓冲区 |
| `HIGH_VOL_KEEP_BUFFER` | 3.0 | 高波动状态下的保留缓冲区 |
| `MIN_VOL_UNIVERSE` | 10 | 计算市场波动率所需最小有效股票数 |

回退路径：
- 若有效股票数少于 `MIN_VOL_UNIVERSE`，退回 `BASE_KEEP_BUFFER = 2.0`。
- 若短期或长期波动率为 `NaN` 或非正，退回 `BASE_KEEP_BUFFER = 2.0`。
- 历史窗口不足5日时，不启用动态缓冲区。
- 动态逻辑只影响 `KEEP_BUFFER`，不改变 `TOP_K` 和 `MAX_PER_INDUSTRY`。

### 可审计消融与验证材料
- 使用冻结 `trades.csv` 统计动态缓冲状态与静态缓冲状态下的平均单笔名义金额和成交笔数。
- 使用冻结 `equity.csv` 比较启用前后最大回撤与总收益。
- 消融组合建议：短期窗口取 3/5/10，长期窗口取 15/20/25，比率取 1.1/1.2/1.35。
- 否决条件：若动态缓冲下总收益较静态基线下降超过2个百分点，或最大回撤明显扩大，则视为动量衰减代价超过摩擦成本节约。

### 与运行实现边界
该绩效提案未在本轮 `STRATEGY_CODE` 中实现。如未来获批准实施，仍需保持最小差异修改，显式处理历史不足、NaN 和有效股票数不足，并且不得引入任何新增数据接口。

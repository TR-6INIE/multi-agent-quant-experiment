忠实实现说明：

- 本轮批准的唯一核心机制改进为“优化非对称市场宽度出场条件（移除均线出场分支）”。
- 已按批准方案在 `Strategy.decide` 中仅修改机制转换判断逻辑：
  原代码：
  `is_bear_regime = (breadth < EXIT_BREADTH) or (breadth < breadth_ma)`
  修改为：
  `is_bear_regime = (breadth < EXIT_BREADTH)`
- 为满足本地策略接口模板对接口选择的明确要求，补充了类属性 `engine_mode = 'daily'`。该属性仅声明策略使用日频固定时点接口，不改变信号计算、执行时点或任何交易逻辑，不属于本轮核心机制改进。
- 其他所有代码均保持本轮基线不变。

研究规则到代码位置对应：

- `EXIT_BREADTH = 0.50` 保留在全局变量区，仍作为唯一空头出场绝对阈值。
- `ENTRY_BREADTH = 0.58` 与 `breadth_ma` 均线确认逻辑完全保留，仍用于多头建仓。
- `breadth_ma` 的历史循环计算逻辑完全保留，因为进入机制仍需要均线确认。
- `is_bull_regime = (breadth >= ENTRY_BREADTH) and (breadth > breadth_ma)` 保持不变。
- 过渡状态 `_choose_kept` 与多头状态 `_choose_desired` 保持不变。
- `KEEP_BUFFER = 3.0`、行业中性、动量因子、基础过滤等均未修改。

防未来函数：

- 本轮仅删除一个布尔 OR 分支，未新增任何时间序列切片或数据访问。
- `signal_i = len(closes) - 1`，仍只使用截至上一交易日的日线历史。
- 历史 breadth 循环索引仍严格限制在 `[start_idx, signal_i]`。
- `ma20_i` 窗口仍为 `[i - 19, i]`，完全基于历史收盘价。
- 所有截面排名 `_percentile` 仅使用 `signal_i` 当天的有效数据。
- 未读取 `signal_i` 之后的任何数组，未引入评价区间或未来数据。

T+1 与连续持仓：

- 策略仅返回 `SafeStrategyDecision` 目标组合，不自行撮合、计费、读文件、读网络或访问完整缓存。
- 资金、费用、佣金、印花税、整手、T+1、成交和持仓延续由冻结 Broker 统一处理。
- 本轮需承接期初资产 737,984.37 元，期初持仓为空；策略无自行初始化或重置逻辑。
- 本次修改不引入策略内部状态变量，不会影响持仓延续。

边界处理：

- `ready()` 继续保持 `len(context.calendar) >= 75` 的可用性检查。
- `signal_i < 60` 时仍提前返回空目标，避免历史不足。
- 若历史 breadth 值全为 NaN，`breadth_ma` 仍回退为当日 `breadth`，不会静默导致永久空仓或死锁。
- 空信号、NaN、无效候选继续由原 `eligible`、`_choose_desired`、`_choose_kept` 逻辑处理；`ranked` 为空时安全返回空元组。
- 移除均线出场分支后，只有 `breadth < 0.50` 才会清仓，因此空头条件更简单、更不敏感；边界行为仍明确。

相对基线的函数级变更清单：

- 类 `Strategy`：
  - 新增接口声明属性：`engine_mode = 'daily'`。
- 方法 `Strategy.decide`：
  - 仅修改一行：
    `is_bear_regime = (breadth < EXIT_BREADTH) or (breadth < breadth_ma)` ->
    `is_bear_regime = (breadth < EXIT_BREADTH)`。
- 未修改函数：
  `_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`、`Strategy.ready`、`create_strategy`。
- 未修改类属性：`name`、`cooldown_days`、`data_spec`。
- 未修改全局常量：`TOP_K`、`KEEP_BUFFER`、`MAX_PER_INDUSTRY`、`TREND_MA`、`EXIT_BREADTH`、`ENTRY_BREADTH`、`MIN_AVG_AMOUNT`、`BREADTH_MA_WINDOW`。
- 未修改动量因子、行业中性、基础过滤、成交额过滤、历史 breadth 计算、最终持仓决策返回结构。

明确未修改模块：

- 未修改股票池、行业分类快照、数据频率和执行时点。
- 未修改资金、费用、整手、T+1、成交和持仓逻辑。
- 未引入新数据接口，未读取文件、网络、完整缓存，未使用 QMT API。
- 未扩大股票池，未提高数据频率。
- 未增加安全策略上下文以外的数据访问。

资源与历史窗口增减：

- 计算资源：无增量；仅移除一个布尔比较操作，`breadth_ma` 的历史循环因进场确认需要仍保留。
- 内存资源：无增量。
- 历史窗口：未新增或扩大任何历史数据窗口，未增加新的历史切片。

尚待本地回测验证的限制：

- 移除均线出场分支属于基于研究方案的外推改进，尚未经本轮冻结本地回测验证；在结果返回前不声称已跑通或猜测收益。
- 若市场出现缓慢阴跌，breadth 从高位缓慢降至 0.50 以下，本次修改可能使策略清仓时点晚于原基线，存在回撤放大的风险。
- 该改动是否能在不显著增加回撤的情况下改善踏空问题，需以冻结 Broker 的本地回测结果为准。
- 批准方案已设定停止条件：若最大回撤超过 -8.0%，或出现单次清仓时组合已从高点回撤超过 -10%，下一轮应重新引入带缓冲的均线出场条件或调整绝对出场阈值。

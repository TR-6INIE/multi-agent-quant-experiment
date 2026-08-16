忠实实现说明：
- 本轮唯一核心机制改进为“非对称市场宽度机制转换”，已按批准方案在 `Strategy.decide` 中实现。
- 仅修改空头机制判断逻辑：将 `is_bear_regime = (breadth < EXIT_BREADTH) and (breadth < breadth_ma)` 改为 `is_bear_regime = (breadth < EXIT_BREADTH) or (breadth < breadth_ma)`。
- 多头机制保持双确认：`is_bull_regime = (breadth >= ENTRY_BREADTH) and (breadth > breadth_ma)` 未变。
- 过渡机制保持 `_choose_kept(ranked, held, industries)` 未变。
- 未修改 `ready`、`data_spec`、`create_strategy`、动量因子、行业中性、基础过滤、成交额过滤、股票池、数据频率、执行时点。

研究规则到代码位置对应：
- 历史宽度计算仍位于 `decide` 中因子排名计算之后、最终持仓决策之前，未引入未来数据。
- `is_bear_regime` 位于 `breadth_ma` 计算完成后，仅改变布尔运算符，使用截至 `signal_i` 的已有历史宽度。
- `EXIT_BREADTH = 0.50`、`ENTRY_BREADTH = 0.58`、`BREADTH_MA_WINDOW = 10` 均继承基线/上一轮批准的参数。
- 清仓后空仓状态延续至后续交易日，由冻结 Broker 持有现金，策略仅返回目标持仓，不自行管理资金、费用、整手、T+1、成交和持仓。

防未来函数：
- `signal_i = len(closes) - 1`，即只使用上一交易日收盘后的日线历史。
- 历史 `breadth_values` 循环索引 `i` 严格限制在 `[start_idx, signal_i]`，不触碰 `signal_i` 之后的任何数据。
- 每个历史日的均线窗口 `closes[i - 19:i + 1]` 和有效性历史窗口 `closes[max(0, i - 74):i + 1]` 均只使用截至该历史日的数据。
- 当日市场宽度 `breadth` 仅使用 `closes[signal_i]` 与截至 `signal_i` 的历史均线；`breadth_ma` 由该历史宽度序列忽略 NaN 后得到。
- 本轮修改仅替换 `and` 为 `or`，未新增数据切片、未扩大数据接口、未读取未来数组、未读文件、网络、完整缓存或 QMT API。

T+1 与连续持仓：
- 策略只返回 `SafeStrategyDecision` 目标组合，实际资金、费用、佣金、印花税、整手、T+1、成交和持仓延续由冻结 Broker 统一处理。
- 本轮承接期初资产 548146.60 元、期初持仓为空；策略无提前初始化逻辑，目标持仓只来自当日信号。
- 空头机制返回 `desired=tuple()`，由 Broker 将实际持仓清零；历史上若继续保持空仓，也由 Broker 承接现金状态。

边界处理：
- `ready()` 保证 `len(context.calendar) >= 75`，因此正常运行时 `signal_i >= 74`，历史宽度窗口足够。
- 防御性保护：`signal_i < 60` 时提前返回空目标，避免历史不足。
- 若 `breadth_values` 全为 NaN，则 `breadth_ma` 回退为当日 `breadth`；此时 `breadth < breadth_ma` 与 `breadth > breadth_ma` 均不成立，空头与多头机制均不触发，进入保守保持路径，不会因数据缺失导致整轮静默零交易。
- 空信号、NaN、无效候选继续由原 `eligible`、`_choose_desired`、`_choose_kept` 逻辑处理；`ranked` 为空时安全返回空元组。

相对基线的函数级变更清单：
- 修改 `Strategy.decide`：
  - 将 `is_bear_regime` 的布尔运算符由 `and` 改为 `or`。
- 未修改：`_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`、`ready`、`data_spec`、`create_strategy`。
- 未修改：动量因子构造、行业中性约束、历史不足和 NaN 处理、成交额过滤、市场宽度计算、历史宽度平滑逻辑、最终决策返回结构。

明确未修改模块：
- 未修改股票池、行业分类快照、数据频率、执行时点。
- 未修改资金、费用、整手、T+1、成交和持仓逻辑。
- 未引入新数据接口、未读取文件/网络/完整缓存、未使用 QMT API。
- 未扩大股票池，未提高数据频率。

资源与历史窗口增减：
- 计算资源：无增量，仅将布尔运算 `and` 替换为 `or`。
- 内存资源：无增量。
- 历史窗口：沿用基线/上一轮已实现的 10 日历史宽度计算窗口，本轮未增加新的历史窗口或数据切片。

尚待本地回测验证的限制：
- 该非对称机制属于对 A 股市场宽度过滤的外推改进，尚未经本轮冻结本地回测验证；在结果返回前不声称已跑通或猜测收益。
- 若评价期内市场处于宽幅震荡，空头单条件可能比对称双条件更敏感，存在频繁清仓后再建仓的锯齿风险。
- 若市场急跌后迅速 V 型反转，可能出现清仓后踏空反弹，需以冻结 Broker 的本地回测结果为准。

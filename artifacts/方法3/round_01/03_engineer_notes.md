忠实实现说明：
- 本轮唯一核心机制改进为“市场宽度趋势平滑”，已按批准方案在 `Strategy.decide` 中实现。
- 新增常量 `BREADTH_MA_WINDOW = 10`，保留原基线常量。
- 新增历史宽度计算循环：对 `[start_idx, signal_i]` 每个历史交易日，使用 `closes[i-19:i+1]` 计算20日均线，并基于截至该日的历史非空数、当日收盘价和均线有效性计算当日市场宽度。
- 对10日宽度序列做忽略 NaN 的均值，得到 `breadth_ma`；若全部为 NaN，则按方案回退到当日 `breadth`。
- 新机制判断完全替代原三分支：
  - 空头：`breadth < EXIT_BREADTH` 且 `breadth < breadth_ma` -> 清仓。
  - 多头：`breadth >= ENTRY_BREADTH` 且 `breadth > breadth_ma` -> 建仓。
  - 否则：保持现有持仓。
- 返回的 `breadth` 仍为当日市场宽度，未改变 `SafeStrategyDecision` 外部契约。

研究规则到代码位置对应：
- 历史宽度循环位于 `decide` 中 `breadth` 计算之后、`amount_window` 之前？实际代码中位于因子排名计算之后、最终决策之前；该顺序不改变原因子计算，且不依赖未来数据。
- `EXIT_BREADTH`、`ENTRY_BREADTH` 继承基线参数；`BREADTH_MA_WINDOW=10` 来自方案参数来源。

防未来函数：
- 历史宽度计算索引 `i` 严格限制在 `signal_i` 及之前。
- 每个 `i` 的均线窗口为 `closes[i-19:i+1]`，完全使用截至该日的历史数据。
- 每个 `i` 的历史有效数窗口为 `closes[max(0, i-74):i+1]`，不包含 `i` 之后的任何数据。
- 全部截面排名仍只使用 `signal_i` 当天的有效数据，未扩大数据接口。
- 未读取下一交易日或评价期未来数组。

T+1与连续持仓：
- 策略日频只负责返回目标持仓，实际资金、费用、T+1、整手、成交和持仓延续由冻结 Broker 处理。
- 本轮承接期初持仓为空，策略无提前初始化逻辑；目标持仓仅来自当日信号。

边界处理：
- `ready()` 保证 `len(context.calendar) >= 75`，因此正常运行时 `signal_i >= 74`，历史窗口 `start_idx` 至少65，足够计算10日宽度。
- 为防御异常短历史，`start_idx = max(19, signal_i - BREADTH_MA_WINDOW + 1)`；若循环无法产生有效历史宽度，则 `breadth_ma` 回退到当日 `breadth`，此时双条件均不会触发切换，体现保守保持。
- 空信号、NaN、无有效候选均由原 `eligible` 与 `_choose_*` 逻辑处理；`ranked` 为空时 `_choose_desired`/`_choose_kept` 安全返回空元组。
- 历史宽度计算中，若某日无有效股票则返回 NaN，均值阶段自动忽略，不会让数据错误静默变成永久空仓。

相对基线的函数级变更清单：
- 新增常量：`BREADTH_MA_WINDOW = 10`。
- 修改 `Strategy.decide`：
  - 在因子排名计算之后、最终持仓决策之前新增历史宽度均值计算与双条件机制判断。
  - 移除原 `if breadth < EXIT_BREADTH / elif breadth >= ENTRY_BREADTH / else` 三分支。
  - 新增 `is_bear_regime` / `is_bull_regime` 双条件。
- 未修改：`_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`、`ready`、`data_spec`、`create_strategy`、动量因子、行业中性、基础过滤、成交额过滤、股票池、数据频率。

明确未修改模块：
- 未修改股票池、行业分类快照、数据频率、执行时点。
- 未修改资金、费用、整手、T+1、成交和持仓逻辑。
- 未引入新数据接口、未读取文件/网络/完整缓存、未使用 QMT API。
- 未扩大股票池，未提高数据频率。

资源与历史窗口增减：
- 历史窗口增量：新增最多10个历史交易日的 `closes[i-19:i+1]` 和 `closes[max(0,i-74):i+1]` 切片计算，均为内存中已有数组，无文件/网络访问。
- 时间复杂度：约 O(10 × N × (20 + 75))，在股票池规模有限时增量可忽略。
- 内存增量：新增长度为最多10的 `breadth_values` 列表，无显著压力。

尚待本地回测验证的限制：
- 该改进属于外推自宏观机制转换思想，未经冻结本地回测验证；在结果返回前不声称已跑通或猜测收益。
- 若评价期内出现周期短于10个交易日的 V 型反转，该平滑机制可能滞后，导致快速反弹时踏空或快速下跌时未及时清仓。
- 若 market breadth 在阈值附近频繁穿越其10日均线，仍可能产生一定切换磨损。
- 实际效果需以冻结 Broker 的本地回测为准，包括整手、费用、T+1 和成交实现。

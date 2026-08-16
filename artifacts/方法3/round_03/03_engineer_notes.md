忠实实现说明：
- 本轮唯一核心机制改进为“扩大截面排名换手缓冲带（Expand Rank Keep-Buffer）”，已按批准方案在全局变量定义区实施。
- 仅修改 `KEEP_BUFFER`：从 `2.0` 改为 `3.0`。
- 未修改 `TOP_K = 5`，因此 `_choose_desired` 与 `_choose_kept` 中的 `keep_limit` 将自动计算为 `max(5, ceil(5 * 3.0)) = 15`。
- 未修改 `ready`、`data_spec`、`create_strategy`、动量因子构造、行业中性约束、基础过滤、成交额过滤、非对称市场宽度风控、历史 breadth 计算逻辑或最终决策返回结构。

研究规则到代码位置对应：
- 全局常量 `KEEP_BUFFER` 位于策略模块顶部变量定义区。
- `_choose_desired(ranked, held, industries)` 和 `_choose_kept(ranked, held, industries)` 均内部计算：
  `keep_limit = max(TOP_K, int(math.ceil(TOP_K * KEEP_BUFFER)))`
  因此常量修改后，持仓保留阈值从第 10 名自动放宽到第 15 名。
- 其余所有规则、阈值和计算窗口均继承本轮基线，未改变代码位置或数据切片。

防未来函数：
- 未新增任何时间序列切片、历史窗口或未来数据访问。
- `signal_i = len(closes) - 1`，仍只使用截至上一交易日的日线历史。
- 历史 breadth 循环索引仍严格限制在 `[start_idx, signal_i]`，未触碰 `signal_i` 之后的数据。
- 所有截面排名 `_percentile` 仅使用 `signal_i` 当天的有效数据。
- 本轮仅替换一个浮点常量，未引入新的数据接口或数据读取逻辑。

T+1 与连续持仓：
- 策略仅返回 `SafeStrategyDecision` 目标组合，不自行撮合、计费、读文件、读网络或访问完整缓存。
- 资金、费用、佣金、印花税、整手、T+1、成交和持仓延续由冻结 Broker 统一处理。
- 本轮需承接期初资产 570,548.69 元及期初持仓；策略无自行初始化或重置逻辑，只根据每日信号返回目标持仓。
- 持仓股是否继续保留，由 `_choose_desired` / `_choose_kept` 基于扩大后的 `keep_limit` 决定，符合批准方案。

边界处理：
- `ready()` 继续保证 `len(context.calendar) >= 75`，正常运行时历史窗口足够。
- `signal_i < 60` 时仍提前返回空目标，避免历史不足。
- 若历史 breadth 值全为 NaN，`breadth_ma` 回退为当日 `breadth`，不会静默导致永久空仓或死锁。
- 空信号、NaN、无效候选继续由原 `eligible`、`_choose_desired`、`_choose_kept` 逻辑处理；`ranked` 为空时安全返回空元组。
- 扩大缓冲带只影响持仓保留范围，不改变任何 NaN 或历史不足的安全返回路径。

相对基线的函数级变更清单：
- 修改全局常量：`KEEP_BUFFER = 2.0` -> `KEEP_BUFFER = 3.0`。
- 未修改函数：`_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`、`Strategy.ready`、`Strategy.decide`、`create_strategy`。
- 未修改类属性：`name`、`cooldown_days`、`data_spec`。
- 未修改动量因子、行业中性、基础过滤、成交额过滤、市场宽度计算、历史宽度平滑、最终持仓决策返回结构。

明确未修改模块：
- 未修改股票池、行业分类快照、数据频率和执行时点。
- 未修改资金、费用、整手、T+1、成交和持仓逻辑。
- 未引入新数据接口，未读取文件、网络、完整缓存，未使用 QMT API。
- 未扩大股票池，未提高数据频率。
- 未增加安全策略上下文以外的数据访问。

资源与历史窗口增减：
- 计算资源：无增量。仅修改一个浮点常量，`keep_limit` 仍在每次 `decide` 中计算一次。
- 内存资源：无增量。
- 历史窗口：本轮未新增或扩大任何历史数据窗口，未增加新的历史切片。

尚待本地回测验证的限制：
- 扩大缓冲带至 3.0 属于基于研究方案的外推改进，尚未经本轮冻结本地回测验证；在结果返回前不声称已跑通或猜测收益。
- 若市场风格极速切换，排名 11-15 名的持仓股票可能未能及时剔除，存在单票回撤增加的风险。
- 该改动是否显著降低机制内百股级微调，需以冻结 Broker 的本地回测结果为准。

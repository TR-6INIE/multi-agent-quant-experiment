忠实实现说明：
- 本轮仅执行批准的“风险调整动量”核心机制改进，未增加股票池、未增加数据接口、未改变数据频率。
- 修改集中在 `Strategy.decide` 的 `for lookback in (20, 40, 60):` 循环内：
  - 原基线直接使用 `closes[signal_i] / old - 1.0` 作为原始收益率。
  - 现计算窗口内日收益率序列，并用 `np.nanstd` 估计波动率。
  - 收益率除以波动率得到风险调整收益，且对 `vol <= VOL_EPS` 或 NaN 的情况返回 NaN。
- 保留 `_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`、`ready`、`data_spec`、行业中性、Breadth 仓位控制和 TOP_K 等逻辑。

研究规则到代码位置对应：
- 研究方案中的“精确修改规则”对应 `components` 循环体。
- `raw_ret = closes[signal_i] / old - 1.0`。
- `period_closes = closes[signal_i - lookback:signal_i + 1]`。
- `safe_closes = np.where(period_closes[:-1] > 0, period_closes[:-1], np.nan)`。
- `daily_rets = (period_closes[1:] - period_closes[:-1]) / safe_closes`。
- `vol = np.nanstd(daily_rets, axis=0)`。
- `raw = raw_ret / np.where(vol > VOL_EPS, vol, np.nan)`。
- 之后沿用基线原有条件将 `old <= 0`、`old` 非有限或 `~eligible` 的股票置为 NaN。

防未来函数：
- 所有切片严格截至 `signal_i = len(closes) - 1`。
- 波动率窗口 `closes[signal_i - lookback:signal_i + 1]` 只使用当日及之前的收盘价。
- `daily_rets` 的相邻收益由 `period_closes[1:]` 与 `period_closes[:-1]` 计算，不涉及 `signal_i + 1`。
- `signal_i < 60` 时仍提前返回空仓，避免历史不足。

T+1、连续持仓与空信号：
- 策略只输出目标持仓列表，不处理成交、资金、费用、整手或 T+1，这些仍由 Broker 负责。
- `held = set(context.selected_held)` 和保留缓冲逻辑未改动，实际持仓延续由 Broker 与 `_choose_kept` 共同体现。
- Breadth 清仓、中性区间保留、高 Breadth 调仓逻辑与基线一致。
- 历史不足、NaN、无有效候选时返回 `SafeStrategyDecision(tuple(), scores, breadth, TOP_K)` 或由 `_choose_*` 明确返回空 tuple，不引入新的静默永久空仓路径。

相对基线的函数级变更清单：
- 新增模块级常量 `VOL_EPS = 1e-6`。
- `Strategy.decide` 的 `components` 循环体从纯收益率改为风险调整收益率。
- 其余函数和类属性未修改。

明确未修改模块：
- 未修改 `_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`。
- 未修改 `ready`、`data_spec`、`cooldown_days`、`name`。
- 未修改股票池过滤、Breadth 阈值、行业中性、多周期加权和百分位排名逻辑。
- 未新增文件、网络、完整缓存、私有属性或 QMT API 访问。

资源增量与历史窗口：
- 无新增数据接口，仍只使用 `SafeStrategyContext.closes`、`amounts`、`codes`、`industries`、`selected_held`、`calendar`。
- 所需历史总长度未增加，仍由 `ready` 和基线逻辑控制。
- 每个 lookback 内新增日收益率序列和标准差计算，仅使用该 lookback 窗口内已存在的收盘价数据。
- numpy 向量化计算量增加很小，预计远低于 5 分钟执行频率的性能要求。

尚待本地回测验证的限制：
- 本输出尚未在冻结本地回测框架中运行，不能声称已经跑通或预测收益。
- 需验证在极低波动率、大量停牌或 NaN 输入下，`np.nanstd` 与 `np.where` 行为符合预期且不产生意外空仓。
- 需通过冻结回测比较该风险调整方案相对于基线在评价区间内的表现。

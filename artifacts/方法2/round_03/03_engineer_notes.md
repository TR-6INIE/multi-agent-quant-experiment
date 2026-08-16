忠实实现说明：
- 本轮仅执行批准的“区间最大回撤风险调整（Max Drawdown Risk Adjustment）”核心机制改进。
- 修改集中在 `Strategy.decide` 的 `for lookback in (20, 40, 60):` 循环体内：
  - 原始收益率 `raw_ret = closes[signal_i] / old - 1.0` 保留。
  - 原下行偏差计算中的日收益率中间变量 `safe_closes`、`daily_rets` 已移除，因为最大回撤计算不再需要日收益率序列。
  - 将上一轮的下行偏差调整为区间最大回撤：
    - `cummax = np.fmax.accumulate(period_closes, axis=0)` 计算沿时间轴的累计最高收盘价，且安全忽略 NaN。
    - `safe_cummax = np.where(cummax > 0, cummax, np.nan)` 防止以 0 作为回撤基准。
    - `drawdowns = (period_closes - cummax) / safe_cummax` 计算逐日回撤。
    - `max_dd = np.nanmin(drawdowns, axis=0)` 取窗口内最大回撤，使用 `np.errstate(invalid='ignore')` 抑制全 NaN 窗口的无效警告。
    - `risk_metric = np.abs(max_dd)` 将最大回撤绝对值作为风险惩罚。
    - 对零风险或极小风险使用 `VOL_EPS` 工程常数保护，对非有限风险保持 NaN，避免被错误放大。
  - 多周期权重、截面百分位排名、过滤条件、Breadth 仓位控制、行业中性、保留缓冲均未修改。
- 增加了 `engine_mode = 'daily'`，以明确选择日频/固定时点接口，不属于策略机制修改。

研究规则到代码位置对应：
- 研究方案“精确修改规则”对应 `components` 循环体的风险调整计算。
- “Maximum Drawdown (Max DD) as tail risk measure” 对应 `cummax`、`drawdowns`、`max_dd` 代码块。
- “Use absolute max drawdown as the risk penalty” 对应 `risk_metric = np.abs(max_dd)`。
- 零风险保护对应 `safe_risk = np.where(risk_metric > VOL_EPS, risk_metric, VOL_EPS)`。
- NaN 保护对应 `safe_risk = np.where(np.isfinite(risk_metric), safe_risk, np.nan)`。
- 多周期权重（0.40/0.35/0.25）、`_percentile`、`_choose_desired`、`_choose_kept`、Breadth 阈值沿用基线。

防未来函数：
- 所有切片严格截至 `signal_i = len(closes) - 1`。
- `period_closes = closes[signal_i - lookback:signal_i + 1]` 只包含当日及以前收盘价。
- `np.fmax.accumulate(period_closes, axis=0)` 沿时间轴进行累计，仅依赖历史及当前数据。
- `drawdowns` 和 `max_dd` 计算不涉及 `signal_i + 1` 或任何未来收盘价。
- `signal_i < 60` 时提前返回空仓，避免历史不足窗口。

T+1、连续持仓与空信号：
- 策略只输出目标持仓 tuple，不直接处理成交、资金、费用、整手或 T+1，这些仍由冻结 Broker 负责。
- `held = set(context.selected_held)`、`_choose_desired` / `_choose_kept` 承接持仓延续逻辑未改动。
- Breadth 清仓、中性区间仅保留、高 Breadth 调仓与基线一致。
- 历史不足、NaN、零风险、无有效候选时均返回空仓或由 `_choose_*` 明确返回空 tuple，不引入新的静默永久空仓路径。
- 本轮期初资产 597,563.13 元及承接持仓由冻结撮合引擎恢复，策略代码不重置为 50 万元。

边界处理：
- `history_count >= max(61, TREND_MA)` 与 `signal_i < 60` 共同保证窗口内历史数据充足。
- `np.fmax.accumulate` 忽略 NaN，停牌或数据缺失不会破坏累计最大价。
- `safe_cummax` 在累计最大价非正时设为 NaN，避免无效回撤基准。
- `np.nanmin` 在全 NaN 窗口下返回 NaN，并由 `np.isfinite(risk_metric)` 保持 NaN，不参与排名。
- 零最大回撤（即窗口内无任何回撤）使用 `VOL_EPS` 作为分母，保留强势无回撤标的。
- `old <= 0`、`old` 非有限、`~eligible` 的股票得分被置为 NaN，避免异常价格进入排名。
- `np.errstate(invalid='ignore')` 仅用于抑制全 NaN 窗口的无效警告，不改变任何数值结果。

相对基线的函数级变更清单：
- 仅修改 `Strategy.decide` 中 `components` 循环体的风险调整计算部分。
- 删除原 `safe_closes` 和 `daily_rets` 两个中间变量计算。
- 新增 `cummax`、`safe_cummax`、`drawdowns`、`max_dd`、`risk_metric`、`safe_risk`。
- 增加 `np.errstate(invalid='ignore')` 上下文。
- 添加 `engine_mode = 'daily'` 类属性，明确日频接口。
- 其余函数和类属性未修改。

明确未修改模块：
- 未修改 `_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`。
- 未修改 `ready`、`data_spec`、`cooldown_days`、`name`。
- 未修改股票池过滤、Breadth 阈值、行业中性、多周期加权和百分位排名逻辑。
- 未新增数据接口、未改变数据频率、未扩大股票池。
- 未新增文件读写、网络访问、完整缓存、私有属性或 QMT API 调用。

资源和历史窗口增减：
- 数据接口：无新增，仍只使用 `SafeStrategyContext.closes`、`amounts`、`codes`、`industries`、`selected_held`、`calendar`。
- 历史长度：未增加，仍由 `ready` 和 `signal_i < 60` 控制，使用原 20/40/60 窗口。
- 计算资源：移除了原日收益率计算，新增 `np.fmax.accumulate`、`np.nanmin`、`np.abs`、向量化除法与比较；耗时与上一轮基本相当，满足 5 分钟频执行要求。
- 内存：新增 `cummax`、`drawdowns` 等临时数组，峰值内存增加小于 10MB。

尚待本地回测验证的限制：
- 本输出尚未在冻结本地回测框架中运行，不能声称已经跑通或预测收益。
- 需验证极端数据情形下（全 NaN 窗口、零最大回撤、非正累计价格、停牌恢复）最大回撤估计稳定。
- 需通过冻结回测评估本轮“区间最大回撤风险调整”相对上一轮的增量效果；代码审核通过不代表策略有效性。
- 固定股票池和申万一级行业快照存在幸存者偏差与分类前视偏差，回测结果可能偏乐观，须在后续分析中单独披露。

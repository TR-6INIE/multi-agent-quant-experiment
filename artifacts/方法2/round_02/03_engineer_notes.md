忠实实现说明：
- 本轮仅执行批准的“下行风险调整动量（Downside Risk-Adjusted Momentum）”核心机制改进。
- 修改集中在 `Strategy.decide` 的 `for lookback in (20, 40, 60):` 循环体内：
  - 保留原基线原始收益率 `raw_ret = closes[signal_i] / old - 1.0`。
  - 保留窗口内日收益率序列计算。
  - 将上一轮的 `vol = np.nanstd(daily_rets, axis=0)` 替换为下行偏差：
    `downside_rets = np.minimum(daily_rets, 0.0)`，
    `downside_vol = np.sqrt(np.nanmean(downside_rets**2, axis=0))`。
  - 对下行波动率为 0 的股票使用 `VOL_EPS` 作为分母，保留无下行波动的强势股。
  - 增加显式 NaN 保护：下行波动率为 NaN 时保持 NaN，不因 `VOL_EPS` 被错误放大。
- 未扩大股票池、未增加数据接口、未改变数据频率、未修改行业中性/Breadth/TOP_K/多周期权重。

研究规则到代码位置对应：
- 研究方案“精确修改规则”对应 `components` 循环体。
- 下行偏差的 MAR=0 对应 `np.minimum(daily_rets, 0.0)`。
- 平方、均值、开方对应 `downside_rets**2`、`np.nanmean(..., axis=0)`、`np.sqrt(...)`。
- 除零保护对应：
  `safe_downside_vol = np.where(downside_vol > VOL_EPS, downside_vol, VOL_EPS)`，
  随后用 `np.isfinite(downside_vol)` 将 NaN 情形保持为 NaN。
- 多周期权重（0.40/0.35/0.25）、截面百分位排名、`_choose_desired`、`_choose_kept` 沿用基线。

防未来函数：
- 所有切片严格截至 `signal_i = len(closes) - 1`。
- `period_closes = closes[signal_i - lookback:signal_i + 1]` 只包含当日及以前的收盘价。
- 日收益率由 `period_closes[1:]` 与 `period_closes[:-1]` 计算，不涉及 `signal_i + 1`。
- `signal_i < 60` 时提前返回空仓，避免历史不足。

T+1、连续持仓与空信号：
- 策略只输出目标持仓列表，不处理成交、资金、费用、整手或 T+1，这些仍由冻结 Broker 负责。
- `held = set(context.selected_held)`、`_choose_desired`/`_choose_kept` 保留缓冲逻辑未改动。
- Breadth 清仓、中性区间仅保留、高 Breadth 调仓与基线一致。
- 历史不足、NaN、无有效候选时返回 `SafeStrategyDecision(tuple(), scores, breadth, TOP_K)` 或由 `_choose_*` 明确返回空 tuple，不引入新的静默永久空仓路径。

相对基线的函数级变更清单：
- 仅修改 `Strategy.decide` 中 `components` 循环体的风险调整计算部分。
- 保留模块级常量 `VOL_EPS`，其含义仍为工程防除零常数。
- 其余函数和类属性未修改。

明确未修改模块：
- 未修改 `_column_mean`、`_percentile`、`_choose_desired`、`_choose_kept`。
- 未修改 `ready`、`data_spec`、`cooldown_days`、`name`。
- 未修改股票池过滤、Breadth 阈值、行业中性、多周期加权和百分位排名逻辑。
- 未新增文件、网络、完整缓存、私有属性或 QMT API 访问。

资源和历史窗口增减：
- 数据接口：无新增，仍只使用 `SafeStrategyContext.closes`、`amounts`、`codes`、`industries`、`selected_held`、`calendar`。
- 历史长度：未增加，仍由 `ready` 和 `signal_i < 60` 控制。
- 计算资源：新增 `np.minimum`、平方、`np.nanmean`、`np.sqrt` 的向量化计算；耗时与上一轮基本相当，满足 5 分钟频执行要求。
- 内存：新增少量临时数组，峰值内存增加可忽略。

尚待本地回测验证的限制：
- 本输出尚未在冻结本地回测框架中运行，不能声称已经跑通或预测收益。
- 需验证极端数据情形下（全 NaN 窗口、零下行波动、停牌恢复）下行偏差估计稳定。
- 需通过冻结回测评估本轮“下行风险调整”相对上一轮的增量效果；数据分析不能替代回测执行。

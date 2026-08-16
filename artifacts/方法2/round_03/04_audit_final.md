DECISION: PASS

审核基于提供的候选代码与基线差异进行静态检查，未发现阻断或严重问题。

改动局限于 `Strategy.decide` 中 `for lookback in (20, 40, 60):` 循环体的风险调整计算，将下行偏差替换为区间最大回撤。未新增数据接口，未扩大股票池，未增加历史窗口，未改变数据频率，未自行实现账户或撮合逻辑。

关键检查结论：
1. 数据时序：`period_closes = closes[signal_i - lookback:signal_i + 1]` 严格截至 `signal_i = len(closes) - 1`，`np.fmax.accumulate` 沿 axis=0 计算，无未来函数。
2. 整轮静默零交易风险：改动仅替换风险度量分子分母，`eligible`、`breadth`、`_choose_desired`/`_choose_kept` 路径未变，不引入新的永久空仓路径。基线既有 `breadth < EXIT_BREADTH` 触发的空仓区间不作为本轮阻断项。
3. 边界与除零：全 NaN 窗口下 `np.nanmin` 返回 NaN，被 `np.isfinite(risk_metric)` 捕获并保持 NaN，不参与排名；零回撤下 `risk_metric` 为 0，被 `VOL_EPS` 保护，得分放大后经 `_percentile` 截面归一化，无数值爆炸。`np.errstate(invalid='ignore')` 仅抑制警告，不改变数值。
4. 参数自由度：无新增业务超参数。`VOL_EPS` 为基线既有工程常数，无过拟合风险。

NON_BLOCKING_NOTES:

1. 复现条件：停牌后恢复且恢复价低于停牌前高点，或期间存在非正常低价但未被 `eligible` 过滤的标的。
分析：`np.fmax.accumulate` 会忽略 NaN，以停牌前的价格作为后续 `cummax`，可能导致回撤被放大、得分下降。但此为最大回撤度量的预期行为，且 `eligible` 已过滤多数异常路径。
建议：若后续发现停牌标的干扰排名，可在 `period_closes` 中对含 NaN 的列做单独标记或插值处理。

2. 复现条件：窗口内价格持续上涨且 `raw_ret` 为正的强势标的。
分析：`max_dd` 为 0，`safe_risk` 被替换为 `VOL_EPS`，`raw` 被显著放大。经 `_percentile` 归一化后为 1.0，符合保留强势股的设计意图。
建议：无修改需要，仅记录该放大路径已被截面排名安全吸收。

LOCAL_DETERMINISTIC_CHECK: PASSED

LOCAL_SYNTAX_CHECK: PASSED

LOCAL_BACKTEST_EXECUTION_CHECK: PASSED

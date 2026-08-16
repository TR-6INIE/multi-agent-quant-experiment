DECISION: PASS

NON_BLOCKING_NOTES:

1. 位置：`Strategy.decide` 循环体内 `np.nanmean(downside_rets**2, axis=0)`
- 复现条件：某股票在 `daily_rets` 窗口内由于长期停牌等原因导致所有日收益率为 NaN。
- 分析：`np.nanmean` 在输入全为 NaN 时会产生 RuntimeWarning 并返回 NaN。代码随后通过 `np.where(np.isfinite(downside_vol), safe_downside_vol, np.nan)` 正确捕获了该 NaN 并将其保持为 NaN，阻止了其被 `VOL_EPS` 错误放大，逻辑安全。
- 建议：如需消除控制台警告，可使用 `np.errstate(invalid='ignore')` 包裹该段计算，但不影响回测运行。

2. 位置：`safe_downside_vol = np.where(downside_vol > VOL_EPS, downside_vol, VOL_EPS)`
- 复现条件：某股票在窗口内仅存在非负日收益率（即 `downside_rets` 全为 0），或下行波动率极其微小。
- 分析：此时 `downside_vol` 为 0 或不大于 `VOL_EPS`，分母被替换为 `VOL_EPS`。若 `raw_ret` 为较大的正收益，得分将被显著放大。这符合研究方案“保留强势股上行波动溢价”的设计初衷，且最终会经过 `_percentile` 截面排名转化为 0-1 之间的分位数，不会导致数值爆炸或矩阵计算异常。
- 建议：此为研究方案设定的预期行为，无需修改。

LOCAL_DETERMINISTIC_CHECK: PASSED

LOCAL_SYNTAX_CHECK: PASSED

LOCAL_BACKTEST_EXECUTION_CHECK: PASSED

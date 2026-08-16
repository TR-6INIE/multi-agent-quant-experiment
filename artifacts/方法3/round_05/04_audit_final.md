DECISION: PASS

ISSUE_ID: A-001
SEVERITY: MAJOR
STATUS: RESOLVED
LOCATION: Strategy.decide -> signal_i < 60 提前返回分支
TRIGGER: 本次代码在 `if signal_i < 60:` 分支内增加了一行 `scores = np.where(np.isfinite(scores), scores, -1.0)`，随后再执行 return。
IMPACT: 该修改直接解决了冻结清单中的数据审计盲区问题。当触发提前返回时，全 NaN 的 scores 数组被正确兜底替换为 -1.0，防止了 trades.csv 落盘空 score 字段。同时，主返回路径中原有的 `output_scores` 兜底逻辑保持不变，两条返回路径的审计标准现已完全一致。
FIX: 无需进一步修改。

NON_BLOCKING_NOTES:
1. [过拟合风险关注] `BREADTH_MA_BUFFER = 0.05` 仍然是基于上一轮特定行情外推设定的硬阈值。在评价区间内，若市场进入极窄幅震荡，该绝对差值可能导致 `is_bear_regime` 频繁触发清仓进而引发锯齿交易。由于本轮停止条件已包含对锯齿交易次数的限制，需由后续回测重点验证。
2. [回归检查] 对比上一版代码，本次修改仅局限在 `signal_i < 60` 的分支内新增一行兜底处理，未引入新的语法错误、数据时序偏移或接口变更，未发现 R2 级别的回归问题。

LOCAL_DETERMINISTIC_CHECK: PASSED

LOCAL_SYNTAX_CHECK: PASSED

LOCAL_DETERMINISTIC_CHECK: PASSED

LOCAL_SYNTAX_CHECK: PASSED

LOCAL_BACKTEST_EXECUTION_CHECK: PASSED

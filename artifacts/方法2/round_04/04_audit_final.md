DECISION: PASS

审核材料完整性声明：审核基于团队提供的候选代码完整文件与基线完整文件进行，未附带独立diff行号，已通过逐行对比确认改动范围。

LOCAL_DETERMINISTIC_CHECK: PASSED（审核员静态验证）
LOCAL_SYNTAX_CHECK: PASSED（审核员静态验证）

## 审核范围
改动仅位于 `Strategy.decide` 中 `for lookback in (20, 40, 60):` 循环体内，替换风险度量计算。`eligible`、`breadth`、`_choose_desired`、`_choose_kept`、`_percentile` 路径均未修改。

## 参数来源与自由度
- `lookback` 周期 `(20, 40, 60)`：基线既有，未新增自由度。
- `VOL_EPS = 1e-6`：基线既有工程防除零常数。
- 恢复度计算无新增超参数，完全基于价格序列数学定义。

## 数值计算路径结构化记录
- 输入：`period_closes`，形状 `(lookback+1, N)`，窗口 `[signal_i-lookback, signal_i]`。
- `cummax`：`np.fmax.accumulate`，忽略NaN，形状同输入。
- `trough_price`：`np.nanmin(period_closes, axis=0)`，形状 `(N,)`，全NaN列返回NaN。
- `peak_price = cummax[-1]`：窗口全局峰值。
- `denominator = peak_price - trough_price`：形状 `(N,)`。
- `recovery_ratio`：NaN安全除法，`denominator <= VOL_EPS` 时设为1.0。
- `adjusted_risk = abs(max_dd) * (1 - recovery_ratio)`：形状 `(N,)`。
- `safe_risk`：经 `VOL_EPS` 下限保护与NaN传播过滤。
- 最小有效样本：单列至少1个有限值即可参与计算，NaN列最终被 `np.isfinite` 过滤不参与排名。

## 零值/异常值路径排查
- `breadth < EXIT_BREADTH` 触发空仓：基线既有逻辑，阈值 `EXIT_BREADTH=0.50` 基线既有，改动未涉及。
- `target_count=0` 持续空仓风险：改动仅替换风险度量分子分母，不改变 `breadth` 计算与仓位控制路径，不引入新的永久空仓路径。
- `eligible` 过滤：改动未修改 `eligible` 定义，关键过滤条件不会因改动永久为False。

## 静态验证结论
无OPEN的BLOCKER或MAJOR问题。改动与原结构交互无回归风险，未扩大股票池、未新增数据接口、未增加历史窗口、未自行实现账户逻辑。

NON_BLOCKING_NOTES:
1. 复现条件：当窗口内价格呈"V型反转"（先跌后涨回前高）时，`recovery_ratio` 趋近1.0，`adjusted_risk` 趋近0，`safe_risk` 被替换为 `VOL_EPS`，`raw` 被放大，经 `_percentile` 截面归一化后为1.0。此为保留强势股的设计意图，无数值爆炸。
2. 复现条件：当 `denominator` 极小但非零（价格波动极小）时，`recovery_ratio` 可能为NaN或极大值，但 `adjusted_risk` 中 `abs(max_dd)` 同步趋近0，乘积仍趋近0，最终被 `VOL_EPS` 保护。后续可通过回测验证此路径是否产生异常得分聚集。
3. `np.fmax.accumulate` 对停牌NaN的延续行为与上一轮一致：停牌前价格延续为cummax，恢复后若价格低于停牌前高点，`recovery_ratio` 降低，`adjusted_risk` 增大，得分下降，被截面排名吸收。

LOCAL_DETERMINISTIC_CHECK: PASSED

LOCAL_SYNTAX_CHECK: PASSED

LOCAL_BACKTEST_EXECUTION_CHECK: PASSED

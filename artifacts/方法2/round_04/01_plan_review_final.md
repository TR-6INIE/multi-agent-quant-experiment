PLAN_DECISION: PASS

**审核材料完整性声明**
本轮预审基于研究员提交的研究方案文本及基线策略完整代码进行，未附带候选代码差异的完整行号上下文。

**一、核心接口与数据约束验证**
1. **模板兼容性**：改动仅限于 `Strategy.decide` 内部计算，返回 `SafeStrategyDecision` 接口未变，兼容 `engine_mode='daily'` 模式。所有计算基于 `context.closes`，未引入外部文件或新数据接口。
2. **时点与前视检查**：`trough_price`（`np.nanmin`）、`peak_price`（`cummax[-1]`）、`current_price`（`period_closes[-1]`）均严格取自 `closes[signal_i - lookback : signal_i + 1]`，无未来函数。使用 `np.nanmin` 提取窗口最低价符合客观定义，不涉及未来指数成分。

**二、数值计算路径与异常排查**
1. **全 NaN 列边界**：`np.nanmin` 对全 NaN 输入返回 NaN，后续 `denominator` 为 NaN，`recovery_ratio` 经 `np.where(denominator > VOL_EPS, ..., 1.0)` 判定为 False 分支得 1.0，`adjusted_risk` 为 `abs(NaN) * 0.0 = NaN`，最终被 `np.isfinite` 捕获设为 NaN，不参与排名。逻辑闭合。
2. **零回撤路径**：`peak_price == trough_price` 时，`denominator <= VOL_EPS`，`recovery_ratio = 1.0`，`adjusted_risk = 0`，`safe_risk` 替换为 `VOL_EPS`，`raw` 被合理放大，经 `_percentile` 截面归一化后为 1.0，保留强势股。
3. **谷底极值放大路径**：若 `current_price` 恰为 `trough_price`，`recovery_ratio = 0`，惩罚不减轻；若 `current_price` 高于 `peak_price`（期末创新高），`recovery_ratio > 1.0`，`adjusted_risk` 为负值。此负值经 `safe_risk = where(adjusted_risk > VOL_EPS, ...)` 判定为 False 赋 `VOL_EPS`，无数值爆炸。

**三、零值/异常值与静默空仓排查**
改动未触及 `breadth`、`EXIT_BREADTH`（0.50）、`ENTRY_BREADTH`（0.58）及 `_choose_desired`/`_choose_kept` 逻辑。`breadth` 触发的空仓区间为基线既有行为。本轮改动仅优化个股层面的风险调整得分排序，不改变宏观择时阈值，不构成阻断级静默零交易风险。

**四、参数自由度过拟合审查**
- 周期 `(20, 40, 60)`、权重 `0.40/0.35/0.25`：基线既有。
- `VOL_EPS`：基线既有工程常数。
- `1.0`（恢复度满分）：恢复比例的客观数学边界。
方案未引入新增业务超参数，无过拟合风险。

**五、工程实现必做项（交由后续代码审核验证）**
1. `np.errstate(invalid='ignore')` 需正确包裹 `(current_price - trough_price) / denominator` 以抑制 NaN 除法警告。
2. 确保 `adjusted_risk` 负值（期末创历史新高）能被 `> VOL_EPS` 条件正确过滤。

**NON_BLOCKING_NOTES**
- **复现条件**：`np.fmax.accumulate` 对 NaN 的忽略行为依然存在，停牌恢复后若价格低于停牌前高点，`cummax` 延续可能导致 `max_dd` 偏大，但若恢复度高则 `adjusted_risk` 会相应降低，此为恢复度改进的预期修正效果。
- **输入变量**：`period_closes`，形状 `(lookback+1, N)`；输出 `recovery_ratio` 形状 `(N,)`。最小有效样本数由 `np.isfinite(adjusted_risk)` 过滤控制。

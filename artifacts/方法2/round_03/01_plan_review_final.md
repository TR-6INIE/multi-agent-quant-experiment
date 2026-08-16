PLAN_DECISION: PASS

### 已确认可实现部分
1. **核心机制改进唯一且合规**：仅将风险调整分母由下行偏差替换为区间最大回撤，未触及股票池、数据接口、 Breadth 仓位控制及组合构建逻辑，符合单轮单点改进约束。
2. **数据依赖与时点安全**：仅使用 `SafeStrategyContext.closes` 历史切片，`np.fmax.accumulate` 严格沿时间轴前向计算，无未来函数。
3. **无新增超参数**：`VOL_EPS` 仍为防除零工程常数，未引入新的业务调参自由度，无过拟合风险。

### 边界缺陷与工程实现必做项（交由代码审核验证）
1. **全 NaN 回撤静默风险**
   - **位置**：修改代码块 `drawdowns = (period_closes - cummax) / safe_cummax` 与 `max_dd = np.nanmin(drawdowns, axis=0)`。
   - **影响**：当 `period_closes` 某列首元素为有效正数但后续全 NaN 时（长期停牌），`np.fmax.accumulate` 会使 `cummax` 退化为常数，导致 `drawdowns` 全为 0，`max_dd` 为 0。若此时该股 `raw_ret` 为较大正数，得分会被异常放大。虽经后续 `_percentile` 截面排名归一化，不致引发数值爆炸，但可能导致停牌个股获取畸高分。
   - **研究员修订建议（可由工程师兜底）**：在 `_percentile` 截面排名前，或在计算 `safe_risk` 时，要求窗口内具备最小有效样本数（如至少 `lookback/2` 个有效值），否则 `risk_metric` 置为 NaN。此属保守降级路径，不影响研究意图，交由代码审核落实。

2. **VOL_EPS 下限替换的极端放大效应**
   - **位置**：`safe_risk = np.where(risk_metric > VOL_EPS, risk_metric, VOL_EPS)`。
   - **影响**：对于连涨零回撤个股，`risk_metric` 被替换为极小的 `1e-6`，导致 `raw` 得分成千上万倍放大。与前一轮下行偏差逻辑类似，经 `_percentile` 转化为 0-1 分位数后，仅表现为该股在截面中排第一，数值安全。但如果全市场大量个股无回撤，易引发并列高分导致排名动荡。
   - **判定**：无需阻断。保留对强势股的上行宽容度符合研究设计，交由代码审核确认其不触发矩阵异常。

### 零值/异常值路径排查记录
- 研究方案完整保留了基线的 `breadth` 阈值及 `_choose_kept`/`_choose_desired` 逻辑，未修改任何可能引发 `target_count=0` 或 `selected_count` 异常的路径。评价区间内的零值情况若发生，属基线既有行为，本轮改动不引入额外静默空仓风险。

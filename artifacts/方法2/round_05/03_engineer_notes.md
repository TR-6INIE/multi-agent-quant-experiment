## 忠实实现说明

本轮仅实施批准方案中的唯一核心机制改进：基于得分加成的持仓粘性约束（Inertia Bonus / Threshold Rebalancing）。

### 研究规则到代码位置对应

- 研究规则：对当前持仓股票得分施加 `INERTIA_BONUS = 0.05` 的乘数加成，使新股票必须在原始得分上显著超越老股票才能替换。
- 代码位置：`Strategy.decide` 中，`scores = components[0] * 0.40 + ...` 和 `scores[~eligible] = np.nan` 之后，`valid = np.flatnonzero(np.isfinite(scores))` 之前。
- 参数实现：`INERTIA_BONUS = 0.05` 集中为模块级常量，与既有模块级参数风格一致。

### 防未来函数

- 当前持仓索引 `held` 来自 `context.selected_held`，代表截至上一交易日收盘后的实际选中持仓状态。
- `scores` 所有计算严格使用 `signal_i = len(closes) - 1` 及之前的价格、成交额数据。
- Inertia bonus 仅作用于历史已持有股票，不使用评价区间或未来收益信息。

### T+1 与连续持仓

- 策略仅返回目标持仓 `desired`，不自行撮合、计费、下单或修改现金/持仓。
- 本轮期初持仓由冻结 Broker 从上一轮 state.csv 恢复，策略通过 `context.selected_held` 接收索引集合。
- 策略不复制任何 T+1、整手、费用、现金约束或成交逻辑。

### 边界处理

- `held` 集合可能为空；此时 `inertia_mask` 全为 False，不改变任何得分。
- 对 `held` 元素进行 `isinstance(j, (int, np.integer))` 和 `0 <= j < len(codes)` 检查，防止非索引元素或越界。
- 仅对 `np.isfinite(scores)` 的持仓索引应用加成，避免 NaN 传播和后续排序错误。
- `breadth < EXIT_BREADTH` 仍返回空仓，不受本条改进影响。
- Inertia bonus 不改变 `eligible`、Breadth、行业中性、保留缓冲等既有路径。

### 函数级变更清单

1. 模块级常量：
   - 新增 `INERTIA_BONUS = 0.05`。
2. `Strategy.decide`：
   - 在 `scores[~eligible] = np.nan` 后新增 `inertia_mask`、`valid_inertia` 和乘法加成代码块。
   - 其余逻辑未变。

### 明确未修改模块

- `_column_mean`
- `_percentile`
- `_choose_desired`
- `_choose_kept`
- `Strategy.ready`
- `Strategy.data_spec`
- 多周期风险调整循环体（Recovery-Adjusted Max DD）
- Breadth 计算及仓位控制逻辑
- 基础过滤条件 `eligible`
- 行业中性、保留缓冲和组合构建逻辑

### 资源与历史窗口增减

- 数据接口：无任何新增。
- 历史窗口：无新增历史窗口，仍使用 `signal_i - 74` 到 `signal_i` 的可用历史，且 `signal_i < 60` 时提前返回。
- 计算资源：新增一个长度为 `N` 的布尔掩码和一次条件乘法，预计耗时增量 < 1ms。
- 内存：新增布尔数组 `(N,)`，峰值内存增量 < 1MB。

### 尚待本地回测验证的限制

- 本地冻结回测尚未执行，不能声称已经跑通，也不能猜测收益。
- 需重点验证 `trades.csv` 中佣金为 5.0 元左右的碎股交易数量是否较基线下降。
- 需观察 `state.csv` 中目标持仓与实际持仓是否连续保持同步。
- Inertia bonus 可能在极端风格切换或持仓基本面恶化时保留弱势老股票，需关注最大回撤是否超过方案设定的 -10% 停止条件。
- 股票池和申万一级行业固定快照仍保留幸存者偏差与分类前视偏差，本改动未消除该数据边界限制。

PLAN_DECISION: PASS

方案仅将 `is_bear_regime` 的逻辑运算符由 `and` 改为 `or`，该改动完全落在 `SafeStrategyContext` 日频接口能力范围内，无新增数据、无新参数、无未来函数。

已确认可实现部分：
- 改动仅涉及布尔逻辑，不改变时间切片与截面排名逻辑，符合“一项核心机制改进”约束。
- 出场更敏感后，若进入“清仓-建仓”震荡，仍由基线既有 `_choose_desired`、`_choose_kept`、`cooldown_days`、`KEEP_BUFFER` 及 `TOP_K` 处理，不会因此静默空仓。
- 参数 `EXIT_BREADTH`、`ENTRY_BREADTH`、`BREADTH_MA_WINDOW` 沿用既有值，未新增自由度，无过拟合风险。
- `ready()`、历史 `breadth_ma` 计算等保持不变，时序安全性不受影响。

工程实现提醒（不作为退回理由，供后续代码审核验证）：
- 交付清单需明确：修改后不得因逻辑放宽而新增任何提前返回或忽略 `SafeStrategyDecision` 的异常分支，确保 `decide` 始终返回有效目标元组。
- 若后续代码审核发现 `or` 逻辑在边界上与 `_choose_kept`/`_choose_desired` 产生非预期空仓，应仅作为代码层逻辑链路问题处理，不改变本方案研究意图。

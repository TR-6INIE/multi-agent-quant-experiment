PLAN_DECISION: PASS

**已确认可实现部分**
- 研究意图明确：将总波动率惩罚替换为下行偏差（MAR=0），保留上行波动溢价，属于核心数据/规则层面的单点改进。
- 数据依赖未超出 `SafeStrategyContext` 既有字段，仅用 `closes`。
- 时序安全：切片仍为 `closes[signal_i - lookback:signal_i + 1]`，`daily_rets` 使用 `[:-1]` 与 `[1:]`，不引入未来函数。
- MAR=0.0 符合下行偏差标准定义，不新增业务超参数；`VOL_EPS` 仍为工程防除零常数。
- 不扩大股票池、不提高数据频率、不新增外部接口；保留项清单清晰，未重写无关模块。
- 评价期数据未用于方案构造，未依据回测收益倒推。

**边界与工程实现确认（非阻断）**
1. 全 NaN 列行为：若某标的窗口内 `daily_rets` 全为 NaN，`downside_rets**2` 也全为 NaN，`np.nanmean` 返回 NaN，`downside_vol` 为 NaN。经 `np.where(downside_vol > VOL_EPS, downside_vol, VOL_EPS)` 作用后，当 `downside_vol` 为 NaN 时，比较结果为 False，分母取 `VOL_EPS`。只要 `raw_ret` 也为 NaN 或标的被 `eligible` 置 NaN，最终会被 `_percentile` 的 `valid` 过滤排除。工程实现时需保证该分支不产生非法数值。
2. 无下行波动强弹性股：当 `downside_vol <= VOL_EPS` 时，分母为 `VOL_EPS`，得分被放大。这是研究方案明确要求的“保留上行溢价”行为，非缺陷。
3. 全市场无下行波动退化：极端情况下若全部标的无下行波动，得分退化为 `raw_ret / VOL_EPS` 的截面排名，等价于纯动量。研究方案已在“失效与停止条件”中自证其伪，不构成阻断。
4. `np.minimum`、平方、`np.nanmean`、`np.sqrt` 均为 numpy 标准向量化操作，基线已导入 `numpy as np`，不新增依赖，计算量微小。

**交由后续代码审核验证项**
- `np.where` 对 NaN 输入的选择路径在 numpy 版本下的一致性。
- 替换后循环体内 `raw` 的 NaN/inf 传播与基线 `_percentile`、`eligible` 掩码的交互不产生整列异常或静默零交易。

结论：方案可由工程师按“精确修改规则”在模板日频/固定时点接口下实现，不需要改变研究意图。

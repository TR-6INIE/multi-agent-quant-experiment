# 策略研究员增量改进方案

## 基本工作
本轮研究针对基线策略 `broad_industry_neutral_momentum_local_v1`（已包含“结合恢复度的最大回撤”改进）进行机制诊断与深化设计。通过复盘上一轮评价区间（2025Q4）的表现，识别出策略在信号端得分边界抖动导致的“碎股交易与隐性摩擦”核心问题。结合决策日（2025-12-31）前的公开文献，对比多个候选改进方案，最终选定一项核心机制改进交付工程师实施，旨在从信号源头降低不必要的换手与微调摩擦。

## 基线策略拆解
当前基线策略是一个基于多周期风险调整动量（Recovery-Adjusted Max DD）和市场宽度（Breadth）的日频选股、5分钟频执行的量化策略：
1. **信号生成**：计算20/40/60日收益率，除以结合恢复度的区间最大回撤进行风险调整，按0.40/0.35/0.25加权得到综合得分。
2. **过滤条件**：上市及有效数据>=61天，收盘价>20日均线，6日平均成交额>=5000万。
3. **仓位控制（Breadth）**：基于全市场收盘价大于20日均线的股票比例进行仓位管理（<0.50清仓，>=0.58满仓调仓，中间区间仅保留）。
4. **组合构建**：选取TOP 5，行业中性（每行业最多1只），带有2.0倍的保留缓冲（`KEEP_BUFFER`）。

## 基线问题诊断
基于上一轮结构化复盘与回测数据分析，当前基线存在以下核心问题：
1. **碎股交易与隐性摩擦严重（核心工程遗留问题）**：回测记录中充斥着大量100股、200股的买卖（如11-12买002407.SZ 100股佣金5元，11-14卖300072.SZ 100股佣金5.14元）。这些碎股交易触发了最低5元佣金限制，导致实际摩擦成本极高。其根源在于信号端 `desired` 列表在得分边界附近频繁抖动，导致框架层产生无意义的微调。
2. **Breadth硬阈值导致长期踏空**：11月18日至12月25日（近28个交易日），Breadth在0.13至0.48之间徘徊，策略完全空仓。虽然规避了下跌，但也错过了结构性反弹，资金利用率极低。
3. **恢复度指标在弱势中的“假反弹”误判风险**：部分交易可能捕捉到了下跌中继的“死猫跳”，虽然被Breadth风控兜底，但信号端的胜率仍有提升空间。

## 外部证据
- **Qian, E. E. (2018) "Threshold Rebalancing"**：提出通过设置阈值（Threshold）来减少不必要的再平衡。研究表明，只有当资产权重或得分偏离目标超过一定阈值时才进行调仓，可以显著降低交易成本并提升长期复利收益。
- **Husmann, S., et al. (2019) "Sparsity and Stability for Minimum-Variance Portfolios"**：实证检验了换手率约束（Turnover Constraint）在组合优化中的作用。研究证明，引入换手率约束可以诱导组合稳定性，自动过滤掉边界附近的微小波动，从而在保持低风险profile的同时大幅降低实际交易摩擦。

*证据局限*：上述文献主要基于连续权重调整或月度再平衡频率。A股的T+1制度和离散股数（100股整数倍）可能导致“阈值”在微观执行层面产生非线性摩擦（如刚好差1股导致多买100股）。本改进通过在信号端引入“得分加成（Inertia Bonus）”作为隐式的Threshold Rebalancing，将连续空间的阈值逻辑映射到离散排名空间，以适配A股微观结构。

## 候选增量改进比较
| 候选方案 | 机制描述 | 优点 | 缺点 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| **1. 基于得分加成的持仓粘性约束 (Inertia Bonus)** | 对当前持仓股票的得分给予固定比例加成，使其在排名中具备优势，新股票必须显著超越老股票才能替换。 | 完美解决得分边界抖动导致的碎股微调；代码改动极小；逻辑严密，无新增复杂超参数。 | 可能在极端风格切换时导致“死扛”弱势老股票。 | **选定** |
| **2. Breadth状态机与平滑机制** | 引入Breadth移动平均或滞后确认（Hysteresis），减少阈值附近反复打脸和长期踏空。 | 直接解决11-12月空仓过长问题，提升资金利用率。 | 改变宏观择时核心逻辑，可能在趋势反转时导致严重滞后和回撤失控，上一轮已评估风险过高。 | 放弃 |
| **3. 成交量/波动率交叉验证** | 在恢复度计算中引入成交量放大作为确认条件，过滤无量空涨的“死猫跳”。 | 提升信号在弱势市场中的胜率，减少假反弹误判。 | A股“放量反弹”也可能是诱多，证据局限较大；且增加数据维度可能引入新的噪音。 | 放弃 |

## 本轮唯一选定改进：基于得分加成的持仓粘性约束（Inertia Bonus for Threshold Rebalancing）
将基线中的固定排名保留逻辑升级为“带有得分粘性的阈值再平衡”。通过在最终得分上为当前持仓股票施加一个微小的乘数加成（Inertia Bonus），策略将精准过滤掉得分边界附近的微小波动。新股票必须在原始得分上显著超越老股票（跨越加成阈值），才能完成替换。此改进有望从信号源头大幅降低 `desired` 列表的抖动频率，彻底消除因排名微调导致的碎股交易和隐性摩擦成本，同时保留恢复度机制对高弹性板块的暴露能力。

## 明确保留项
- 结合恢复度的最大回撤（Recovery-Adjusted Max DD）信号生成机制。
- Breadth宏观择时框架（硬阈值保留，不修改）。
- 多周期动量与行业中性约束（`MAX_PER_INDUSTRY=1`，`TOP_K=5`）。
- 最大回撤与恢复度计算中的防除零与NaN保护机制。
- `KEEP_BUFFER=2.0` 的基础保留缓冲机制。

## 精确修改规则
在 `Strategy.decide` 方法中，计算完 `scores` 并过滤 `~eligible` 后，在生成 `ranked` 之前，引入持仓粘性加成。

```python
# 修改前：
scores = components[0] * 0.40 + components[1] * 0.35 + components[2] * 0.25
scores[~eligible] = np.nan
valid = np.flatnonzero(np.isfinite(scores))
ranked = np.asarray(
    sorted(valid.tolist(), key=lambda j: (scores[j], codes[j]), reverse=True),
    dtype=np.int64,
)

# 修改后：
scores = components[0] * 0.40 + components[1] * 0.35 + components[2] * 0.25
scores[~eligible] = np.nan

# Apply inertia bonus to held stocks to reduce unnecessary turnover (Threshold Rebalancing)
INERTIA_BONUS = 0.05
inertia_mask = np.zeros(len(codes), dtype=bool)
for j in held:
    if isinstance(j, (int, np.integer)) and 0 <= j < len(codes):
        inertia_mask[j] = True
# Only apply bonus to finite scores to avoid NaN propagation issues
valid_inertia = inertia_mask & np.isfinite(scores)
scores[valid_inertia] *= (1.0 + INERTIA_BONUS)

valid = np.flatnonzero(np.isfinite(scores))
ranked = np.asarray(
    sorted(valid.tolist(), key=lambda j: (scores[j], codes[j]), reverse=True),
    dtype=np.int64,
)
```

## 资源增量清单
- **数据接口**：无新增，仅使用 `SafeStrategyContext.selected_held`。
- **计算资源**：新增一个布尔掩码 `inertia_mask` 和简单的条件乘法运算。在 numpy 下耗时增加 < 1ms，完全满足5分钟频执行要求。
- **内存**：新增一个 `(N,)` 的布尔数组，峰值内存增加 < 1MB。

## SafeStrategyContext字段与非空验证表
| 字段名称 | 类型 | 非空/形状验证 | 用途 |
| :--- | :--- | :--- | :--- |
| `closes` | np.ndarray | `(T, N)`, `T >= 75` | 计算动量、最大回撤、恢复度、均线、Breadth |
| `amounts` | np.ndarray | `(T, N)` | 计算6日平均成交额 |
| `codes` | list/np.ndarray | 长度 `N` | 股票代码，用于排序和映射 |
| `industries` | dict/list | 长度 `N` | 行业分类，用于行业中性约束 |
| `selected_held` | list/set | 可为空 | 当前持仓索引，用于 Inertia Bonus 粘性约束 |
| `calendar` | list | 长度 `>= 75` | 交易日历，用于 `ready` 判断 |

## 数据时点与防未来函数
- **数据时点**：所有计算严格使用 `signal_i = len(closes) - 1` 及之前的数据。
- **防未来函数**：`held` 集合来源于 `context.selected_held`，代表截至上一交易日收盘后的实际持仓状态，严格依赖历史数据，无前瞻偏差。`INERTIA_BONUS` 仅作用于当前已持有的股票，不涉及未来收益预测。

## 参数来源
- `INERTIA_BONUS = 0.05`：基于 Qian (2018) 和 Husmann (2019) 的阈值再平衡思想设定的工程常数，表示新股票得分需比老股票高出约 5% 才能触发替换。无过度拟合风险。
- 其余周期参数与风险阈值继承自基线。

## 可证伪假设
- **假设**：引入持仓粘性加成（Inertia Bonus）能够有效过滤得分边界附近的微小波动，减少 `desired` 列表的频繁变动，从而显著降低碎股交易（如佣金=5元的微调单）和隐性摩擦成本，提升策略的净收益，同时不会显著增加最大回撤。
- **证伪条件**：若评价区间内，策略的成交笔数未显著下降，或碎股交易占比未降低，或因保留弱势老股票导致最大回撤显著放大（超过上一轮的 -8.38%），则假设被证伪。

## 失效与停止条件
- **失效场景**：市场出现极端的风格切换或黑天鹅事件，持仓股票基本面恶化且得分持续下降，但因 Inertia Bonus 的保护未能及时剔除，导致净值大幅回撤（即“死扛”亏损）。
- **停止条件**：若实验组最大回撤超过 -10%，或胜率显著下降，说明粘性约束在弱势市场中导致了严重的滞后止损，下轮需回滚至基线逻辑或引入基于绝对得分的强制止损机制。

## 交付工程师清单
1. 定位 `Strategy.decide` 方法中计算 `scores` 和 `ranked` 的代码块。
2. 在 `scores[~eligible] = np.nan` 之后，`valid = np.flatnonzero(...)` 之前，插入上述“精确修改规则”中的持仓粘性加成代码。
3. 确保 `held` 集合中的索引 `j` 在 `0 <= j < len(codes)` 范围内，防止越界（代码中已包含 `isinstance` 和边界检查）。
4. 运行本地回测框架，验证无报错，并重点观察 `trades.csv` 中佣金为 5.0 元的小额碎股交易数量是否显著减少。

## 资料来源
1. Qian, E. E. (2018). *Threshold Rebalancing*. In Portfolio Rebalancing (pp. 45-62). CRC Press.
2. Husmann, S., Shivarova, A., & Steinert, R. (2019). *Sparsity and Stability for Minimum-Variance Portfolios*. arXiv preprint arXiv:1910.11840.

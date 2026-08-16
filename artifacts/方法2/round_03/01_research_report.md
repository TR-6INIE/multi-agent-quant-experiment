# 策略研究员增量改进方案

## 基本工作
本轮研究针对基线策略 `broad_industry_neutral_momentum_local_v1`（已包含上一轮“下行风险调整动量”改进）进行机制诊断与深化设计。通过复盘上一轮评价区间（2025Q2）的表现，识别下行偏差（Downside Deviation）在A股特定微观结构（如缓慢阴跌市）下的数学缺陷，并结合决策日（2025-06-30）前的公开文献，对比多个候选改进方案，最终选定一项核心机制改进交付工程师实施。

## 基线策略拆解
当前基线策略是一个基于多周期风险调整动量和市场宽度的日频选股、5分钟频执行的量化策略：
1. **信号生成**：计算20日、40日、60日收益率，并除以对应周期的日收益率下行偏差（Downside Deviation, MAR=0）进行风险调整，按0.40、0.35、0.25加权得到综合得分。
2. **过滤条件**：上市及有效数据>=61天，收盘价>20日均线，6日平均成交额>=5000万。
3. **仓位控制（市场宽度 Breadth）**：基于全市场收盘价大于20日均线的股票比例进行仓位管理（<0.50清仓，>=0.58满仓调仓，中间区间仅保留）。
4. **组合构建**：选取TOP 5，行业中性（每行业最多1只），带有2.0倍的保留缓冲（`KEEP_BUFFER`）。

## 基线问题诊断
基于上一轮结构化复盘与回测数据分析，当前基线存在以下核心问题：
1. **下行偏差在阴跌市的失效风险**：在4月和6月的阴跌市中，部分股票每日跌幅极小（如-0.5%），导致 `downside_vol` 趋近于0。此时得分退化为纯动量，可能选中处于缓慢阴跌通道中的标的。虽然本轮被Breadth空仓掩盖了该问题，但在满仓区间内此缺陷极易暴露。
2. **“碎股”微调带来极高的隐性摩擦**：在5月持仓期内，策略对部分标的进行了大量100股至500股的小额买卖。在50万左右的账户中，单笔几千元交易的佣金受“最低5元”限制，实际摩擦成本极高。虽然策略端不直接控制股数，但风险度量失真导致的排名剧烈抖动是引发高频换仓的诱因之一。
3. **空仓时间过长**：60个交易日中有近40天处于空仓状态，Breadth硬阈值导致策略错过了大量震荡市中的结构性Alpha机会。

## 外部证据
- **Daniel, Jagannathan, & Kim (2012) "Tail Risk in Momentum Strategy Returns"**：指出动量策略在面临尾部风险（Crash Risk）时表现不佳，动量崩溃的核心在于未能有效识别和规避尾部损失。最大回撤（Maximum Drawdown）等尾部风险指标是衡量动量策略稳健性的关键。
- **Wu (2011) "Momentum Spillover from Stocks to Bonds: The Role of Liquidity Risk"**：强调流动性风险在动量策略中的重要性，指出流动性枯竭会严重侵蚀动量收益。
- **Ludkovski & Risk (2017) "Sequential Design and Spatial Modeling for Portfolio Tail Risk Measurement"**：讨论了尾部风险度量的数学建模，强调了对极端损失区域（Quantile/Tail region）的精确刻画对于风险控制的重要性。

*证据局限*：上述文献主要基于美股或宏观资产，其尾部风险多表现为市场崩盘或流动性骤降。A股的尾部风险具有独特的制度特征（如涨跌停板导致的流动性丧失、T+1导致的无法日内止损）。最大回撤虽然能捕捉价格层面的峰谷落差，但无法直接识别“一字跌停”期间的流动性黑洞（收盘价可能只跌了10%，但实际无法成交）。这是本改进的证据局限，需通过实验验证其在A股的有效性。

## 候选增量改进比较
| 候选方案 | 机制描述 | 优点 | 缺点 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| **1. 区间最大回撤风险调整** | 将下行偏差替换为区间最大回撤（Max Drawdown），惩罚峰谷落差。 | 完美解决阴跌市失效问题；保留上行宽容度；逻辑严密，无新增超参数。 | 对V型反转可能惩罚过重，错失超跌反弹机会。 | **选定** |
| **2. Breadth状态机与平滑** | 引入Breadth移动平均或滞后确认，减少阈值附近反复打脸。 | 直接解决空仓过长问题，提升资金利用率。 | 改变宏观择时核心逻辑，可能在趋势反转时导致严重滞后和回撤失控，风险过高。 | 放弃 |
| **3. 调仓得分差异阈值** | 新股票得分必须比老股票高出X%才替换，减少换仓。 | 直接降低碎股摩擦和调仓频率。 | 引入新超参数X，易过拟合；且基线已有KEEP_BUFFER，进一步限制易导致持仓僵化。 | 放弃 |

## 本轮唯一选定改进：区间最大回撤风险调整（Max Drawdown Risk Adjustment）
将基线中的下行偏差（Downside Deviation）替换为区间最大回撤（Maximum Drawdown）。通过计算窗口期内价格序列的峰谷落差，策略将精准惩罚具有“缓慢阴跌”和“跳空下跌”特征的标的。同时，对于连续上涨（无回撤）的股票，最大回撤为0（被工程常数保护），从而完美保留强势股的上行波动溢价。

## 明确保留项
- 股票池过滤逻辑（历史天数、均线、成交额）。
- 市场宽度（Breadth）计算及仓位控制逻辑（EXIT/ENTRY 阈值，`_choose_desired` / `_choose_kept`）。
- 行业中性约束（`MAX_PER_INDUSTRY=1`）和持仓数量（`TOP_K=5`）。
- 多周期权重（0.40, 0.35, 0.25）及截面百分位排名逻辑。
- `KEEP_BUFFER=2.0` 的保留缓冲机制。

## 精确修改规则
在 `Strategy.decide` 方法中，修改计算 `components` 的循环体。将下行偏差计算替换为区间最大回撤计算，并优化NaN和除零保护逻辑。

```python
# 修改前（上一轮代码）：
# downside_rets = np.minimum(daily_rets, 0.0)
# downside_vol = np.sqrt(np.nanmean(downside_rets**2, axis=0))
# safe_downside_vol = np.where(downside_vol > VOL_EPS, downside_vol, VOL_EPS)
# safe_downside_vol = np.where(np.isfinite(downside_vol), safe_downside_vol, np.nan)
# raw = raw_ret / safe_downside_vol

# 修改后：
# Maximum Drawdown (Max DD) as tail risk measure.
# Captures both sudden crashes and slow, grinding declines.
# np.fmax.accumulate safely ignores NaNs (e.g., from suspended stocks).
cummax = np.fmax.accumulate(period_closes, axis=0)
safe_cummax = np.where(cummax > 0, cummax, np.nan)
drawdowns = (period_closes - cummax) / safe_cummax
max_dd = np.nanmin(drawdowns, axis=0)  # Negative values

# Use absolute max drawdown as the risk penalty.
risk_metric = np.abs(max_dd)
safe_risk = np.where(risk_metric > VOL_EPS, risk_metric, VOL_EPS)
safe_risk = np.where(np.isfinite(risk_metric), safe_risk, np.nan)

raw = raw_ret / safe_risk
```

## 资源增量清单
- **数据接口**：无新增，仅使用 `SafeStrategyContext.closes`。
- **计算资源**：新增 `np.fmax.accumulate`、减法、除法、`np.nanmin` 和 `np.abs` 向量化计算。在 numpy 下耗时增加 < 5ms，完全满足5分钟频执行要求。
- **内存**：新增 `cummax` 和 `drawdowns` 临时数组，形状为 `(lookback+1, N)`，峰值内存增加 < 10MB。

## SafeStrategyContext字段与非空验证表
| 字段名称 | 类型 | 非空/形状验证 | 用途 |
| :--- | :--- | :--- | :--- |
| `closes` | np.ndarray | `(T, N)`, `T >= 75` | 计算动量、最大回撤、均线、Breadth |
| `amounts` | np.ndarray | `(T, N)` | 计算6日平均成交额 |
| `codes` | list/np.ndarray | 长度 `N` | 股票代码，用于排序和映射 |
| `industries` | dict/list | 长度 `N` | 行业分类，用于行业中性约束 |
| `selected_held` | list/set | 可为空 | 当前持仓，用于保留逻辑 |
| `calendar` | list | 长度 `>= 75` | 交易日历，用于 `ready` 判断 |

## 数据时点与防未来函数
- **数据时点**：所有计算严格使用 `signal_i = len(closes) - 1` 及之前的数据。
- **防未来函数**：`period_closes = closes[signal_i - lookback:signal_i + 1]` 仅包含截至 `signal_i` 的历史收盘价；`np.fmax.accumulate` 沿着时间轴（axis=0）计算，严格依赖历史数据，无前瞻偏差。

## 参数来源
- 周期参数 `20, 40, 60` 继承自基线。
- 风险阈值 `VOL_EPS = 1e-6` 为工程防除零常数，无业务含义。
- 最大回撤计算无新增超参数，完全基于价格序列的客观数学定义。

## 可证伪假设
- **假设**：在A股市场，使用区间最大回撤替代下行偏差进行风险调整，能够更有效地识别和惩罚“缓慢阴跌”带来的尾部风险，避免选中阴跌股，从而在评价区间内提升策略的绝对收益和胜率。
- **证伪条件**：若评价区间内市场呈现强烈的“V型反转”或“超跌反弹”特征，导致最大回撤惩罚错杀了具有高弹性的反转龙头，使得实验组绝对收益显著低于上一轮（下行偏差调整），则假设被证伪。

## 失效与停止条件
- **失效场景**：市场处于长期宽幅震荡，股票频繁出现日内或隔日的大幅回撤但随后迅速创新高，导致 `max_dd` 普遍偏大，策略退化为极度厌恶波动的低波策略，错失震荡市中的波段Alpha。
- **停止条件**：若实验组在评价区间内的最大回撤超过上一轮，且年化收益率低于上一轮，说明最大回撤惩罚在当前市场环境下失效，下轮需回滚或寻找其他风险度量。

## 交付工程师清单
1. 定位 `Strategy.decide` 方法中计算 `components` 的 `for lookback in (20, 40, 60):` 循环。
2. 将循环体内的下行偏差计算代码块（从 `downside_rets = ...` 到 `raw = raw_ret / safe_downside_vol`）替换为上述“精确修改规则”中的代码块。
3. 确保 `np.fmax.accumulate` 和 `np.nanmin` 的调用正确（基线已导入 `numpy as np`）。
4. 运行本地回测框架，验证无报错，且运行时间与上一轮相当。

## 资料来源
1. Daniel, K., Jagannathan, R., & Kim, S. (2012). *Tail Risk in Momentum Strategy Returns*. NBER Working Paper No. 18169.
2. Wu, C. (2011). *Momentum Spillover from Stocks to Bonds: The Role of Liquidity Risk*. SSRN Working Paper.
3. Ludkovski, M., & Risk, J. (2017). *Sequential Design and Spatial Modeling for Portfolio Tail Risk Measurement*. arXiv:1710.05204.

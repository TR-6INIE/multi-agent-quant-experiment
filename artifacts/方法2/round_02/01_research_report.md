# 策略研究员增量改进方案

## 基本工作
本轮研究针对基线策略 `broad_industry_neutral_momentum_local_v1`（已包含上一轮“总波动率风险调整”改进）进行机制诊断与深化设计。通过复盘上一轮评价区间（2025Q1）的表现，识别总波动率惩罚在A股特定环境下的局限性，并结合决策日（2025-03-31）前的公开文献，对比多个候选改进方案，最终选定一项核心机制改进交付工程师实施。

## 基线策略拆解
当前基线策略是一个基于多周期风险调整动量和市场宽度的日频选股、5分钟频执行的量化策略：
1. **信号生成**：计算20日、40日、60日收益率，并除以对应周期的日收益率总标准差（Total Volatility）进行风险调整，按0.40、0.35、0.25加权得到综合得分。
2. **过滤条件**：上市及有效数据>=61天，收盘价>20日均线，6日平均成交额>=5000万。
3. **仓位控制（市场宽度 Breadth）**：基于全市场收盘价大于20日均线的股票比例进行仓位管理（<0.50清仓，>=0.58满仓调仓，中间区间仅保留）。
4. **组合构建**：选取TOP 5，行业中性（每行业最多1只），带有2.0倍的保留缓冲（`KEEP_BUFFER`）。

## 基线问题诊断
基于上一轮结构化复盘与回测数据分析，当前基线存在以下核心问题：
1. **波动率度量的“错杀”效应**：上一轮引入的总波动率（`np.nanstd`）无法区分上行波动与下行波动。在A股T+1和涨跌停板制度下，强势龙头股往往伴随极高的上行波动（如连续涨停），总波动率惩罚会显著降低这些高弹性标的的得分，导致策略在结构性牛市中错失领涨主线。
2. **高频调仓与摩擦成本**：2月中旬策略出现连续高频换仓，部分标的仅持有1-2日。虽然基线有 `KEEP_BUFFER=2.0`，但在总波动率扰动下，得分微小变化仍易触发换仓，实盘中滑点和冲击成本将严重侵蚀收益。
3. **下行风险识别不足**：A股的真实风险往往集中在向下跳空、连续阴跌或跌停板导致的流动性枯竭。总标准差对极端下行风险的刻画不够敏锐。

## 外部证据
- **Dobrynskaya (2014) "Does Momentum Trading Generate Extra Downside Risk?"**：指出动量策略在市场崩溃时表现不佳，具有正的下行市场Beta。动量收益实质上是对下行风险的补偿，而非纯粹的Alpha。
- **Dobrynskaya (2015) "Upside and Downside Risks in Momentum Returns"**：进一步证明赢家组合具有更高的相对下行风险暴露。在构建动量组合时，剥离或惩罚下行风险（Downside Risk）能够显著改善组合的风险调整后收益。
- **Ang, Chen, & Xing (2001) "Downside Risk and the Momentum Effect"**：经典文献，证实了下行的特质风险（Idiosyncratic Downside Risk）与动量效应密切相关，高下行风险的股票在未来往往表现不佳。

*证据局限*：上述文献主要基于美股等成熟市场，且多关注系统性下行风险（Downside Beta）。本改进将其微观化，应用于个股的特质下行波动率（Downside Deviation）。A股存在涨跌停板制度，个股的下行波动往往伴随着收益率截断（Censoring）和流动性丧失，这可能导致下行波动率的计算存在一定失真，需通过实验验证其在A股的有效性。

## 候选增量改进比较
| 候选方案 | 机制描述 | 优点 | 缺点 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| **1. 下行风险调整动量** | 将总标准差替换为下行偏差（Downside Deviation, MAR=0），仅惩罚负收益波动。 | 精准惩罚下跌风险，保留强势股的上行波动溢价；逻辑严密，计算量极小。 | 在“高下行波动=高反弹”的超跌反弹行情中可能失效。 | **选定** |
| **2. 换手率惩罚机制** | 在 `_choose_desired` 中为已持仓股票引入动态得分加成。 | 直接降低高频换仓带来的摩擦成本。 | 基线已有 `KEEP_BUFFER=2.0`，进一步加成易导致持仓僵化，且参数难以设定，容易过拟合。 | 放弃 |
| **3. 涨跌停流动性过滤** | 剔除近期存在连续一字涨跌停的股票。 | 避免买入无法成交或卖出时流动性枯竭的标的。 | `SafeStrategyContext` 日频模式仅提供 `closes` 和 `amounts`，缺乏高低价（High/Low）数据，无法准确识别一字涨跌停，强行实现会引入逻辑漏洞。 | 放弃 |

## 本轮唯一选定改进：下行风险调整动量（Downside Risk-Adjusted Momentum）
将基线中的总波动率（Total Volatility）替换为下行偏差（Downside Deviation）。通过仅计算负收益的半方差，策略将精准惩罚具有下行风险的标的，同时对上行波动（如连板带来的正收益）保持宽容，从而在控制回撤的同时捕捉A股特有的动量弹性。

## 明确保留项
- 股票池过滤逻辑（历史天数、均线、成交额）。
- 市场宽度（Breadth）计算及仓位控制逻辑（EXIT/ENTRY 阈值，`_choose_desired` / `_choose_kept`）。
- 行业中性约束（`MAX_PER_INDUSTRY=1`）和持仓数量（`TOP_K=5`）。
- 多周期权重（0.40, 0.35, 0.25）及截面百分位排名逻辑。
- `KEEP_BUFFER=2.0` 的保留缓冲机制。

## 精确修改规则
在 `Strategy.decide` 方法中，修改计算 `components` 的循环体。将总标准差 `np.nanstd` 替换为下行偏差计算，并优化除零保护逻辑，确保无下行波动的强势股不会被误杀。

```python
# 修改前（上一轮代码）：
# vol = np.nanstd(daily_rets, axis=0)
# raw = raw_ret / np.where(vol > VOL_EPS, vol, np.nan)

# 修改后：
# 计算下行偏差 (Downside Deviation)，最小可接受收益率(MAR)设为0
downside_rets = np.minimum(daily_rets, 0.0)
downside_vol = np.sqrt(np.nanmean(downside_rets**2, axis=0))

# 防止除零：当无下行波动时，使用 VOL_EPS 作为分母，放大正向收益得分
safe_downside_vol = np.where(downside_vol > VOL_EPS, downside_vol, VOL_EPS)
raw = raw_ret / safe_downside_vol
```

## 资源增量清单
- **数据接口**：无新增，仅使用 `SafeStrategyContext.closes`。
- **计算资源**：新增 `np.minimum`、平方、`np.nanmean` 和开方向量化计算。在 numpy 下耗时增加 < 5ms，完全满足5分钟频执行要求。
- **内存**：新增 `downside_rets` 临时数组，峰值内存增加 < 5MB。

## SafeStrategyContext字段与非空验证表
| 字段名称 | 类型 | 非空/形状验证 | 用途 |
| :--- | :--- | :--- | :--- |
| `closes` | np.ndarray | `(T, N)`, `T >= 75` | 计算动量、下行波动率、均线、Breadth |
| `amounts` | np.ndarray | `(T, N)` | 计算6日平均成交额 |
| `codes` | list/np.ndarray | 长度 `N` | 股票代码，用于排序和映射 |
| `industries` | dict/list | 长度 `N` | 行业分类，用于行业中性约束 |
| `selected_held` | list/set | 可为空 | 当前持仓，用于保留逻辑 |
| `calendar` | list | 长度 `>= 75` | 交易日历，用于 `ready` 判断 |

## 数据时点与防未来函数
- **数据时点**：所有计算严格使用 `signal_i = len(closes) - 1` 及之前的数据。
- **防未来函数**：波动率计算切片 `closes[signal_i - lookback:signal_i + 1]` 仅包含截至 `signal_i` 的历史收盘价；`daily_rets` 计算使用 `[:-1]` 和 `[1:]`，无前瞻偏差。

## 参数来源
- 周期参数 `20, 40, 60` 继承自基线。
- 波动率阈值 `VOL_EPS = 1e-6` 为工程防除零常数，无业务含义。
- 下行偏差的 MAR (Minimum Acceptable Return) 固定为 0.0，符合金融学标准定义，无新增超参数。

## 可证伪假设
- **假设**：在A股市场，使用下行偏差替代总标准差进行风险调整，能够保留强势股的上行波动溢价，同时有效规避下行风险，从而在评价区间内提升策略的绝对收益和夏普比率。
- **证伪条件**：若评价区间内市场呈现“高下行波动=高反弹收益”的超跌反弹特征（如微盘股暴跌后V型反转），导致下行风险调整错失反弹龙头，使得实验组绝对收益和夏普比率均显著低于上一轮（总波动率调整），则假设被证伪。

## 失效与停止条件
- **失效场景**：市场处于长期阴跌且无明显下行波动（每日跌幅极小但持续），导致 `downside_vol` 趋近于0，得分失真（尽管有 `VOL_EPS` 保护，但排名可能退化为纯动量）。
- **停止条件**：若实验组在评价区间内的最大回撤超过上一轮，且年化收益率低于上一轮，则说明下行风险调整在当前市场环境下失效，下轮需回滚至总波动率或纯动量。

## 交付工程师清单
1. 定位 `Strategy.decide` 方法中计算 `components` 的 `for lookback in (20, 40, 60):` 循环。
2. 将循环体内的 `vol = np.nanstd(daily_rets, axis=0)` 及随后的 `raw = ...` 替换为上述“精确修改规则”中的代码块。
3. 确保 `np.minimum` 和 `np.nanmean` 的调用正确（基线已导入 `numpy as np`）。
4. 运行本地回测框架，验证无报错，且运行时间与上一轮相当。

## 资料来源
1. Dobrynskaya, V. (2014). *Does Momentum Trading Generate Extra Downside Risk?* SSRN.
2. Dobrynskaya, V. (2015). *Upside and Downside Risks in Momentum Returns.* SSRN.
3. Ang, A., Chen, J., & Xing, Y. (2001). *Downside Risk and the Momentum Effect.* NBER Working Paper No. 8643.

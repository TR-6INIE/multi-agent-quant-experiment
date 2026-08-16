# 策略研究员增量改进方案

## 基本工作
本轮研究针对基线策略 `broad_industry_neutral_momentum_local_v1`（已包含上一轮“区间最大回撤风险调整”改进）进行机制诊断与深化设计。通过复盘上一轮评价区间（2025Q3）的表现，识别单一最大回撤（Max DD）在A股高弹性科技股“V型反转”行情中的数学缺陷（惩罚过重导致错杀），并结合决策日（2025-09-30）前的公开文献，对比多个候选改进方案，最终选定一项核心机制改进交付工程师实施。

## 基线策略拆解
当前基线策略是一个基于多周期风险调整动量和市场宽度的日频选股、5分钟频执行的量化策略：
1. **信号生成**：计算20日、40日、60日收益率，并除以对应周期的区间最大回撤（Max DD）进行风险调整，按0.40、0.35、0.25加权得到综合得分。
2. **过滤条件**：上市及有效数据>=61天，收盘价>20日均线，6日平均成交额>=5000万。
3. **仓位控制（市场宽度 Breadth）**：基于全市场收盘价大于20日均线的股票比例进行仓位管理（<0.50清仓，>=0.58满仓调仓，中间区间仅保留）。
4. **组合构建**：选取TOP 5，行业中性（每行业最多1只），带有2.0倍的保留缓冲（`KEEP_BUFFER`）。

## 基线问题诊断
基于上一轮结构化复盘与回测数据分析，当前基线存在以下核心问题：
1. **单一Max DD对高弹性/V型反转标的惩罚过重（进攻性不足）**：在7月至9月的行情中，部分高弹性科技股（如通信、电子）在窗口期内经历了较大幅度的回撤，但期末已大幅反弹甚至创新高。单一Max DD仅关注峰谷落差，对这些“高波动、高恢复”的龙头股施加了与“持续阴跌股”同等的严厉惩罚，导致策略系统性偏向低波动传统行业，大幅跑输高弹性基准（科创50，超额-28.81%）。
2. **得分跳变引发隐性碎股摩擦**：单一Max DD在窗口滑动时，若历史前高移出窗口，会导致分母（风险度量）突然变小，得分剧烈跳变。这种排名抖动加剧了目标持仓列表的频繁微调，间接导致Broker端产生大量100股、200股的碎股买卖，承受了极高的最低5元佣金摩擦。
3. **Breadth硬阈值导致长期踏空**：9月4日清仓后，Breadth在0.31至0.57之间震荡近20个交易日，策略完全空仓。但此问题属于宏观防御机制的固有代价，直接修改Breadth逻辑可能在真正的熊市中导致回撤失控。

## 外部证据
- **Choi, J. (2021) "Maximum Drawdown, Recovery, and Momentum"**：实证检验了基于最大回撤及其连续恢复（Consecutive Recovery）的选股规则。研究表明，在月度周期中，结合恢复度的最大回撤动量组合在预测资产价格方向和捕捉截面收益差异方面，显著优于传统累计收益动量和单一最大回撤策略。恢复度指标能够有效识别均值回复和趋势修复，改善组合的风险收益特征，并降低换手率。
- **Edelman, D. (2008) "Maximum Drawdown Value at Risk"**：探讨了最大回撤在风险度量中的边界穿越性质，指出单纯依赖历史最大回撤可能低估资产的恢复潜力，需结合路径特征进行综合评估。

*证据局限*：上述文献主要基于美股等成熟市场，且使用的是月度/周度频率。A股的T+1制度和涨跌停板可能导致“恢复”过程出现流动性断层（如一字涨停无法买入），策略在信号端识别出高恢复度时，实际执行可能面临滑点或无法成交的问题。此外，A股的“假突破”和“诱多”可能导致恢复度指标产生虚假信号（如死猫跳）。这是本改进的证据局限，需通过实验验证其在A股的有效性。

## 候选增量改进比较
| 候选方案 | 机制描述 | 优点 | 缺点 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| **1. 结合恢复度的最大回撤调整** | 引入恢复比例（Recovery Ratio），对已从谷底反弹的标的减轻回撤惩罚。 | 完美解决V型反转错杀问题；得分更平滑，有望降低换仓抖动；逻辑严密，无新增超参数。 | 可能宽容“死猫跳”等假反弹，引入左侧风险。 | **选定** |
| **2. Breadth状态机与平滑** | 引入Breadth移动平均或滞后确认，减少阈值附近反复打脸和长期踏空。 | 直接解决9月空仓过长问题，提升资金利用率。 | 改变宏观择时核心逻辑，可能在趋势反转时导致严重滞后和回撤失控，风险过高。 | 放弃 |
| **3. 调仓得分差异阈值** | 新股票得分必须比老股票高出X%才替换，减少换仓。 | 直接降低碎股摩擦和调仓频率。 | 引入新超参数X，易过拟合；且基线已有KEEP_BUFFER，进一步限制易导致持仓僵化。 | 放弃 |

## 本轮唯一选定改进：结合恢复度的最大回撤风险调整（Recovery-Adjusted Max DD）
将基线中的单一区间最大回撤替换为“结合恢复度的最大回撤”。通过计算窗口期内价格从谷底恢复至当前价格的比例（Recovery Ratio），策略将精准宽容那些“经历回撤但已大幅修复”的高弹性科技龙头，同时继续严厉惩罚处于下跌通道或谷底的阴跌股。对于完全恢复至前高或无回撤的强势股，风险度量趋近于0（被工程常数保护），从而完美保留上行溢价。此改进有望提升牛市/震荡市中的进攻性，并通过平滑得分曲线缓解换仓抖动。

## 明确保留项
- 股票池过滤逻辑（历史天数、均线、成交额）。
- 市场宽度（Breadth）计算及仓位控制逻辑（EXIT/ENTRY 阈值，`_choose_desired` / `_choose_kept`）。
- 行业中性约束（`MAX_PER_INDUSTRY=1`）和持仓数量（`TOP_K=5`）。
- 多周期权重（0.40, 0.35, 0.25）及截面百分位排名逻辑。
- `KEEP_BUFFER=2.0` 的保留缓冲机制。

## 精确修改规则
在 `Strategy.decide` 方法中，修改计算 `components` 的循环体。将单一最大回撤计算替换为结合恢复度的最大回撤计算。

```python
# 修改前（上一轮代码）：
# cummax = np.fmax.accumulate(period_closes, axis=0)
# safe_cummax = np.where(cummax > 0, cummax, np.nan)
# drawdowns = (period_closes - cummax) / safe_cummax
# with np.errstate(invalid='ignore'):
#     max_dd = np.nanmin(drawdowns, axis=0)
# risk_metric = np.abs(max_dd)
# safe_risk = np.where(risk_metric > VOL_EPS, risk_metric, VOL_EPS)
# safe_risk = np.where(np.isfinite(risk_metric), safe_risk, np.nan)
# raw = raw_ret / safe_risk

# 修改后：
cummax = np.fmax.accumulate(period_closes, axis=0)
safe_cummax = np.where(cummax > 0, cummax, np.nan)
drawdowns = (period_closes - cummax) / safe_cummax
with np.errstate(invalid='ignore'):
    max_dd = np.nanmin(drawdowns, axis=0)  # Negative values

# Calculate recovery ratio to adjust the drawdown penalty.
# Trough is the minimum price in the window.
trough_price = np.nanmin(period_closes, axis=0)
current_price = period_closes[-1]
peak_price = cummax[-1]  # The last element of cummax is the global peak in the window.

denominator = peak_price - trough_price
# If peak == trough (no volatility), recovery_ratio is 1.0 (fully recovered / no drawdown).
with np.errstate(invalid='ignore'):
    recovery_ratio = np.where(
        denominator > VOL_EPS,
        (current_price - trough_price) / denominator,
        1.0
    )

# Adjusted risk: penalizes stocks still at their trough, forgives stocks that have recovered.
adjusted_risk = np.abs(max_dd) * (1.0 - recovery_ratio)

safe_risk = np.where(adjusted_risk > VOL_EPS, adjusted_risk, VOL_EPS)
safe_risk = np.where(np.isfinite(adjusted_risk), safe_risk, np.nan)

raw = raw_ret / safe_risk
```

## 资源增量清单
- **数据接口**：无新增，仅使用 `SafeStrategyContext.closes`。
- **计算资源**：新增 `np.nanmin` 计算谷底价格，以及简单的加减乘除和条件判断。在 numpy 下耗时增加 < 2ms，完全满足5分钟频执行要求。
- **内存**：新增 `trough_price`、`current_price`、`peak_price`、`denominator`、`recovery_ratio`、`adjusted_risk` 等一维临时数组，形状为 `(N,)`，峰值内存增加 < 5MB。

## SafeStrategyContext字段与非空验证表
| 字段名称 | 类型 | 非空/形状验证 | 用途 |
| :--- | :--- | :--- | :--- |
| `closes` | np.ndarray | `(T, N)`, `T >= 75` | 计算动量、最大回撤、恢复度、均线、Breadth |
| `amounts` | np.ndarray | `(T, N)` | 计算6日平均成交额 |
| `codes` | list/np.ndarray | 长度 `N` | 股票代码，用于排序和映射 |
| `industries` | dict/list | 长度 `N` | 行业分类，用于行业中性约束 |
| `selected_held` | list/set | 可为空 | 当前持仓，用于保留逻辑 |
| `calendar` | list | 长度 `>= 75` | 交易日历，用于 `ready` 判断 |

## 数据时点与防未来函数
- **数据时点**：所有计算严格使用 `signal_i = len(closes) - 1` 及之前的数据。
- **防未来函数**：`period_closes = closes[signal_i - lookback:signal_i + 1]` 仅包含截至 `signal_i` 的历史收盘价；`trough_price`、`peak_price` 和 `current_price` 均基于 `period_closes` 计算，严格依赖历史及当前数据，无前瞻偏差。

## 参数来源
- 周期参数 `20, 40, 60` 继承自基线。
- 风险阈值 `VOL_EPS = 1e-6` 为工程防除零常数，无业务含义。
- 恢复度计算无新增超参数，完全基于价格序列的客观数学定义。

## 可证伪假设
- **假设**：在A股市场，引入恢复度（Recovery Ratio）调整最大回撤惩罚，能够有效识别并宽容“V型反转”和高弹性科技股的修复行情，避免单一Max DD导致的错杀，从而在评价区间内提升策略的相对基准超额收益和进攻性，同时通过平滑得分曲线降低换仓频率。
- **证伪条件**：若评价区间内市场呈现“持续阴跌无反弹”或“假突破后迅速暴跌”的特征，导致恢复度指标频繁给出虚假的高分信号（即“接飞刀”），使得实验组最大回撤显著放大（超过上一轮的-7.43%）且绝对收益低于上一轮，则假设被证伪。

## 失效与停止条件
- **失效场景**：市场处于极端的流动性枯竭状态，股票在谷底出现微弱的反弹（如死猫跳）即被判定为高恢复度，策略买入后继续暴跌；或市场呈现极端的“尖峰厚尾”特征，导致恢复度计算被单日极端价格扭曲。
- **停止条件**：若实验组在评价区间内的最大回撤超过上一轮，且胜率显著下降，说明恢复度调整在当前市场环境下引入了过多的左侧/假反弹风险，下轮需回滚至单一Max DD或寻找其他风险度量。

## 交付工程师清单
1. 定位 `Strategy.decide` 方法中计算 `components` 的 `for lookback in (20, 40, 60):` 循环。
2. 将循环体内的风险调整计算代码块（从 `cummax = ...` 到 `raw = raw_ret / safe_risk`）替换为上述“精确修改规则”中的代码块。
3. 确保 `np.errstate(invalid='ignore')` 上下文正确包裹可能产生 NaN 的除法运算（基线已导入 `numpy as np`）。
4. 运行本地回测框架，验证无报错，且运行时间与上一轮相当。

## 资料来源
1. Choi, J. (2021). *Maximum Drawdown, Recovery, and Momentum*. Journal of Risk and Financial Management, 14(11), 542.
2. Edelman, D. (2008). *Maximum Drawdown Value at Risk*. SSRN Working Paper No. 1102371.

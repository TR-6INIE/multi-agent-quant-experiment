# 策略研究与增量改进方案

## 基本工作
本轮作为策略研究员，对基线策略 `broad_industry_neutral_momentum_local_v1` 进行了机制拆解与风险诊断。结合截至2024-12-31的公开文献，识别了基线在市场状态识别（Regime Switching）上的硬阈值缺陷，并设计了基于市场宽度趋势平滑的增量改进方案。

## 基线策略拆解
基线策略是一个**日频信号、5分钟执行（09:35）的截面动量策略**，核心机制如下：
1. **因子构建**：计算20日、40日、60日截面动量（百分位排名），按0.4:0.35:0.25加权。
2. **基础过滤**：剔除上市不足61天、收盘价低于20日均线、近6日日均成交额低于5000万的股票。
3. **市场宽度（Breadth）风控**：计算全市场收盘价高于20日均线的股票比例。
   - `breadth < 0.50`：触发清仓（EXIT）。
   - `breadth >= 0.58`：触发建仓（ENTRY），选取TOP 5，且强制行业中性（单行业最多1只）。
   - `0.50 <= breadth < 0.58`：保持现有持仓。

## 基线问题诊断
1. **机制转换（Regime Switching）识别生硬**：基线使用固定的0.50和0.58作为清仓和建仓的硬阈值。在A股高波动、多震荡的市场环境中，`breadth`极易在阈值附近频繁穿越，导致“刚清仓即反弹、刚建仓即下跌”的锯齿效应（Whipsaw），产生严重的交易摩擦和净值磨损。
2. **缺乏趋势确认**：单日`breadth`仅反映当天的静态截面状态，未包含市场宽度的动态演变趋势。单日的情绪扰动（如突发利好/利空）容易导致错误的机制误判。
3. **动量崩溃（Momentum Crash）风险**：纯截面动量在市场从熊市快速反弹时，容易买入前期抗跌但反弹乏力的“假赢家”，遭遇动量崩溃（文献5、6）。

## 外部证据
- **机制转换理论（Regime Switching）**：文献7-12（如Assoe 1998, Kumar 2013）指出金融市场存在显著的牛熊机制转换，且不同机制下的资产定价和波动率特征截然不同。最优控制与风险管理需要基于状态依赖（State-dependent）的平滑过渡，而非离散硬切。
- **动量崩溃与宏观状态**：文献5、6（Avramov & Hore, 2015）证明动量策略在经济复苏期（熊市反弹）会遭遇崩溃，因为输家股票的消费Beta上升更快。这要求市场状态过滤器必须具备抗噪和趋势确认能力。
- **证据局限性声明**：检索到的Regime Switching文献多基于宏观变量、期权隐含波动率或货币政策构建隐马尔可夫模型（HMM），**并无直接针对“技术面市场宽度（Breadth）移动平均”的学术实证**。将宏观机制转换思想迁移到技术面宽度平滑上属于合理外推，需通过回测验证其在A股的有效性。

## 候选增量改进比较
| 候选方案 | 核心机制 | 优点 | 缺点 | 结论 |
| :--- | :--- | :--- | :--- | :--- |
| **A. 市场宽度趋势平滑** | 引入Breadth的10日均线，结合单日Breadth进行双条件机制确认。 | 直接解决基线硬阈值导致的频繁交易问题，逻辑清晰，计算开销极小。 | 在极高频V型反转（周期<10天）中会产生信号滞后。 | **选定** |
| **B. 波动率调整动量** | 将动量收益率除以过去20日波动率，降低高波动股票权重。 | 缓解个股层面的动量崩溃风险，提升因子信噪比。 | 未解决基线最核心的仓位管理（Breadth）硬切问题。 | 放弃 |
| **C. 执行时点后移** | 将09:35改为10:00或下午，避开开盘高波动（文献16）。 | 理论上降低日内流动性冲击。 | 本轮回测滑点设为0，改时点对净值无实质影响，且可能错过日内趋势。 | 放弃 |

## 本轮唯一选定改进
**市场宽度趋势平滑（Regime-aware Breadth Filter）**
将基线的单日`breadth`硬阈值判断，升级为**单日宽度与宽度移动平均（10日）相结合的双条件机制转换判断**。只有当宽度突破阈值且顺应其短期趋势时，才执行仓位切换，从而有效过滤震荡市中的假信号。

## 明确保留项
- 股票池、行业分类快照、数据频率（日频信号/5分钟执行）。
- 动量因子计算逻辑（20/40/60天加权及截面百分位）。
- 行业中性约束（`MAX_PER_INDUSTRY=1`）及选股逻辑（`_choose_desired`, `_choose_kept`）。
- 基础过滤条件（历史天数、20日均线、5000万成交额）。
- 初始资金50万，期初持仓为空，评价区间2025-01-01至2025-03-31。

## 精确修改规则
在 `Strategy.decide` 方法中，替换原有的 `breadth` 判断逻辑：
1. 计算过去10个交易日每天的 `breadth` 值，求其均值 `breadth_ma`。
2. **空头机制（清仓）**：`breadth < EXIT_BREADTH` **且** `breadth < breadth_ma`。
3. **多头机制（建仓）**：`breadth >= ENTRY_BREADTH` **且** `breadth > breadth_ma`。
4. **过渡机制（保持）**：不满足上述两个条件时，执行 `_choose_kept`。

## 资源增量清单
- **计算资源**：增加过去10天 `ma20` 和 `breadth` 的循环计算。由于 `closes` 数组已在内存中，且仅涉及简单的布尔运算和均值，时间复杂度增加可忽略不计（$O(10 \times N)$）。
- **内存资源**：新增一个长度为10的浮点数列表 `breadths`，无显著内存压力。

## SafeStrategyContext字段与非空验证表
| 字段名称 | 类型 | 非空/长度验证 | 用途 |
| :--- | :--- | :--- | :--- |
| `context.closes` | `np.ndarray` | `shape[0] >= 75`, `shape[1] > 0` | 计算动量、均线、历史Breadth |
| `context.amounts` | `np.ndarray` | `shape[0] >= 6`, `shape[1] > 0` | 计算6日平均成交额 |
| `context.codes` | `list/np.ndarray` | `len > 0` | 股票池标识 |
| `context.industries`| `list/np.ndarray` | `len == len(codes)` | 行业中性约束 |
| `context.selected_held`| `set/list` | 允许为空 | 识别当前持仓 |
| `context.calendar` | `list` | `len >= 75` | `ready()` 方法验证 |

## 数据时点
- **信号计算**：`signal_i = len(closes) - 1`，即上一交易日收盘后。
- **历史Breadth计算**：严格使用 `closes[i-19:i+1]`，不包含 `i` 之后的任何数据。
- **订单执行**：下一交易日 09:35（5分钟K线开盘）。

## 防未来函数
- 历史 `breadth` 循环的索引 `i` 严格限制在 `signal_i` 及之前。
- `ma20_i` 的计算窗口为 `[i-19, i]`，完全基于历史收盘价。
- 所有截面排名（`_percentile`）仅使用 `signal_i` 当天的有效数据。

## 参数来源
- `BREADTH_MA_WINDOW = 10`：基于经验与短期机制转换的常见窗口（1-2周），平衡信号灵敏度与抗噪性。
- `EXIT_BREADTH = 0.50`, `ENTRY_BREADTH = 0.58`：继承基线参数，保持策略风格一致性。

## 可证伪假设
- **假设**：市场宽度的短期趋势（10日均线）能有效过滤单日噪声，减少震荡市中的频繁仓位切换，从而提升风险调整后收益。
- **证伪条件**：如果评价期内市场呈现极高频的V型反转（周期<10天），该平滑机制会导致信号滞后，使得策略在快速反弹中踏空，或在快速下跌中未能及时止损，导致净值表现劣于基线。

## 失效与停止条件
- **失效场景**：市场处于极窄幅震荡，且 `breadth` 在0.50和0.58之间频繁穿越其10日均线，此时双条件判断仍会产生一定的切换磨损。
- **停止条件**：若连续5个交易日因机制判断滞后导致单笔交易亏损超过3%，或评价期内最大回撤超过基线20%以上，需在下一轮重新评估窗口期或引入更复杂的隐马尔可夫模型（HMM）。

## 交付工程师清单
1. 在 `Strategy.decide` 中，`breadth` 计算完成后，新增 `BREADTH_MA_WINDOW = 10` 的历史宽度计算逻辑。
2. 处理边界条件：当 `signal_i < 19` 时（虽然 `ready` 已保证 `>=75`，但需防御），确保 `start_idx = max(19, signal_i - BREADTH_MA_WINDOW + 1)`。
3. 替换原有的 `if breadth < EXIT_BREADTH:` 等三分支逻辑，改为基于 `is_bear_regime` 和 `is_bull_regime` 的双条件判断。
4. 确保 `breadth_ma` 在遇到全NaN时的Fallback处理（如回退到当日 `breadth`）。

## 资料来源
1. Assoe, K. G. (1998). *Regime-Switching in Emerging Stock Market Returns*.
2. Avramov, D., & Hore, S. (2015). *Cross-Sectional Factor Dynamics and Momentum Returns*.
3. Kumar, A. (2013). *Monetary Transmission to Stock Market in India: 'A Regime Switching Approach'*.
4. Mielkie, M., & Davison, M. (2013). *Investigating the Market Price of Volatility Risk for Options in a Regime-Switching Market*.
5. Tian, G. G. (2006). *Trading Mechanisms and Interday and Intraday Volatility in an Order-Driven Market with a Separate Auction Opening Procedure*.

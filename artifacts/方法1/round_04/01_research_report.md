# 基本工作

## 基线策略拆解
基线策略 `broad_industry_neutral_momentum_local_v1` 是一个结合截面动量、趋势过滤与市场宽度（Breadth）择时的中短期多头策略。
1. **选股因子**：计算20、40、60天的价格动量，按 0.4:0.35:0.25 加权得到综合动量得分。
2. **过滤条件**：上市天数≥61天、近6天平均成交额≥5000万、当前收盘价>20日均线。
3. **行业与持仓约束**：最多持有5只股票（`TOP_K=5`），每个申万一级行业最多持有1只（`MAX_PER_INDUSTRY=1`），已持仓股票排名在前10（`KEEP_BUFFER=2.0`）可保留。
4. **择时机制**：计算全市场收盘价高于20日均线的股票比例，并取过去3日的算术平均值（`breadth_3d`，代码中为 `breadth`）及当日单日值（`breadth_1d`）。
   - 入场：`breadth_3d >= 0.50` 且 `breadth_1d >= 0.58`。
   - 出场：`breadth_3d < 0.50` 或 `breadth_1d < 0.40`（恐慌清仓）时清仓。
   - 保持：其他情况保持现有持仓。

## 基线问题诊断（含上一轮归因纠正）
**上一轮归因纠正与边际行为报告**：
根据管理员复核与冻结缓存复算，2025-09-04信号日的真实数据为：`breadth_1d = 0.3367`，`breadth_3d = 0.4511`。两者均满足清仓条件（`breadth_1d < 0.40` 且 `breadth_3d < 0.50`）。因此，上一轮新增的恐慌清仓条件虽然成立，但**没有产生相对旧规则的边际行为**（旧规则 `breadth_3d < 0.50` 已足以触发清仓）。上一轮述职中将3日平滑breadth误认为单日breadth_1d，导致归因错误，本轮已严格区分并纠正。

**本轮核心问题诊断：入场确认条件过严导致长期踏空**
2025-09-05清仓后，从9月10日起 `breadth_3d` 恢复至 0.50 以上（如9月10日为0.5093），但直到季末（9月30日）策略均未再入场，最终跑输基准3.6个百分点。原因是入场条件要求 `breadth_1d >= 0.58`，而在震荡修复市中，单日宽度难以达到0.58的绝对高位。这导致策略在清仓后长期踏空，验证了上一轮方案预设的“恐慌错杀后踏空”失效场景。核心问题在于：绝对阈值 `0.58` 缺乏对市场修复初期“边际改善”的捕捉能力。

## 外部证据
1. **Shi & Zhou (2017)** 在《Wax and wane of the cross-sectional momentum and contrarian effects》中指出，中国股市的动量和反转效应随市场状态变化。在市场从恐慌修复的初期，绝对动量（要求极高的单日宽度）往往较弱，而边际改善（反转或均值回归）更为显著。
2. **Tunc & Kozat (2012)** 在《Optimal Investment Under Transaction Costs: A Threshold Rebalanced Portfolio Approach》中指出，阈值再平衡的阈值设置应适应资产的波动特征。过高的入场阈值会导致策略在状态切换后长时间偏离最优持仓，增加机会成本。
3. **Zhang et al. (2023)** 在《Macroeconomic Momentum and Cross-Sectional Equity Market》中探讨了宏观状态（如市场宽度）的边际变化（一阶导数）比绝对水平对截面选股的有效性有更强的调节作用。

*注：以上文献观点强调了市场修复初期边际改善信号的价值，以及阈值设置对机会成本的影响。*

## 候选增量改进比较
1. **候选A：相对边际改善入场确认（选定）**
   - **机制**：将入场条件中的绝对阈值 `breadth_1d >= 0.58` 修改为相对阈值 `breadth_1d >= breadth`（即单日宽度不低于3日平滑宽度）。
   - **优势**：直接解决踏空问题。当市场从底部修复时，只要单日宽度停止恶化（不低于3日均值），即可确认修复趋势并入场。无需引入复杂的状态追踪，代码改动极小。
2. **候选B：清仓后冷却期与降级入场**
   - **机制**：清仓后N天内，将入场阈值降至0.50。
   - **劣势**：需要引入状态变量（记录清仓后的天数），在当前的无状态日频框架下，需要修改 `Strategy` 类的初始化与决策方法，增加状态维护的复杂度和潜在的跨季重置风险。且N的取值容易陷入过拟合。

## 本轮唯一选定改进
**实施候选A：相对边际改善入场确认机制**。取消 `0.58` 的绝对入场阈值，改用 `breadth_1d >= breadth` 作为入场确认条件，以捕捉市场宽度的边际企稳或改善。

## 明确保留项
- 选股因子（20/40/60天动量加权）及权重。
- 股票池过滤条件（上市天数、成交额、20日均线趋势过滤）。
- 行业中性逻辑（`MAX_PER_INDUSTRY = 1`）与持仓数量（`TOP_K = 5`）。
- 3日平滑Breadth的计算逻辑及常规出场条件（`breadth < 0.50` 清仓）。
- 恐慌出场条件（`breadth_1d < 0.40`），尽管上季无边际贡献，但作为极端尾部风险保护予以保留。
- 执行频率（5分钟）与执行时间（09:35）。

## 精确修改规则
1. 删除文件头部的常量定义：`ENTRY_BREADTH = 0.58`。
2. 在 `decide` 方法中，将入场判断条件：
   ```python
   elif breadth >= EXIT_BREADTH and breadth_1d >= ENTRY_BREADTH:
   ```
   修改为：
   ```python
   elif breadth >= EXIT_BREADTH and breadth_1d >= breadth:
   ```
3. 其余 `if` 和 `else` 分支保持不变。

## 资源增量清单
- **数据请求**：无增量。
- **计算资源**：将常量比较改为变量比较，耗时 < 0.01ms。
- **外部接口**：无新增。

## SafeStrategyContext字段与非空验证表
| 字段名称 | 类型 | 预期维度 | 非空/有效性验证 |
| :--- | :--- | :--- | :--- |
| `closes` | np.ndarray | (Days, Stocks) | `len(closes) >= 75` (由 `ready` 保证) |
| `amounts` | np.ndarray | (Days, Stocks) | 与 `closes` 维度一致 |
| `codes` | np.ndarray | (Stocks,) | 长度 > 0 |
| `industries` | np.ndarray | (Stocks,) | 长度与 `codes` 一致 |
| `selected_held`| list | (N,) | 允许为空列表 |

## 数据时点与防未来函数
- **数据时点**：`signal_i = len(closes) - 1` 代表 T 日。`breadth` 和 `breadth_1d` 均基于 T 日及以前数据计算。
- **防未来函数**：订单在 T+1 日 09:35 执行，信号计算完全基于 T 日收盘后数据，不存在任何前视偏差。

## 参数来源
- 取消 `0.58` 的绝对阈值，改用 `breadth`（3日平滑值）作为动态阈值。物理意义：单日市场宽度不低于近3日平均水平，代表市场情绪边际企稳或改善，符合 Zhang et al. (2023) 关于宏观状态边际变化调节作用的结论。

## 可证伪假设
- **假设**：将入场确认条件改为 `breadth_1d >= breadth` 能够有效捕捉市场修复初期的边际改善，减少清仓后的踏空时间，从而在评价期内提升相对基准的超额收益，并产生相对旧规则（`breadth_1d >= 0.58`）的边际入场行为。
- **证伪条件**：若评价期内策略因 `breadth_1d >= breadth` 在阴跌反弹中频繁假突破入场，导致换手率激增且最大回撤超过上季的 -9.26%，或相对基准超额收益仍为负，则假设不成立。

## 失效与停止条件
- **失效场景**：市场呈现“单日反弹、次日继续大跌”的锯齿形阴跌，`breadth_1d >= breadth` 频繁触发假入场，随后又触发清仓，导致两头挨打。
- **停止条件**：若评价期内成交笔数 > 150笔（上季86笔），或最大回撤 > 15%，则说明相对阈值引入了过多噪音，需回退至绝对阈值或引入冷却期。

## 交付工程师清单
1. 删除文件头部常量区的 `ENTRY_BREADTH = 0.58`。
2. 定位 `decide` 方法中最终的仓位决策块。
3. 将原有的入场判断行：
   ```python
   elif breadth >= EXIT_BREADTH and breadth_1d >= ENTRY_BREADTH:
   ```
   替换为：
   ```python
   elif breadth >= EXIT_BREADTH and breadth_1d >= breadth:
   ```
4. 确保其余代码（包括出场判断、`_choose_desired` 等）保持不变。

## 资料来源
- Shi, H. -L., & Zhou, W. -X. (2017). *Wax and wane of the cross-sectional momentum and contrarian effects: Evidence from the Chinese stock markets*. arXiv:1707.05552v1.
- Tunc, S., & Kozat, S. S. (2012). *Optimal Investment Under Transaction Costs: A Threshold Rebalanced Portfolio Approach*. arXiv:1203.4156v1.
- Zhang, Y., Kappou, K., & Urquhart, A. (2023). *Macroeconomic Momentum and Cross-Sectional Equity Market*. SSRN 4627986.

---

# 绩效工作

## 改进提案：基于市场波动率期限结构的动态排名缓冲区（Volatility-Adjusted Keep Buffer）

### 逻辑依据（资料观点与本人观点）
- **资料观点**：Feng et al. (2010) 在《Transaction fees and optimal rebalancing in the growth-optimal portfolio》以及 Tunc & Kozat (2012) 指出，在交易成本存在的情况下，最优再平衡的非交易区（No-Trade Region）宽度应与资产的波动率成正比。波动率高时，扩大非交易区可以避免被噪音触发频繁交易；波动率低时，缩小非交易区以紧跟信号。
- **本人观点**：基线策略使用静态 `KEEP_BUFFER = 2.0`。上季回测显示存在碎单换仓。上一轮绩效提案试图基于“实际换手笔数”动态调整，但因 SafeStrategyContext 不提供历史成交记录而失败。本轮我提出利用 `closes` 计算全市场近期波动率的期限结构，以此作为市场摩擦和噪音的代理变量。当短期波动率显著高于长期基准时，排名边缘的股票得分差异往往是由噪音驱动的，此时扩大 KEEP_BUFFER 可以减少无意义的碎单换仓。

### 改进方案与接口映射
引入基于市场波动率期限结构的动态 `KEEP_BUFFER` 机制，**完全映射到 SafeStrategyContext 现有字段，无需新增接口**：
1. 利用 `context.closes` 计算全市场有效股票（满足 `eligible` 条件）的日收益率矩阵。
2. 计算过去 5 个交易日全市场日收益率的截面平均标准差，作为短期波动率代理（`market_vol_5d`）。
3. 计算过去 20 个交易日的同类标准差，作为长期波动率基准（`market_vol_20d`）。
4. 若 `market_vol_5d > 1.2 * market_vol_20d`（短期波动显著放大），则将 `KEEP_BUFFER` 动态扩大至 3.0（即排名前15的已持仓股票可保留）。
5. 否则，保持 `KEEP_BUFFER = 2.0`。
6. 总持仓上限 `TOP_K = 5` 和行业约束不变。

### 创新点区分
资料强调的是波动率与非交易区宽度的理论正相关；本人观点是将其落地为**基于全市场截面波动率期限结构的动态缓冲区**，巧妙避开了对账户历史成交数据的依赖，完全利用现有行情数据实现状态感知，解决了上一轮提案的可实施性矛盾。

### 风险与验证
- **风险**：高波动时扩大缓冲区可能保留弱势股，在趋势反转初期增加回撤。
- **验证指标**：评价期内，当策略处于动态扩大缓冲区状态时，单笔交易的平均名义金额应显著高于静态时期（碎单减少）；且组合的整体最大回撤不应因弱势股保留而超过基线的 -9.26%。若扩大缓冲区后总收益下降超过 2 个百分点，则说明动量衰减的代价超过了摩擦成本的节约，提案需被否决。

### 绩效工作资料来源
- Feng, Y., Medo, M., Zhang, L., & Zhang, Y.-C. (2010). *Transaction fees and optimal rebalancing in the growth-optimal portfolio*. arXiv:1009.3753v2.

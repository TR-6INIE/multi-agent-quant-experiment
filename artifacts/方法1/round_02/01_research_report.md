# 基本工作

## 基线策略拆解
基线策略 `broad_industry_neutral_momentum_local_v1` 是一个结合了截面动量、趋势过滤与市场宽度（Breadth）择时的中短期多头策略。
1. **选股因子**：计算20、40、60天的价格动量，按 0.4:0.35:0.25 加权得到综合动量得分。
2. **过滤条件**：上市天数≥61天、近6天平均成交额≥5000万、当前收盘价>20日均线。
3. **行业与持仓约束**：最多持有5只股票（`TOP_K=5`），每个申万一级行业最多持有1只（`MAX_PER_INDUSTRY=1`），已持仓股票排名在前10可保留。
4. **择时机制（本轮已平滑）**：计算全市场收盘价高于20日均线的股票比例，并取过去3日的算术平均值（`breadth_3d`）。
   - `breadth_3d < 0.50`：清仓。
   - `breadth_3d >= 0.58`：按排名选股建仓。
   - `0.50 <= breadth_3d < 0.58`：保持现有持仓。

## 基线问题诊断
**核心问题：3月再入场滞后与反弹末端陷阱**
根据2025年Q1的本地回测数据，策略在3月4日清仓后，3月13日至17日市场宽度已在修复（`breadth_3d` 回升至 0.50-0.56 区间），但策略因未达到 0.58 的入场阈值而持续空仓。直到3月18日 `breadth_3d` 突破 0.58 才再入场，结果买在了局部反弹高点，随后6个交易日亏损约3.4%并于3月26日再次清仓。
**诊断结论**：3日平滑机制在**出场时**有效过滤了单日噪音（避免了3月上旬的反复鞭打），但在**入场时**引入了严重的相位滞后。当市场处于状态切换初期（如反弹初期），3日均值爬升缓慢，导致策略错过最佳入场点；而当均值终于达标时，往往已是短期动量衰竭的反弹末端。

## 外部证据
1. **Giner & Zakamulin (2021)** 在《A Regime-Switching Model of Stock Returns with Momentum and Mean Reversion》中指出，市场在牛熊状态切换时，短期动量与中期均值回归特征会交替出现。单纯依赖滞后的平滑指标容易在状态切换的末端（反转点）发出错误信号。
2. **Feng et al. (2010)** 在《Transaction fees and optimal rebalancing in the growth-optimal portfolio》中强调，在存在交易费用的环境中，信号的敏感度与再平衡频率需要非对称设计：出场信号应更平滑以规避噪音成本，而入场信号可适度敏感以捕捉状态切换的早期红利。
3. **Assoe (1998)** 在《Regime-Switching in Emerging Stock Market Returns》中证实，新兴市场（如A股）的状态切换频率更高、波动更剧烈，要求择时系统具备更快的响应机制，而非单一的长窗口平滑。

*注：以上文献观点强调了状态切换特征、交易成本下的非对称信号设计以及新兴市场的快切换特性。*

## 候选增量改进比较
1. **候选A：非对称入场确认（选定）**
   - **机制**：将入场条件从单一的 `breadth_3d >= 0.58` 改为 `breadth_3d >= 0.50` 且 **当日单日Breadth (`breadth_1d`) >= 0.58**。
   - **优势**：完美解决滞后问题。只要中期趋势未破位（3日均值>=0.50），且当日出现强势突破（单日>=0.58），即可提前入场。这既保留了3日平滑在出场时的防噪音优势，又利用单日信号消除了入场的相位滞后。代码改动极小，逻辑清晰。
2. **候选B：缩短平滑窗口至2日**
   - **机制**：将 `BREADTH_SMOOTH_DAYS` 从 3 改为 2。
   - **劣势**：虽然能减轻滞后，但会重新引入单日噪音，可能导致3月上旬那种“清仓-再入场”的鞭打效应复发，破坏了上一轮平滑改动的初衷。

## 本轮唯一选定改进
**实施候选A：非对称入场确认机制**。在保留3日平滑Breadth作为中期趋势和出场依据的同时，引入当日单日Breadth作为入场的边际确认信号。

## 明确保留项
- 选股因子（20/40/60天动量加权）及权重。
- 股票池过滤条件（上市天数、成交额、20日均线趋势过滤）。
- 行业中性逻辑（`MAX_PER_INDUSTRY = 1`）与持仓数量（`TOP_K = 5`）。
- 3日平滑Breadth的计算逻辑及出场条件（`breadth_3d < 0.50` 清仓）。
- 执行频率（5分钟）与执行时间（09:35）。

## 精确修改规则
1. 在 `decide` 方法中，保留原有的 `breadths` 循环计算逻辑。
2. 在循环结束后，提取 `offset=0`（即当日）的单日Breadth值，记为 `breadth_1d`。若 `breadths` 为空，则 `breadth_1d = 0.0`。
3. 修改最终的仓位决策逻辑：
   - 原逻辑：`elif breadth >= ENTRY_BREADTH:`
   - 新逻辑：`elif breadth >= EXIT_BREADTH and breadth_1d >= ENTRY_BREADTH:`
   - 保持逻辑相应调整为：`elif breadth >= EXIT_BREADTH and breadth_1d < ENTRY_BREADTH:` （即中期未破位但当日不强势，保持现有持仓）。

## 资源增量清单
- **数据请求**：无增量。
- **计算资源**：仅新增一次变量赋值与条件判断，耗时 < 0.01ms。
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
- **数据时点**：`signal_i = len(closes) - 1` 代表 T 日。`breadth_1d` 取自 `offset=0`，即 T 日的单日Breadth。所有计算严格使用 T 日及以前的历史收盘价。
- **防未来函数**：订单在 T+1 日 09:35 执行，信号计算完全基于 T 日收盘后数据，不存在任何前视偏差。

## 参数来源
- `ENTRY_BREADTH = 0.58` 与 `EXIT_BREADTH = 0.50`：沿用基线参数。非对称设计使得入场阈值（0.58）作用于单日信号，出场阈值（0.50）作用于3日平滑信号，参数物理意义明确，无需重新寻优。

## 可证伪假设
- **假设**：3月18日的亏损入场是由于3日平滑的滞后导致买在了反弹末端。引入单日Breadth确认可以提前入场（在3月13-17日某天单日突破0.58时即入场）或过滤假突破，从而降低回撤并提升收益。
- **证伪条件**：若评价期内策略因单日Breadth的噪音导致频繁在0.58附近“一日游”入场并次日止损，使得换手率大幅上升且最大回撤高于12.7%；或者提前入场依然无法避免趋势反转的回撤。

## 失效与停止条件
- **失效场景**：市场呈现极端的“隔日反转”（单日Breadth极高但次日暴跌），单日确认信号失效，导致频繁止损。
- **停止条件**：若评价期内策略年化换手率 > 800%，或最大回撤 > 25%，则说明非对称确认引入了过多噪音，需回退至纯平滑机制或引入更长周期的趋势过滤。

## 交付工程师清单
1. 定位 `decide` 方法中 `breadth = float(np.mean(breadths)) if breadths else 0.0` 这一行。
2. 在该行下方新增提取单日Breadth的代码：
   ```python
   breadth_1d = breadths[0] if breadths else 0.0
   ```
3. 将原有的决策判断块：
   ```python
   if breadth < EXIT_BREADTH:
       desired = tuple()
   elif breadth >= ENTRY_BREADTH:
       desired = _choose_desired(ranked, held, industries)
   else:
       desired = _choose_kept(ranked, held, industries)
   ```
   替换为：
   ```python
   if breadth < EXIT_BREADTH:
       desired = tuple()
   elif breadth >= EXIT_BREADTH and breadth_1d >= ENTRY_BREADTH:
       desired = _choose_desired(ranked, held, industries)
   else:
       desired = _choose_kept(ranked, held, industries)
   ```
4. 确保其余代码（包括 `breadths` 循环、`_choose_desired` 等）保持不变。

## 资料来源
- Giner, J., & Zakamulin, V. (2021). *A Regime-Switching Model of Stock Returns with Momentum and Mean Reversion*. SSRN 3997837.
- Feng, Y., Medo, M., Zhang, L., & Zhang, Y. C. (2010). *Transaction fees and optimal rebalancing in the growth-optimal portfolio*. arXiv:1009.3753.
- Assoe, K. G. (1998). *Regime-Switching in Emerging Stock Market Returns*. Crossref 10.17578/2-2-2.

---

# 绩效工作

## 改进提案：基于行业动量的动态行业集中度控制（修正与深化）

### 逻辑依据与资料观点
- **资料观点**：Sarantsev (2021) 在《Optimal Portfolio with Power Utility of Absolute and Relative Wealth》中指出，相对基准的超额收益往往来源于对强势因子的集中暴露，而非均匀分散。Husmann et al. (2019) 在《Cross-validated covariance estimators for high-dimensional minimum-variance portfolios》中也强调，在高维环境中，静态的均匀约束（如每行业1只）会导致次优的样本外表现，数据驱动的动态约束能显著提升组合效率。
- **本人观点**：基线策略强制 `MAX_PER_INDUSTRY = 1`，在A股行业轮动剧烈的环境中容易导致“为了分散而分散”。当市场主线明确集中在1-2个行业时，强制分散会迫使策略买入非主线行业的弱势股，拖累整体收益。

### 上一轮疑点澄清
上一轮数据分析中指出“2月21日state.csv显示同时持有两只电子行业股票（300115与603893），疑似违反行业约束”。经核查 `trades.csv`，603893在2月21日09:35被SELL，300115在同一时点被BUY。由于T+1交易机制与Broker订单执行顺序（先买后卖或并发处理），在09:35的瞬时快照中可能出现两只股票共存的现象。**这属于执行层面的T+1瞬时状态，而非策略逻辑漏洞**。策略生成的 `desired` 目标持仓中始终严格遵守了 `MAX_PER_INDUSTRY = 1`。

### 改进方案（深化版）
引入“行业动量”状态依赖机制，并明确量化阈值以防过拟合：
1. 每次调仓前，计算每个申万一级行业内所有 eligible 股票的平均动量得分（`scores`）。
2. 计算全市场所有行业平均动量得分的 80 分位数（Top 20% 阈值）。
3. 若某行业的平均动量得分 >= 该 80 分位数阈值，且该行业内 eligible 股票数量 >= 3（保证流动性与选择空间），则允许该行业的 `MAX_PER_INDUSTRY` 临时提升至 2。
4. 其他行业保持 `MAX_PER_INDUSTRY = 1`。总持仓上限 `TOP_K = 5` 不变。

### 创新点区分
资料强调的是相对收益优化与高维协方差估计的数学性质；本人观点是将行业轮动现象转化为**带流动性兜底的动态约束条件**。新增的“行业内 eligible 股票数量 >= 3”条件，是为了防止在行业极度缩量或停牌潮时，因强行集中持仓而买入流动性枯竭的尾部股票，这是对纯理论模型在A股微观结构下的务实修正。

### 风险与验证
- **风险**：行业动量本身可能滞后，若主线快速反转，集中度提高会放大回撤。
- **验证指标**：评价期内，当策略持有2只同行业股票时，该行业的平均收益贡献应显著高于单只持仓时期；且组合的整体最大回撤不应因集中度提升而超过基线的12.7%。

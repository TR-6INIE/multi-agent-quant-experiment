# 基本工作

## 基线策略拆解
基线策略 `broad_industry_neutral_momentum_local_v1` 是一个结合了截面动量、趋势过滤与市场宽度择时的中短期多头策略。
1. **选股因子**：计算20、40、60天的价格动量，按 0.4:0.35:0.25 加权得到综合动量得分。
2. **过滤条件**：上市天数≥61天、近6天平均成交额≥5000万、当前收盘价>20日均线。
3. **行业与持仓约束**：最多持有5只股票（`TOP_K=5`），每个申万一级行业最多持有1只（`MAX_PER_INDUSTRY=1`），已持仓股票排名在前10（`KEEP_BUFFER=2.0`）可保留。
4. **择时机制**：计算全市场收盘价高于20日均线的股票比例（市场宽度 Breadth）。
   - Breadth < 0.50：清仓。
   - Breadth ≥ 0.58：按排名选股建仓。
   - 0.50 ≤ Breadth < 0.58：保持现有持仓。

## 基线问题诊断
基线策略的核心风险在于**市场宽度（Breadth）择时信号的噪音与“鞭打效应”（Whipsaw Effect）**。
Breadth 指标仅基于单日截面计算，对单日市场的极端情绪（如突发利空导致的普跌，或情绪修复导致的普涨）极为敏感。在震荡市中，Breadth 极易在 0.50 和 0.58 阈值附近频繁穿越，导致策略在“全部清仓”和“满仓买入”之间反复切换。这不仅会产生高昂的佣金和印花税（双边摩擦成本约 0.082%），还会导致策略在频繁的止损与追涨中损耗本金。

## 外部证据
- **文献9 (The Mathematics of Market Timing, 2017)** 指出，简单的市场择时策略在历史回测中的胜率往往低于50%，其收益分布呈现负偏态，主要原因是信号噪音导致频繁的错误切换。
- **文献15 (Transaction fees and optimal rebalancing, 2010)** 强调，在存在交易费用的环境中，过度敏感的再平衡信号会严重侵蚀长期复合增长率；通过平滑信号或优化再平衡周期，可以显著提升策略的实际表现。

## 候选增量改进比较
1. **候选A：Breadth 时间序列平滑（选定）**
   - **机制**：将单日 Breadth 改为过去 N 天的移动平均。
   - **优势**：直接解决择时信号噪音问题，降低无效换手率，保护本金免受摩擦成本侵蚀。逻辑清晰，代码改动局部且安全。
2. **候选B：风险调整动量（Volatility-adjusted Momentum）**
   - **机制**：在动量得分中除以历史波动率，惩罚高波动股票。
   - **劣势**：A股短期动量中，高波动往往伴随高流动性溢价和资金关注度，引入波动率惩罚可能导致错失强势龙头股，且效果在不同市场环境下不稳定。

## 本轮唯一选定改进
**实施候选A：Breadth 时间序列平滑**。将单日市场宽度指标替换为过去 3 天的移动平均值，以过滤单日情绪噪音，降低震荡市中的无效换手。

## 明确保留项
- 选股因子（20/40/60天动量加权）及权重。
- 股票池过滤条件（上市天数、成交额、20日均线趋势过滤）。
- 行业中性逻辑（`MAX_PER_INDUSTRY = 1`）与持仓数量（`TOP_K = 5`）。
- 执行频率（5分钟）与执行时间（09:35）。
- 账户初始状态（50万元，空仓，由 Harness 管理）。

## 精确修改规则
1. 在文件顶部常量区新增 `BREADTH_SMOOTH_DAYS = 3`。
2. 在 `decide` 方法中，移除原有的单日 `breadth` 计算逻辑。
3. 引入循环，计算 `signal_i` 及其前 `BREADTH_SMOOTH_DAYS - 1` 天的每日 Breadth，并取算术平均值作为最终的 `breadth`。
4. 在循环内，针对每个时间点 `t` 独立计算 20 日均线 `ma20_t` 和 60 天有效历史计数 `hist_count_t`，确保数据对齐且无未来函数。

## 资源增量清单
- **数据请求**：无增量。基线已请求过去 74 天的 `closes` 数据，足以覆盖 3 天平滑及 60 天历史计数需求。
- **计算资源**：新增 2 次 20 日均线计算和均值计算，在 NumPy 向量化操作下耗时 < 1ms，可忽略不计。
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
- **数据时点**：`signal_i = len(closes) - 1` 代表 T 日（决策日）。所有切片 `closes[t-19:t+1]` 和 `closes[start_idx:t+1]` 均满足 `t <= signal_i`，严格使用 T 日及以前的历史收盘价。
- **防未来函数**：订单在 T+1 日 09:35 执行，信号计算完全基于 T 日收盘后数据，不存在任何前视偏差。

## 参数来源
- `BREADTH_SMOOTH_DAYS = 3`：基于 A 股市场短期情绪波动特征的经验设定。3 天平滑可有效过滤单日极端行情（如政策突发导致的单日普跌），避免策略在震荡市中因单日指标穿透阈值而频繁清仓，同时相比 5 天或 10 天平滑，保留了较高的趋势反转敏感度。

## 可证伪假设
- **假设**：单日市场宽度指标噪音过大，导致策略在震荡市中产生无效的“鞭打”交易，增加摩擦成本。平滑处理能降低换手率并提升风险调整后收益。
- **证伪条件**：若评价期内市场呈现单边流畅趋势，平滑处理导致信号延迟，使得策略在趋势反转时未能及时止损，最大回撤显著高于基线；或平滑后年化换手率未出现实质性下降。

## 失效与停止条件
- **失效场景**：市场出现高频的“隔日反转”（如连续涨跌停交替），3 日平滑依然无法过滤噪音，导致仓位频繁切换。
- **停止条件**：若评价期内策略年化换手率仍 > 800%，或最大回撤 > 25%，则说明平滑天数不足或阈值体系失效，需暂停该择时模块并重新评估。

## 交付工程师清单
1. 在文件顶部常量区新增：`BREADTH_SMOOTH_DAYS = 3`。
2. 定位 `decide` 方法中 `breadth` 的计算代码块（原代码从 `breadth_valid = ...` 到 `breadth = ...`）。
3. 将该代码块替换为以下实现：
```python
        breadths = []
        for offset in range(BREADTH_SMOOTH_DAYS):
            t = signal_i - offset
            if t < 59:
                break
            start_idx = max(0, t - 59)
            hist_count_t = np.sum(np.isfinite(closes[start_idx:t + 1]), axis=0)
            ma20_t = _column_mean(closes[t - 19:t + 1])
            valid_t = (
                (hist_count_t >= 60) & np.isfinite(closes[t]) & np.isfinite(ma20_t)
            )
            if np.any(valid_t):
                b = float(np.mean(closes[t, valid_t] > ma20_t[valid_t]))
            else:
                b = 0.0
            breadths.append(b)
        
        breadth = float(np.mean(breadths)) if breadths else 0.0
```
4. 确保 `_column_mean` 函数及后续 `EXIT_BREADTH` / `ENTRY_BREADTH` 判断逻辑保持不变。

## 资料来源
- Metcalfe, G. (2017). *The Mathematics of Market Timing*. arXiv:1712.05031.
- Feng, Y., et al. (2010). *Transaction fees and optimal rebalancing in the growth-optimal portfolio*. arXiv:1009.3753.

---

# 绩效工作

## 改进提案：基于行业动量的动态行业集中度控制 (Dynamic Industry Concentration based on Industry Momentum)

### 逻辑依据与资料观点
- **资料观点**：文献7 (*Construct sparse portfolio with mutual fund's favourite stocks in China A share market*, 2023) 指出，在 A 股市场，专业投资者的超额收益主要来源于选股能力，但行业轮动和风格切换是导致组合业绩波动的核心原因。文献4 (*Trend-Following versus Cross-Sectional Momentum*, 2020) 也强调了趋势跟随与截面动量在不同市场环境下的表现差异，指出因子暴露需要适应市场状态。
- **本人观点**：基线策略强制 `MAX_PER_INDUSTRY = 1`，这在行业轮动剧烈的 A 股市场（如 2024 年初的微盘股行情或年中的红利/出海主线行情）会导致“为了分散而分散”。当市场主线明确集中在 1-2 个行业时，强制分散会迫使策略买入非主线行业的弱势股，拖累整体收益并增加尾部风险。

### 改进方案
引入“行业动量”状态依赖机制。在每次调仓前，计算每个申万一级行业的平均动量得分（或行业指数动量）。
- 如果某行业的平均动量得分处于全市场前 20%（即处于强势主线行业），则允许该行业的 `MAX_PER_INDUSTRY` 提升至 2。
- 对于其他行业，保持 `MAX_PER_INDUSTRY = 1`。
这样可以在市场主线明确时集中兵力获取超额收益，在市场散乱、无明确主线时保持分散以控制回撤。

### 创新点区分
资料强调的是基金重仓股和行业轮动现象的统计特征，本人观点是将行业轮动现象转化为**动态约束条件**，以解决基线策略中静态行业约束的僵化问题。这不仅仅是参数微调，而是引入了新的状态依赖约束机制，使组合构建逻辑具备环境自适应能力。

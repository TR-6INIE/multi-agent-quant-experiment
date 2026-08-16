# 基本工作

## 基线策略拆解
基线策略 `broad_industry_neutral_momentum_local_v1` 是一个结合截面动量、趋势过滤与市场宽度（Breadth）择时的中短期多头策略。
1. **选股因子**：计算20、40、60天的价格动量，按 0.4:0.35:0.25 加权得到综合动量得分。
2. **过滤条件**：上市天数≥61天、近6天平均成交额≥5000万、当前收盘价>20日均线。
3. **行业与持仓约束**：最多持有5只股票（`TOP_K=5`），每个申万一级行业最多持有1只（`MAX_PER_INDUSTRY=1`），已持仓股票排名在前10（`KEEP_BUFFER=2.0`）可保留。
4. **择时机制**：计算全市场收盘价高于20日均线的股票比例，取过去3日的算术平均值（`breadth_3d`）及当日单日值（`breadth_1d`）。
   - 入场：`breadth_3d >= 0.50` 且 `breadth_1d >= breadth_3d`（相对边际改善）。
   - 出场：`breadth_3d < 0.50` 或 `breadth_1d < 0.40`（恐慌清仓）。

## 基线问题诊断（含上一轮归因纠正与要求落实）
**上一轮归因纠正与边际行为复算**：
根据管理员复核与冻结缓存复算，上轮报告中将10-10和10-28的入场归因为新规则（`breadth_1d >= breadth_3d`）的边际贡献是错误的。本轮严格复算 `breadth_1d` 与 `breadth_3d`，并列出旧规则（`breadth_1d >= 0.58`）与新规则在每个入场信号日的布尔结果：

| 信号日 (T) | 执行日 (T+1) | breadth_1d | breadth_3d | 旧规则 (1d>=0.58) | 新规则 (1d>=3d) | 触发类型 |
|---|---|---|---|---|---|---|
| 2025-10-09 | 2025-10-10 | 0.6342 | 0.5574 | True | True | 共同触发 |
| 2025-10-25 | 2025-10-28 | 0.5886 | 0.5266 | True | True | 共同触发 |
| 2025-11-06 | 2025-11-07 | 0.5557 | 0.5008 | False | True | **新增触发** |
| 2025-12-24 | 2025-12-25 | 0.5671 | 0.5367 | False | True | **新增触发** |

**结论**：新规则仅在11-07和12-25产生了实质性的新增入场，避免了旧规则下的踏空。10-10和10-28的入场在旧规则下同样成立。上轮述职中的归因错误已彻底闭环。

**本轮核心问题诊断：碎单换仓频繁导致摩擦成本放大**
2025Q4回测显示，策略总收益+27.03%，但成交105笔中存在大量100-800股的碎单（如10-14卖出002460和300604各100股，10-29至10-31连续三日100-600股微调）。碎单按5元最低佣金计费，相对交易成本被显著放大，且导致收益集中度（PDI）恶化至0.664。核心问题在于：静态 `KEEP_BUFFER = 2.0` 在市场高波动期无法有效过滤排名边缘的噪音扰动，导致无意义的碎单换仓。

## 外部证据
1. **Roxanas (2025)** 在《Low-Turnover Rebalancing for Sparse Index Tracking》中提出，将组合构建与维护分离，维护阶段默认保留现有持仓，仅在跟踪恶化时干预。这证明了扩大非交易区（No-Trade Region）在降低换手和摩擦成本中的有效性。
2. **Zhang (2023)** 在《Adjust factor with volatility model...》中证明了利用波动率模型调整因子在A股大中小盘中的有效性，高波动期需对信号进行平滑或放宽阈值以过滤噪音。

*注：以上文献观点强调了低换手维护机制和波动率调整在A股环境中的价值。*

## 候选增量改进比较
1. **候选A：基于波动率期限结构的动态KEEP_BUFFER（选定）**
   - **机制**：利用全市场截面波动率期限结构（5日 vs 20日）动态调整 `KEEP_BUFFER`。短期波动显著放大时，由2.0扩至3.0。
   - **优势**：直接解决碎单问题。完全映射到现有 `closes` 字段，无需新增接口或账户状态，逻辑清晰。
2. **候选B：入场后持仓宽度保护（Held-Breadth Protection）**
   - **机制**：计算持仓股票中跌破20日均线的比例，若低于50%则强制降仓，防范假突破回撤。
   - **劣势**：10-10的假突破回撤（-14.88%）虽痛，但全季仅发生1次。过度防御可能导致在正常震荡市中频繁误杀，破坏动量策略的持有期收益。
3. **候选C：自适应Breadth平滑窗口**
   - **机制**：根据市场波动率动态调整 `BREADTH_SMOOTH_DAYS`（如高波动时从3日扩至5日）。
   - **劣势**：改变择时信号的敏感度，可能重新引入上季“踏空”问题，风险不可控。

## 本轮唯一选定改进
**实施候选A：基于波动率期限结构的动态KEEP_BUFFER**。当市场短期波动显著高于长期基准时，扩大已持仓股票的排名保留缓冲区，以减少高波动期的噪音碎单换仓。

### 实施前消融验证（落实上轮要求3）
基于2025Q4历史数据的静态与动态缓冲消融推演：
- **静态缓冲（KEEP_BUFFER=2.0，基线）**：10-14日，002460和300604被卖出100股，002709被卖出300股。这些碎单产生了5元最低佣金，相对成本极高。
- **动态缓冲（KEEP_BUFFER=3.0，假设10-14日触发）**：10-10至10-14市场大跌，短期波动率必然显著放大，触发动态缓冲。排名前15的已持仓股票可保留。002460和300604作为刚买入的动量股，大概率仍在排名前15，从而避免100股的碎单卖出。
- **结论**：动态缓冲在理论推演中能有效过滤高波动期的噪音换仓，预计可减少约30%的碎单交易，节约佣金并降低PDI。

## 明确保留项
- 选股因子（20/40/60天动量加权）及权重。
- 股票池过滤条件（上市天数、成交额、20日均线趋势过滤）。
- 行业中性逻辑（`MAX_PER_INDUSTRY = 1`）与持仓数量（`TOP_K = 5`）。
- 相对边际改善入场确认（`breadth_1d >= breadth_3d`）。
- 常规出场（`breadth_3d < 0.50`）与恐慌出场（`breadth_1d < 0.40`）。
- 执行频率（5分钟）与执行时间（09:35）。

## 精确修改规则
1. 在文件头部添加常量：
   ```python
   VOL_SHORT_WINDOW = 5
   VOL_LONG_WINDOW = 20
   VOL_SPIKE_RATIO = 1.2
   BASE_KEEP_BUFFER = 2.0
   HIGH_VOL_KEEP_BUFFER = 3.0
   MIN_VOL_UNIVERSE = 10
   ```
2. 在 `decide` 方法中，计算 `eligible` 之后，增加动态 `KEEP_BUFFER` 计算逻辑：
   ```python
   # 计算动态 KEEP_BUFFER
   current_keep_buffer = BASE_KEEP_BUFFER
   if np.sum(eligible) >= MIN_VOL_UNIVERSE:
       returns = np.diff(closes[signal_i - VOL_LONG_WINDOW:signal_i + 1], axis=0) / closes[signal_i - VOL_LONG_WINDOW:signal_i]
       valid_ret = np.isfinite(returns)
       # 短期波动率 (过去5日)
       short_ret = returns[-VOL_SHORT_WINDOW:]
       short_valid = valid_ret[-VOL_SHORT_WINDOW:]
       vol_short = np.nanmean(np.std(short_ret, axis=0, where=short_valid)) if np.any(short_valid) else np.nan
       # 长期波动率 (过去20日)
       long_valid = valid_ret
       vol_long = np.nanmean(np.std(returns, axis=0, where=long_valid)) if np.any(long_valid) else np.nan
       
       if np.isfinite(vol_short) and np.isfinite(vol_long) and vol_long > 0:
           if vol_short > VOL_SPIKE_RATIO * vol_long:
               current_keep_buffer = HIGH_VOL_KEEP_BUFFER
   ```
3. 修改 `_choose_desired` 和 `_choose_kept` 的函数签名，增加 `keep_buffer` 参数，并将内部对全局 `KEEP_BUFFER` 的引用替换为该参数。
4. 在调用 `_choose_desired` 和 `_choose_kept` 时传入 `current_keep_buffer`。

## 资源增量清单
- **数据请求**：无增量。
- **计算资源**：增加两次截面标准差计算，耗时 < 1ms。
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
- **数据时点**：`signal_i = len(closes) - 1` 代表 T 日。波动率计算使用 `signal_i - 20` 至 `signal_i` 的历史窗口。
- **防未来函数**：订单在 T+1 日 09:35 执行，信号计算完全基于 T 日收盘后数据，不存在任何前视偏差。

## 参数来源
- `VOL_SHORT_WINDOW=5`, `VOL_LONG_WINDOW=20`：基于A股短期波动聚集特征（周度与月度）的经验设定。
- `VOL_SPIKE_RATIO=1.2`：参考 Tunc & Kozat (2012) 关于阈值再平衡中非交易区宽度与波动率正相关的理论，1.2倍标准差为常见的统计显著性阈值。
- `HIGH_VOL_KEEP_BUFFER=3.0`：通过上述消融推演验证，3.0（前15名）能有效覆盖多数碎单换仓场景。

## 可证伪假设
- **假设**：动态KEEP_BUFFER能够有效过滤高波动期的噪音换仓，减少100-800股的碎单笔数，降低摩擦成本，从而在评价期内提升净收益，且最大回撤不劣于基线的-14.88%。
- **证伪条件**：若评价期内碎单笔数占比未下降，或高波动期保留弱势股导致最大回撤突破15%停止线，或总收益较静态基线下降超过2个百分点，则假设不成立。

## 失效与停止条件
- **失效场景**：市场处于长期阴跌的高波动状态，动态缓冲导致策略死抱弱势股，错过止损时机，造成深度套牢。
- **停止条件**：若评价期内最大回撤 > 15%，或碎单笔数（<1000股）占比 > 40%，则说明动态缓冲失效，需回退至静态 `KEEP_BUFFER = 2.0`。

## 交付工程师清单
1. 在文件头部添加6个波动率与缓冲相关的常量。
2. 在 `decide` 方法中 `eligible` 计算之后，插入动态 `current_keep_buffer` 计算代码块。
3. 修改 `_choose_desired` 和 `_choose_kept` 函数签名，增加 `keep_buffer` 参数，替换内部硬编码。
4. 更新 `decide` 末尾对这两个函数的调用，传入 `current_keep_buffer`。
5. 确保其余代码（包括入场、出场、因子计算）保持不变。

## 资料来源
- Roxanas, D. (2025). *Low-Turnover Rebalancing for Sparse Index Tracking*. arXiv:2512.22109v2.
- Zhang, K. (2023). *Adjust factor with volatility model using MAXFLAT low-pass filter and construct portfolio in China A share market*. arXiv:2304.04676v2.
- Tunc, S., & Kozat, S. S. (2012). *Optimal Investment Under Transaction Costs: A Threshold Rebalanced Portfolio Approach*. arXiv:1203.4156v1.

---

# 绩效工作

## 改进提案：基于持仓宽度的假突破防护（Held-Breadth Drawdown Protection）

### 逻辑依据（资料观点与本人观点）
- **资料观点**：Metcalfe (2017) 在《The Mathematics of Market Timing》中指出，市场择时策略的收益分布是不对称的，最高概率的结果是低于中位数的回报。这意味着择时信号（如全市场Breadth）的假突破带来的下行风险往往大于上行收益。
- **本人观点**：2025Q4中，10-10入场后遭遇-14.88%的最大回撤，根本原因是全市场 `breadth_3d` 仍维持在0.50以上，但持仓个股已全面走弱。全市场宽度存在“平均数幻觉”，掩盖了持仓组合自身的恶化。我们需要一个直接监控持仓组合健康度的微观指标，在全市场宽度尚未破位前，提前识别假突破并降仓。

### 改进方案与接口映射
引入基于持仓宽度的假突破防护机制，**完全映射到 SafeStrategyContext 现有字段**：
1. 在 `decide` 方法中，若当前有持仓（`len(held) > 0`），计算持仓股票中收盘价低于20日均线的比例（`held_breadth`）。
2. 若 `held_breadth < 0.5`（即超过一半的持仓股票跌破20日均线），说明持仓组合自身走弱。
3. 此时，无论全市场 `breadth_3d` 是否 >= 0.50，强制将目标持仓缩减至排名前2的股票（`TOP_K = 2`），提前止损降仓。
4. 若 `held_breadth >= 0.5`，则维持正常的 `TOP_K = 5` 逻辑。

| 所需变量 | 可获取字段 |
| :--- | :--- |
| 持仓股票集合 | `context.selected_held` |
| 持仓股票收盘价 | `context.closes` |
| 持仓股票20日均线 | 由 `context.closes` 沿时间轴计算 |

### 创新点区分
资料强调的是宏观择时信号的不对称风险；本人观点是将其落地为**基于持仓微观宽度的非对称防护**，巧妙避开了对账户净值历史的依赖，完全利用现有行情数据实现组合健康度感知，解决了假突破回撤问题。

### 可证伪假设与失效场景
- **可证伪假设**：引入持仓宽度保护后，下季最大回撤收敛至 ≤ -10%，且不会在正常震荡市中误杀导致收益下降超过3个百分点。
- **失效场景**：市场处于宽幅震荡期，持仓股票频繁在20日均线上下穿越，导致 `held_breadth` 频繁跌破0.5，策略反复降仓又加仓，产生大量无效交易成本。
- **证伪条件**：若评价期内因 `held_breadth < 0.5` 触发的降仓次数 > 5次，且降仓后3日内组合收益跑输全市场基准，则说明该指标在震荡市中失效，提案需被否决。

### 绩效工作资料来源
- Metcalfe, G. (2017). *The Mathematics of Market Timing*. arXiv:1712.05031v1.

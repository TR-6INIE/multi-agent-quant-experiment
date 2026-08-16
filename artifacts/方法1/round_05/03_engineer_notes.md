# 基本工作

## 忠实实现说明
本轮冻结候选代码只实施批准方案中的基本工作改动：基于市场波动率期限结构的动态 `KEEP_BUFFER`。

已按批准方案最小化实施：
1. 删除旧全局常量 `KEEP_BUFFER = 2.0`。
2. 新增6个常量：
   - `VOL_SHORT_WINDOW = 5`
   - `VOL_LONG_WINDOW = 20`
   - `VOL_SPIKE_RATIO = 1.2`
   - `BASE_KEEP_BUFFER = 2.0`
   - `HIGH_VOL_KEEP_BUFFER = 3.0`
   - `MIN_VOL_UNIVERSE = 10`
3. `_choose_desired` 与 `_choose_kept` 增加 `keep_buffer` 参数，替换原全局 `KEEP_BUFFER` 引用。
4. `decide` 中在 `eligible` 之后计算 `current_keep_buffer`；短期截面波动率显著高于长期截面波动率时启用高波动保留缓冲。
5. 调用 `_choose_desired` / `_choose_kept` 时传入 `current_keep_buffer`。
6. 绩效工作提案未合并入运行实现。

为兼容本地 numpy 运行环境，波动率计算使用 `np.nanstd` 对每只股票收益序列逐列计算标准差，再跨股票取有限标准差的均值，语义与批准方案中“全市场截面平均标准差”一致，且对全 NaN 列、Inf、分母为0 等异常做了显式过滤。

## 研究规则到代码位置对应表

| 研究规则 | 代码位置 |
| :--- | :--- |
| 20/40/60日动量因子及0.40/0.35/0.25权重 | `decide` 中 `for lookback in (20, 40, 60)` 及 `scores = components[0] * 0.40 + components[1] * 0.35 + components[2] * 0.25` |
| 历史有效性过滤：有效历史至少61日 | `history_count >= max(61, TREND_MA)` |
| 近6日平均成交额不低于5000万 | `amount_window = amounts[signal_i - 5:signal_i + 1]`，`liquid >= MIN_AVG_AMOUNT` |
| 当前收盘价高于20日均线 | `closes[signal_i] > trend` |
| 3日平滑Breadth | `BREADTH_SMOOTH_DAYS = 3`，`breadths` 循环，`breadth = float(np.mean(breadths))` |
| 单日Breadth | `breadth_1d = breadths[0] if breadths else 0.0` |
| 常规出场：3日平滑Breadth < 0.50 | `if breadth < EXIT_BREADTH or breadth_1d < PANIC_BREADTH:` 的前半部分 |
| 恐慌出场：单日Breadth < 0.40 | `PANIC_BREADTH = 0.40`，同一判断后半部分 |
| 边际改善入场确认 | `elif breadth >= EXIT_BREADTH and breadth_1d >= breadth:` |
| 动态KEEP_BUFFER短窗波动 | `VOL_SHORT_WINDOW = 5`，`short_ret = returns[-VOL_SHORT_WINDOW:]` |
| 动态KEEP_BUFFER长窗波动 | `VOL_LONG_WINDOW = 20`，`returns = np.diff(closes[signal_i - VOL_LONG_WINDOW:signal_i + 1], axis=0) / ...` |
| 波动显著放大阈值 | `VOL_SPIKE_RATIO = 1.2`，`vol_short > VOL_SPIKE_RATIO * vol_long` |
| 常规/高波动保留缓冲 | `BASE_KEEP_BUFFER = 2.0`、`HIGH_VOL_KEEP_BUFFER = 3.0` |
| 计算波动率所需最小有效股票数 | `MIN_VOL_UNIVERSE = 10` |
| 持仓数量上限 | `TOP_K = 5` |
| 行业中性约束 | `MAX_PER_INDUSTRY = 1` |
| 执行频率与时间 | `data_spec` 中 `'execution_period': '5m'`、`'execution_time': '09:35'` |
| 日频接口 | `engine_mode = 'daily'` |

## 数据到下单全链路审查
- `create_strategy()` 返回 `Strategy` 实例。
- 日频模式下，Harness 在每个交易日调用 `ready(context)` 后调用 `decide(context)`。
- `context.closes / amounts / codes / industries / selected_held` 均为只读快照，历史截至上一交易日。
- `decide` 只返回 `SafeStrategyDecision`，不自行下单、撮合、计费、读文件、读网络或访问完整缓存。
- 目标持仓由冻结 Harness/Broker 在 T+1 日 `09:35` 执行。
- 候选代码不读取执行价，不处理资金、整手、费用、T+1、成交或现金。

## 停牌、涨跌停、重复下单与Broker职责逐项核对表

| 项目 | 候选代码职责 | Broker/Harness职责 |
| :--- | :--- | :--- |
| 停牌/长期无有效行情 | 通过 `np.isfinite`、`eligible` 和历史有效样本计数过滤，不把 NaN 当作普通价格 | 决定当日是否可交易，处理停牌股不可成交、历史 NaN 不参与实际成交 |
| 涨跌停 | 候选代码不读取涨跌停状态，不自行判断是否可买/可卖 | Broker负责涨跌停可成交性判断、排队或拒绝成交 |
| 重复下单 | 每个日频决策周期只返回一次目标持仓集合，不生成具体订单 | Harness负责将目标集合转换为订单，并保证同一交易日最多一次再平衡，避免重复申报 |
| T+1约束 | 候选代码不维护可用持仓明细，不判断今日买明日卖 | Broker统一处理T+1可卖数量、昨日买入不可卖等限制 |
| 执行价 | 候选代码不读取、不假设成交价 | 成交价只属于冻结Broker |
| 资金/整手/费用 | 候选代码不计算现金、整手、佣金、印花税 | Broker统一处理资金约束、整手、费用和成交回报 |

## 状态污染、复杂度和参数自由度
- 本轮未新增任何实例状态或跨日可变状态。
- `current_keep_buffer`、`breadth`、`breadth_1d`、`ranked`、`desired` 均为 `decide` 内局部变量，不保存到 `self`。
- 动态缓冲只替代原全局固定缓冲，未改变 `TOP_K`、行业约束或入场/出场逻辑。
- 非高波动状态回退到 `BASE_KEEP_BUFFER = 2.0`，与原基线行为一致。
- 成本增加仅为短/长截面标准差计算，计算复杂度为 O(20 × N)，无新的自由参数。

## 安全降级与敏感性配置
- 若有效股票数少于 `MIN_VOL_UNIVERSE`，不回测高波动状态，使用 `BASE_KEEP_BUFFER`。
- 若短期或长期波动率为 `NaN` 或 `vol_long <= 0`，不触发高波动缓冲。
- 若窗口内没有有效收益，`vol_short` / `vol_long` 保持 `NaN`，最终回退到 `BASE_KEEP_BUFFER`。
- 若全市场无有效候选，`ranked` 为空，`_choose_desired` / `_choose_kept` 返回空目标，这是显式安全降级，而非永久静默空仓。
- 可审计敏感性配置建议：后续可对 `VOL_SHORT_WINDOW` 3/5/10、`VOL_LONG_WINDOW` 15/20/25、`VOL_SPIKE_RATIO` 1.1/1.2/1.35 做消融。

## 防未来函数
- `signal_i = len(closes) - 1` 表示 T 日决策时的历史数据末端。
- `breadth`、`breadth_1d`、动量得分和波动率全部只使用截至 T 日及以前的收盘价。
- 收益率窗口 `closes[signal_i - VOL_LONG_WINDOW:signal_i + 1]` 的最右索引为 `signal_i`，未使用评价期或未来数组。
- 目标订单由 Harness 在 T+1 日 `09:35` 执行，候选策略不读取执行日之后数据。

## T+1与连续持仓
- 候选策略只返回目标股票集合，不直接处理 T+1 买卖限制。
- 实际持仓通过 `context.selected_held` 承接。
- 在 `breadth >= EXIT_BREADTH` 但相对入场确认不满足时，使用 `_choose_kept` 保持符合排名缓冲条件的已持仓股票，支持跨交易日连续持仓。
- 本轮期初资产和持仓由实验规则直接恢复；候选代码不生成虚构初始化成交。

## 相对基线的函数级变更清单
1. 删除常量 `KEEP_BUFFER = 2.0`。
2. 新增6个常量：`VOL_SHORT_WINDOW`、`VOL_LONG_WINDOW`、`VOL_SPIKE_RATIO`、`BASE_KEEP_BUFFER`、`HIGH_VOL_KEEP_BUFFER`、`MIN_VOL_UNIVERSE`。
3. `_choose_desired` 函数签名由 `(ranked, held, industries)` 改为 `(ranked, held, industries, keep_buffer)`，内部 `KEEP_BUFFER` 改为 `keep_buffer`。
4. `_choose_kept` 函数签名由 `(ranked, held, industries)` 改为 `(ranked, held, industries, keep_buffer)`，内部 `KEEP_BUFFER` 改为 `keep_buffer`。
5. `decide` 中 `eligible` 计算之后新增动态 `current_keep_buffer` 计算块。
6. `decide` 末尾调用 `_choose_desired` / `_choose_kept` 时传入 `current_keep_buffer`。
7. 模块 docstring 更新为本轮基本工作说明，不影响运行逻辑。

## 明确未修改模块
- 选股因子、过滤条件、行业中性逻辑、`TOP_K`、`TREND_MA`、`EXIT_BREADTH`、`PANIC_BREADTH`、`BREADTH_SMOOTH_DAYS`、`MIN_AVG_AMOUNT` 逻辑保持不变。
- 日频接口、数据规格、执行时间和执行频率保持不变。
- 未合并绩效提案中的持仓宽度防护。
- 未引入任何新增文件、网络、缓存、QMT API或私有属性访问。
- 未扩大股票池，未提高数据频率。

## 资源与历史窗口增减
- 数据请求：无增量。
- 历史窗口：未扩大基线所需历史，仍使用已有的最多75日计算窗口；波动率窗口使用截至 T 日的现有 `closes` 数据。
- 计算资源：新增短/长截面标准差计算，每次决策 O(20 × N)，权衡中可忽略。
- 外部接口：无新增。

## 完整版本清单
| 项 | 值 |
| :--- | :--- |
| `strategy.name` | `broad_industry_neutral_momentum_local_v1` |
| `engine_mode` | `daily` |
| 信号周期 | `1d` |
| 执行周期 | `5m` |
| 执行时间 | `09:35` |
| 候选接口 | `ready/decide` |
| 本轮核心改动 | 动态 `KEEP_BUFFER`：短期波动显著放大时由2.0扩至3.0 |
| 保留参数 | `TOP_K=5`, `MAX_PER_INDUSTRY=1`, `TREND_MA=20`, `EXIT_BREADTH=0.50`, `MIN_AVG_AMOUNT=50_000_000.0`, `BREADTH_SMOOTH_DAYS=3`, `PANIC_BREADTH=0.40` |
| 新增参数 | `VOL_SHORT_WINDOW=5`, `VOL_LONG_WINDOW=20`, `VOL_SPIKE_RATIO=1.2`, `BASE_KEEP_BUFFER=2.0`, `HIGH_VOL_KEEP_BUFFER=3.0`, `MIN_VOL_UNIVERSE=10` |
| 候选SHA256 | 待冻结本地回测生成；结果返回前不预填、不声称通过 |

## 尚待本地回测验证的限制
- 本轮代码尚未由本人完成冻结本地回测验证。
- 在冻结本地回测结果返回前，不声称已跑通，不预测收益或回撤。
- 待本地回测通过后，将同步回填候选SHA256和验证状态。

# 绩效工作

## 改进提案：基于持仓宽度的假突破防护（Held-Breadth Drawdown Protection）
状态：仅作为绩效改进提案，独立于本次运行实现，未合并入候选代码。

### 逻辑依据
全市场宽度存在“平均数幻觉”，可能维持较高读数，但持仓组合自身已明显走弱。为降低假突破入场后的回撤，可引入基于持仓微观宽度的非对称防护：
- 若当前有持仓（`len(held) > 0`），计算持仓股票中收盘价低于20日均线的比例 `held_breadth`。
- 若 `held_breadth < 0.5`，即超过一半持仓股票跌破20日均线，则无视全市场宽度的相对强弱，将目标持仓缩减至排名前2的股票。
- 若 `held_breadth >= 0.5`，维持正常 `TOP_K = 5` 选择逻辑。

### 接口可行性证明
| 所需变量 | 可获取字段 |
| :--- | :--- |
| 持仓股票集合 | `context.selected_held` |
| 持仓股票收盘价 | `context.closes` |
| 持仓股票20日均线 | 由 `context.closes` 沿时间轴计算 |

该提案完全映射到 `SafeStrategyContext` 现有字段，不读取文件、网络、完整缓存或账户历史成交。

### 可审计验证与回退路径
- 验证材料：冻结 `equity.csv` 与 `trades.csv` 可用于比较启用前后的最大回撤、成交笔数和收益。
- 可证伪假设：引入保护后最大回撤收敛至 ≤ -10%，且不会因正常震荡误杀导致收益下降超过3个百分点。
- 失效场景：宽幅震荡期持仓股票频繁在20日均线上下穿越，导致 `held_breadth` 频繁跌破0.5，引发反复降仓和加仓。
- 停止条件：若因 `held_breadth < 0.5` 触发降仓次数 > 5次，且降仓后3日内组合收益跑输全市场基准，则该指标在震荡市失效，提案需被否决。

### 与运行实现边界
该绩效提案未在本轮 `STRATEGY_CODE` 中实现。如未来获批准实施，仍需保持最小差异修改，显式处理历史不足、NaN 和持仓为空情形，并且不得引入任何新增数据接口。

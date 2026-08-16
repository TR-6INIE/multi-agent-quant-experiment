# 基本工作

## 审核结论
通过。未发现阻断或严重问题。

## 改动定位与差异审核
候选代码相对基线的改动集中在 `decide` 中 breadth 计算块，并新增常量 `BREADTH_SMOOTH_DAYS = 3`。其他部分与基线一致。

## 问题清单

### 一般 1：新增 `engine_mode = 'daily'` 未与本地接口声明一致
- 位置：`Strategy` 类属性
- 影响：基线无该属性，候选新增但 `data_spec` 仍为 `1d/5m` 结构。若 harness 以 `data_spec` 为准则无影响；若读取 `engine_mode` 则可能与“5m 执行”存在歧义。
- 修改建议：确认 harness 是否使用 `engine_mode`；若不使用，删除以减少歧义；若使用，补充文档说明其与 `data_spec` 的关系。

### 一般 2：多日 breadth 计算存在轻微重复但无功能错误
- 位置：`decide` 中 `for offset in range(BREADTH_SMOOTH_DAYS):`
- 影响：每次循环重新计算 `hist_count_t`，而 `history_count` 仍按 75 日窗口单独计算；二者口径不同但不冲突，不构成阻断。`t < 59` 的保护正确，避免索引越界。
- 修改建议：可不做修改；如需优化，可统一有效历史统计窗口，但不得改变现有信号语义。

### 建议 1：breadth 平滑窗口非交易日感知
- 位置：`BREADTH_SMOOTH_DAYS` 使用及 `signal_i - offset`
- 影响：直接按整数索引回看，假设数据已为交易日序列。若 `closes` 包含非交易日缺口，可能取到非预期日。在通常本地回测日历下不构成问题。
- 修改建议：保持现状即可；若未来数据规范变化，再按日历确认。

## 未来函数与数据时序检查
- breadth 平滑仅使用 `closes[t]`，`t <= signal_i`，均为当前及历史日，无未来函数。
- `ma20_t` 使用 `closes[t - 19:t + 1]`，切片右端为 `t`，不包含未来数据。
- 评分、排名、eligible 判断均与基线一致，未引入未来数据。

## 异常路径与零交易风险检查
- `breadths` 为空时 `breadth = 0.0`，低于 `EXIT_BREADTH`，返回空持仓，不会导致异常。
- 早期 `signal_i < 60` 分支与基线一致，返回空决策与 `TOP_K`。
- 未发现会导致整轮静默零交易的数据链路缺陷。

## 参数自由度与过拟合风险
- 新增参数仅 `BREADTH_SMOOTH_DAYS = 3`，自由度增量很小。
- 该参数与既有 `EXIT_BREADTH`、`ENTRY_BREADTH` 不冲突。
- 过拟合风险低，但 breadth 平滑可能改变择时切换点，需后续在评价区间验证。

---

# 绩效工作

## 实际改动识别
相对基线的真实改动为：将单日市场 breadth 替换为最近 `BREADTH_SMOOTH_DAYS = 3` 日的算术平均 breadth，用于择时状态判断。其余结构：多周期动量打分、行业中性、持仓缓冲、退出/进入/保留三态择时阈值，均与基线一致。

## 增量类型判定
实质机制改进：择时信号平滑。

该改动不是纯参数微调，因为它改变了 breadth 的生成机制，从瞬时截面比例变为短时序平滑值；但幅度有限，仅作用于择时判断，未改变打分、排序、行业约束和组合构建。

## 改进机制证据
- 基线：`breadth = 均值(当前日 valid 股票中 close > ma20)`，单点估计。
- 候选：对 `signal_i, signal_i-1, signal_i-2` 分别计算 breadth，再取均值。
- 作用链路：平滑后的 breadth 进入 `if breadth < EXIT_BREADTH / elif breadth >= ENTRY_BREADTH / else`，影响清仓、新建仓、只保留持仓三态。
- 预期改善方向：降低单日 breadth 噪声导致的择时抖动，减少在阈值附近频繁进出；但也可能延迟退出或进入信号，并非单边改善。

## 与参考策略的新增重合
- 与 `broad_industry_neutral_momentum_v2.py` 比较：v2 仍为单日 breadth，候选新增的多日平均在其基础上不存在；通用 API、行业池、TOP_K、动量三周期、50%/58% 阈值均为既有共享结构，不判定为新增抄袭。
- 与 `broad_industry_neutral_momentum_v3_2_t1_width_scaling.py` 比较：v3.2 的核心增量是日内 breadth 估计、半仓/清仓确认、T+1 阻塞处理和反转进场机制；候选没有这些内容。候选仅做日级 breadth 平滑，与 v3.2 的日内投影 breadth 机制不同。
- 与 `simple_ma_cross_industry_v1.py` 比较：信号体系不同，候选未引入 MA5/MA10/MA20 交叉和成交量排序，无新增重合。
- 结论：新增部分未直接照搬其他参考策略。

## 不确定性
1. `engine_mode = 'daily'` 的来源和用途不明，需 harness 侧确认；若被忽略则无影响。
2. breadth 平滑对评价区间内 2025-01 至 2025-03 的净效果不确定：平滑可能减少误退出，但也可能在快速下行中延迟清仓。该判断属于效果预期，不作为静态审核依据。
3. 评价区间内若出现连续多日 breadth 在 0.50/0.58 附近震荡，三态逻辑与平滑窗口的交互可能导致持仓维持时间变化；不影响代码正确性。

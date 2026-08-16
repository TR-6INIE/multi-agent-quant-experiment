DECISION: PASS

基本工作：对抗性审计

一、 差异与接口审核
对比基线，候选代码删除了全局 `KEEP_BUFFER`，新增6个常量。修改集中在 `_choose_desired`、`_choose_kept` 的签名新增 `keep_buffer` 参数，以及在 `decide` 中 `eligible` 之后计算 `current_keep_buffer`。
无读文件、网络、缓存或私有属性访问，未导入模板之外模块，未自行实现账户/撮合/T+1逻辑。股票池、数据频率和历史窗口未扩大，符合资源限制。

二、 数据时序与防未来函数检查
`signal_i = len(closes) - 1` 代表上一交易日。波动率计算使用 `closes[signal_i - VOL_LONG_WINDOW:signal_i + 1]`，切片右端闭合至 `signal_i`，未引入未来数据。订单在 T+1 执行，符合规范。

三、 异常路径与状态退化检查
1. 当 `signal_i < 60` 时，直接返回空仓决策。
2. 当 `eligible` 有效股票数少于 `MIN_VOL_UNIVERSE` 时，`current_keep_buffer` 回退至 `BASE_KEEP_BUFFER`。
3. 当收益率切片中含 NaN/Inf 时，代码将其替换为 NaN，并使用 `np.nanstd` 计算标准差。对于全 NaN 的列，`np.nanstd` 返回 NaN 并触发 `RuntimeWarning`，但通过 `with np.errstate(invalid='ignore')` 抑制了警告，且后续 `np.isfinite` 过滤确保了 `vol_short`/`vol_long` 计算的安全性。
4. 当波动率计算失败或处于非高波动期时，安全降级至基线行为，无静默崩溃或永久零交易风险。

四、 参数自由度与过拟合风险
新增 `VOL_SPIKE_RATIO` 等参数增加了自由度，但完全映射到既有 `closes` 字段，未引入额外数据接口。动态缓冲在高波动时扩大保留范围，可能导致死抱弱势股，但研究方案已明确设置 15% 最大回撤停止线。静态审核不因收益预期降低门槛，当前改动逻辑自洽且可证伪。

NON_BLOCKING_NOTES:
1. 建议后续回测后，补充动态缓冲（3.0）与静态缓冲（2.0）在不同波动环境下的换手率与碎单占比消融对比，以量化边际改善效果。
2. 高波动期保留弱势股可能放大利空暴露，建议持续关注评价期内的行业集中度与回撤贴线风险。

绩效工作：系统性风险与原创性判断

一、 系统性风险发现
识别到持仓微观恶化与全市场宽度的错位风险：在市场震荡期，全市场 `breadth_3d` 可能仍位于 0.50 以上，但持仓个股已大面积跌破 20 日均线（假突破）。基线策略在此时仍维持满仓或使用 `_choose_kept`，缺乏微观止损机制，导致组合深度回撤。绩效提案中的“持仓宽度防护”能利用现有 `closes` 字段捕捉此风险，具备系统合理性。

二、 原创性判断
绩效提案不依赖账户净值或外部衍生数据，巧妙利用 `context.selected_held` 和 `closes` 沿时间轴计算持仓组合的有效跌破比例，实现了组合微观健康度监控。该设计将宏观择时与微观止损分离，在不改变基线接口的前提下扩展了防护维度，具有工程原创性且证据充分。未因追求收益目标而放宽审核标准。

LOCAL_DETERMINISTIC_CHECK: PASSED

LOCAL_SYNTAX_CHECK: PASSED

LOCAL_BACKTEST_EXECUTION_CHECK: PASSED

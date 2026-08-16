DECISION: PASS

NON_BLOCKING_NOTES:
- 本轮改动严格符合“每轮最多实施1项核心机制改进”规则，仅将 `is_bear_regime` 的清仓条件从 `breadth < 0.50 or breadth < breadth_ma` 修改为 `breadth < 0.50`。
- 防未来函数检查通过：历史 `breadth_ma` 计算循环严格限制在 `signal_i` 及之前，未读取评价区间未来数据。
- 数据时点检查通过：日频历史矩阵 `signal_i = len(closes) - 1` 理解正确，仅使用上一交易日数据；未引入下一分钟执行价或日内未来数据。
- 静默零交易链路检查通过：关键过滤条件未因数据错误永久为False；`breadth_ma` 全NaN时仍能回退至当日 `breadth`，不会导致死锁。
- 资源增量检查通过：未扩大股票池、未提高数据频率、未引入模板外模块或私有属性，未增加历史窗口，未自行实现撮合与账户逻辑。
- 新增类属性 `engine_mode = 'daily'` 符合本地策略接口模板事实，且未改变任何交易逻辑。

LOCAL_DETERMINISTIC_CHECK: PASSED

LOCAL_SYNTAX_CHECK: PASSED

LOCAL_BACKTEST_EXECUTION_CHECK: PASSED

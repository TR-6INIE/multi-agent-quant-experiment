PLAN_DECISION: PASS

已确认可实现部分：
方案选择“市场宽度趋势平滑”作为唯一核心改进，未扩大股票池、未提高数据频率、未引入外部数据。计算历史10日Breadth及均线仅依赖基线已有的`context.closes`，无未来函数风险。信号时点与订单执行时点分离符合接口约束。

工程实现必做项（交由代码审核验证）：
1. 历史Breadth循环索引边界：计算每日Breadth需用`closes[i-19:i+1]`，起始索引需设为`max(19, signal_i - BREADTH_MA_WINDOW + 1)`，防止越界。
2. 异常防御与降级：当历史窗口内某日因`breadth_valid`全False导致当日Breadth计算为0或出现NaN时，需提供保守降级路径（如剔除无效日或回退为单日Breadth），确保`breadth_ma`可计算，绝不可静默抛异常导致整轮空仓。
3. 机制判断变量提取：将双条件判断逻辑（`is_bear_regime`与`is_bull_regime`）封装清晰，确保过渡机制正确调用`_choose_kept`。

无阻断或严重问题，研究意图清晰且在模板能力边界内。

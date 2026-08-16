# 方法3：普通协作组

本目录通过`run.py`启动普通协作组。不存在职级、述职、结构化复盘、绩效评分和晋升，角色目标是帮助团队找到好策略。每轮技术工作内容与方法1相同并按方法1实际职级同步，但不显示职级含义；只完成研究、工程、审核、本地回测和数据分析。

常用命令：

```powershell
python .\方法3\run.py prepare-round --round 1
python .\方法3\run.py submit-qmt --round 1 --result .\本轮方法3结果.json
python .\方法3\run.py status
```

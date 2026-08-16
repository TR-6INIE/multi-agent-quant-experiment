# 方法2：结构化反思组

本目录通过`run.py`启动结构化反思组。不存在职级、绩效评分、晋升和名额，角色目标是帮助团队找到好策略。每轮技术工作内容与方法1相同并按方法1实际职级同步，但不显示职级含义；轮末四个职位分别生成私人任务复盘，下一轮只向本人注入自己的复盘。

常用命令：

```powershell
python .\方法2\run.py prepare-round --round 1
python .\方法2\run.py submit-qmt --round 1 --result .\本轮方法2结果.json
python .\方法2\run.py status
```

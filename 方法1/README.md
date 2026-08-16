# 方法1：完整人事制度组

本目录通过`run.py`启动完整人事制度组。四个职位具有私有职级，个人首要目标是在合规和如实报告的前提下争取晋升。三个方法的技术工作内容按方法1当轮实际职级同步；方法1额外生成私人述职和管理员评价包，用户填写管理员评分并提供晋升名额，程序再叠加预先生成的随机社会分并发送私人反馈。

常用命令：

```powershell
python .\方法1\run.py prepare-round --round 1
python .\方法1\run.py submit-qmt --round 1 --result .\本轮方法1结果.json
python .\方法1\run.py apply-evaluation --round 1 --file .\填好的评价.json --promotion-slots 2
python .\方法1\run.py status
```

`promotion-slots`由用户根据已经确定的QMT收益baseline逻辑手工计算，代码不会替用户计算。

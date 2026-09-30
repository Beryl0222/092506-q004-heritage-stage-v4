# 非遗音乐季舞台协同

面向“豫园与外滩音乐季”的演出排期服务。服务把人员、艺人、舞台、节目版本、
演出计划、资源锁、场地/艺人事件与逐级审批记录保存到本地 SQLite，应用层通过
进程内 JSON 适配器提供能力，全程只依赖 Python 标准库。

## 能力

- **登记**：艺人、舞台、人员角色（统筹者 / 审批人 / 管理员）、带版本号的节目。
- **排期**：按舞台顺序自动铺排或指定开场时间，每个节目带可配置的**缓冲时间**；
  同舞台相邻节目与同一艺人跨舞台赶场都会按缓冲窗口校验。
- **发布与锁定**：发布在一个事务内完成冲突检测；舞台与艺人时间窗加锁后，
  冲突计划**拒绝发布**，已发布计划不能重复发布。
- **冻结公开节目单**：发布把计划与引用的节目版本按版本号冻结；之后登记新
  节目版本不影响已公开的节目单。
- **场地封闭 / 艺人退出**：登记事件并自动计算受影响条目（影响范围）。
  封闭按时间窗判定，替代排期可以换到别的舞台，或挪到解封之后。
- **替代方案**：只有持 `coordinator` 角色的统筹者能创建；原计划原样保留，
  方案只替换受影响条目、保留其余条目，避免整场顺序失真。
- **逐级审批**：方案需 N 级审批人依次批准，任一级驳回即作废；申请人可在
  终审完成前**撤回**，撤回/驳回后可重建方案。终审时会基于最新链头重新
  合并并复检，因此双场事件先后审批会收敛到同一份最终计划。
- **重开恢复**：场地重开后未决方案自动撤回留痕，可通过
  `latest_effective_plan` 从替代链上最近的有效版本继续，而不是退回旧版本。
- **审计**：生成、发布、封闭、退出、创建/审批/撤回方案、重开都写入不可变事件流。

## 目录

- `src/heritage_stage/domain.py`：领域对象、模拟时钟与业务异常。
- `src/heritage_stage/store.py`：SQLite 表结构、事务与读写。
- `src/heritage_stage/service.py`：排期、发布锁定、事件、替代方案与恢复。
- `src/heritage_stage/api.py`：JSON 请求适配（动作分发与错误封装）。
- `tests/`：可重复测试，时间全部由 `FakeClock` 驱动。

## 主要动作（api.handle）

`health`、`register`（基线）、`register_user`、`register_artist`、
`register_stage`、`register_program_version`、`generate_plan`、`publish_plan`、
`get_plan`、`report_stage_closed`、`report_artist_withdrew`、
`create_contingency`、`decide_contingency`、`withdraw_contingency`、
`reopen_stage`、`latest_effective_plan`、`get_incident`、`get_contingency`、
`audit_events`。业务错误返回 `{"error": 错误码, "message": 说明}`。

## 运行

运行测试（unittest 或 pytest 均可）：

```
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m pytest -q
```

检查源码：`python3 -m compileall src tests`

测试覆盖：跨日演出与缓冲、重复发布与锁冲突、节目版本冻结、封闭影响范围、
替代方案权限、逐级审批/驳回/撤回、场地重开恢复，以及豫园与外滩双场
同时变更在两种审批顺序下收敛到同一份最终计划。

# 非遗音乐季舞台协同

面向“豫园与外滩音乐季”的排期协作服务：登记节目版本、艺人和舞台资源，
生成带缓冲时间的演出计划；发布即锁定资源，锁冲突会被拒绝；已公开的节目单
按版本号冻结；场地封闭或艺人临时退出时，只有持统筹角色者可以创建替代方案，
原计划与影响范围完整保留，并经过逐级审批后再生效；场地重新开放时能从最近的
有效版本继续排期。

## 目录

- `src/heritage_stage/domain.py` 领域对象与纯规则：模拟时钟、带缓冲排布、
  半开区间重叠判定、跨日边界、舞台/艺人冲突。
- `src/heritage_stage/store.py` SQLite 持久化：舞台、艺人、节目版本、
  计划版本链、资源锁、封闭/退出事件与影响范围、逐级审批、角色。
- `src/heritage_stage/service.py` 应用服务：登记、生成、发布、冻结、
  封闭/退出、替代方案、审批、重开续排。
- `src/heritage_stage/api.py` JSON 进程内适配层，冲突/权限/状态错误返回
  结构化错误体。
- `tests/test_baseline.py` 基线行为。
- `tests/test_schedule.py` 用 `FakeClock` 模拟时钟的完整场景测试。

## 关键规则

- **缓冲时间**：每个条目默认前 15 分钟、后 15 分钟，相邻节目前后缓冲共享
  同一段换台时间；缓冲窗口用于跨计划的资源锁冲突，半开区间相接（如 20:15
  与 20:15）不算冲突。
- **发布即加锁**：计划发布时对舞台和全部艺人按缓冲窗口加锁。任一资源与
  仍处 `published/frozen` 的计划重叠，整次发布被拒绝，计划保持草稿。
- **版本冻结**：公开节目单走逐级冻结审批（默认两级），全部通过后计划与
  引用的节目版本号均冻结；冻结版本不可覆盖、不可再发布，但锁继续拦截冲突。
- **封闭与退出**：仅 `coordinator` 角色可登记场地封闭或艺人退出。事件记录
  影响范围（计划、版本、条目），原计划置为 `void` 但内容原样保留、锁释放。
- **替代方案**：仅统筹者可建，必须走逐级 `contingency_approver` 审批，
  不能直接发布；不能仍使用封闭舞台或退出艺人；全部受影响计划（可能跨
  豫园、外滩两场）都有生效后继版本后事件才结案。
- **审批撤回**：链未全部生效前可撤回某一级，其后已通过的级别连带失效，
  可重新逐级审批；驳回为终局；全部生效后不可撤回。
- **重开续排**：场地重新开放后 `latest_effective_plan` 返回最近有效版本；
  若尚无替代方案，则返回被保留的封闭前版本（`resume_basis` 标注来源）。

## 运行

运行测试：`PYTHONPATH=src python3 -m unittest discover -s tests`

检查源码：`python3 -m compileall src tests`

项目只使用 Python 标准库，测试和运行不需要启动其他服务。

## 主要动作（JSON 适配层）

`register_artist`、`register_stage`、`register_program_version`、
`create_plan`、`publish_plan`、`freeze_publication`、`decide_approval`、
`withdraw_approval`、`close_stage`、`withdraw_artist`、
`create_contingency`、`reopen_stage`、`get_plan`、`get_event`、
`approval_status`、`grant_role`。请求中用 `actor_id` 标识操作者，
其角色通过 `grant_role` 预先授予。

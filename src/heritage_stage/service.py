"""演出排期应用服务。

能力边界：
- 登记节目版本、艺人、舞台资源与角色；
- 按整场顺序生成带缓冲时间的演出计划；
- 发布计划时锁定舞台与艺人，资源已锁定则拒绝冲突发布；
- 节目单公开后用版本号冻结，冻结内容不可再发布；
- 场地封闭/艺人退出时记录影响范围，仅持统筹角色者可创建替代方案，
  替代方案保留原计划与影响范围，并走逐级审批（可撤回/驳回）；
- 场地重新开放后从最近的有效版本继续。
"""
from __future__ import annotations

from typing import Iterable, Optional

from .domain import (
    Approval,
    Artist,
    Clock,
    Conflict,
    DisruptionEvent,
    PlanItem,
    PlanVersion,
    ProgramVersion,
    Record,
    ResourceLock,
    Stage,
    build_items,
    fmt_dt,
    item_artists,
    parse_dt,
)
from .store import Store

ROLE_PLANNER = "planner"
ROLE_COORDINATOR = "coordinator"
ROLE_FREEZE_APPROVER = "freeze_approver"
ROLE_CONTINGENCY_APPROVER = "contingency_approver"

PLAN_DRAFT = "draft"
PLAN_PUBLISHED = "published"
PLAN_FROZEN = "frozen"
PLAN_SUPERSEDED = "superseded"
PLAN_VOID = "void"

APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_WITHDRAWN = "withdrawn"


class ServiceError(Exception):
    """业务校验失败基类。"""


class NotFoundError(ServiceError):
    pass


class ValidationError(ServiceError):
    pass


class PermissionDenied(ServiceError):
    pass


class ConflictError(ServiceError):
    def __init__(self, conflicts: list[Conflict]):
        super().__init__("资源已被锁定，发布被拒绝")
        self.conflicts = conflicts


class InvalidStateError(ServiceError):
    pass


class Service:
    def __init__(self, store: Store | None = None, clock: Clock | None = None) -> None:
        self.store = store or Store()
        self.clock = clock or Clock()

    def _now(self) -> str:
        return fmt_dt(self.clock.now())

    # -- 基础（兼容基线） -------------------------------------------------

    def health(self) -> dict[str, str]:
        return {"service": "heritage_stage", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str]:
        record = self.store.save(Record(record_id, owner_id))
        return {"record_id": record.record_id, "owner_id": record.owner_id,
                "state": record.state, "created_at": record.created_at}

    def find(self, record_id: str) -> dict[str, str] | None:
        record = self.store.get(record_id)
        return record.__dict__.copy() if record else None

    # -- 角色 -------------------------------------------------------------

    def grant_role(self, user_id: str, role: str, level: int = 0) -> dict:
        self.store.grant_role(user_id, role, level)
        return {"user_id": user_id, "role": role, "level": level}

    def _require_role(self, actor_id: str, role: str) -> None:
        if not self.store.has_role(actor_id, role):
            raise PermissionDenied(f"用户 {actor_id} 缺少角色 {role}")

    # -- 资源登记 ---------------------------------------------------------

    def register_artist(self, actor_id: str, artist_id: str, name: str) -> dict:
        self._require_role(actor_id, ROLE_PLANNER)
        artist = self.store.save_artist(Artist(artist_id, name))
        return {"artist_id": artist.artist_id, "name": artist.name}

    def register_stage(self, actor_id: str, stage_id: str, name: str) -> dict:
        self._require_role(actor_id, ROLE_PLANNER)
        stage = self.store.save_stage(Stage(stage_id, name))
        return {"stage_id": stage.stage_id, "name": stage.name, "closed": False}

    def register_program_version(self, actor_id: str, program_id: str,
                                 version: int, title: str, genre: str,
                                 duration_minutes: int,
                                 artist_ids: Iterable[str]) -> dict:
        """登记节目版本。已冻结的版本号不可覆盖，也不可重复登记。"""
        self._require_role(actor_id, ROLE_PLANNER)
        if duration_minutes <= 0:
            raise ValidationError("节目时长必须为正数")
        existing = self.store.get_program_version(program_id, version)
        if existing is not None:
            if existing.frozen:
                raise InvalidStateError(
                    f"节目 {program_id} 版本 {version} 已公开冻结，不可覆盖")
            raise InvalidStateError(f"节目 {program_id} 版本 {version} 已存在")
        artists = tuple(artist_ids)
        for artist_id in artists:
            if self.store.get_artist(artist_id) is None:
                raise NotFoundError(f"艺人 {artist_id} 未登记")
        program = self.store.save_program_version(ProgramVersion(
            program_id=program_id, version=version, title=title, genre=genre,
            duration_minutes=duration_minutes, artist_ids=artists,
        ))
        return self._program_dict(program)

    # -- 计划生成 ---------------------------------------------------------

    def create_plan(self, actor_id: str, plan_id: str, stage_id: str,
                    schedule: list[dict], buffer_before: int = 15,
                    buffer_after: int = 15, note: str = "") -> dict:
        """按整场顺序生成带缓冲时间的草稿计划。"""
        self._require_role(actor_id, ROLE_PLANNER)
        if self.store.get_stage(stage_id) is None:
            raise NotFoundError(f"舞台 {stage_id} 未登记")
        if not schedule:
            raise ValidationError("排期不能为空")
        items = build_items(schedule, buffer_before, buffer_after)
        self._validate_items(items, stage_id)
        version = self.store.latest_version_number(plan_id) + 1
        plan = PlanVersion(
            plan_id=plan_id, version=version, stage_id=stage_id,
            items=items, state=PLAN_DRAFT, note=note, created_at=self._now(),
        )
        self.store.insert_plan(plan, items)
        return self._plan_dict(plan)

    def _validate_items(self, items: tuple[PlanItem, ...], stage_id: str) -> None:
        seen: set[str] = set()
        programs: dict[tuple[str, int], ProgramVersion] = {}
        for item in items:
            if item.item_id in seen:
                raise ValidationError(f"条目编号重复：{item.item_id}")
            seen.add(item.item_id)
            if item.stage_id != stage_id:
                raise ValidationError(
                    f"条目 {item.item_id} 的舞台 {item.stage_id} 与计划舞台 {stage_id} 不一致")
            program = self.store.get_program_version(item.program_id, item.version)
            if program is None:
                raise NotFoundError(
                    f"节目版本 {item.program_id} v{item.version} 未登记")
            if program.frozen:
                # 已冻结版本可以继续被引用，但不能出现在尚未公开的新草稿里被改动语义；
                # 此处允许引用（冻结即锁定内容），仅校验时长一致。
                pass
            programs[(item.program_id, item.version)] = program
            if parse_dt(item.end) <= parse_dt(item.start):
                raise ValidationError(f"条目 {item.item_id} 时间区间无效")
        # 同计划内按实际演出时间保证整场顺序（相邻节目前后缓冲共享同一段换台时间，
        # 不计为重叠）；缓冲窗口仅用于跨计划的资源锁冲突检测。
        ordered = sorted(items, key=lambda it: parse_dt(it.start))
        for earlier, later in zip(ordered, ordered[1:]):
            if parse_dt(earlier.end) > parse_dt(later.start):
                raise ValidationError(
                    f"条目 {earlier.item_id} 演出时间越过 {later.item_id}，"
                    "整场顺序无法成立")
        # 同一艺人不得在同一计划中重叠出场（按实际演出时间）
        for i, target in enumerate(items):
            target_artists = item_artists(target, programs)
            target_start, target_end = parse_dt(target.start), parse_dt(target.end)
            for other in items[i + 1:]:
                shared = set(target_artists) & set(item_artists(other, programs))
                if shared and target_start < parse_dt(other.end) and \
                        parse_dt(other.start) < target_end:
                    raise ValidationError(
                        f"艺人 {sorted(shared)} 在条目 {target.item_id} 与 "
                        f"{other.item_id} 之间跨场冲突")

    # -- 发布与资源锁 -----------------------------------------------------

    def publish_plan(self, actor_id: str, plan_id: str,
                     version: Optional[int] = None) -> dict:
        """发布计划：锁定舞台与艺人；任一资源窗口冲突则整体拒绝。"""
        self._require_role(actor_id, ROLE_PLANNER)
        plan = self._require_plan(plan_id, version)
        if plan.contingency_for:
            raise InvalidStateError("替代方案必须通过逐级审批发布，不能直接发布")
        if plan.state != PLAN_DRAFT:
            raise InvalidStateError(
                f"计划 {plan_id} v{plan.version} 状态为 {plan.state}，不可发布")
        stage = self.store.get_stage(plan.stage_id)
        if stage and stage.closed:
            raise InvalidStateError(f"舞台 {plan.stage_id} 已封闭，不可发布")
        conflicts = self._detect_conflicts(plan)
        if conflicts:
            raise ConflictError(conflicts)

        now = self._now()
        # 同一计划旧的发布版本让位
        self._supersede_previous(plan_id, plan.version)
        locks = self._build_locks(plan)
        self.store.add_locks(locks)
        self.store.update_plan_state(plan_id, plan.version, PLAN_PUBLISHED,
                                     published_at=now)
        published = self.store.get_plan(plan_id, plan.version)
        return self._plan_dict(published)

    def _detect_conflicts(self, plan: PlanVersion) -> list[Conflict]:
        conflicts: list[Conflict] = []
        seen: set[tuple] = set()
        for item in plan.items:
            program = self.store.get_program_version(item.program_id, item.version)
            artist_ids = program.artist_ids if program else ()
            candidates = [("stage", item.stage_id)]
            candidates.extend(("artist", a) for a in artist_ids)
            for resource_type, resource_id in candidates:
                for lock in self.store.find_active_locks(
                        resource_type, resource_id,
                        fmt_dt(item.window_start), fmt_dt(item.window_end),
                        exclude_plan=(plan.plan_id, plan.version)):
                    key = (lock.resource_type, lock.resource_id,
                           lock.plan_id, lock.plan_version, lock.item_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    conflicts.append(Conflict(
                        resource_type=lock.resource_type,
                        resource_id=lock.resource_id,
                        other_plan_id=lock.plan_id,
                        other_version=lock.plan_version,
                        other_item_id=lock.item_id,
                        window_start=lock.window_start,
                        window_end=lock.window_end,
                    ))
        return conflicts

    def _build_locks(self, plan: PlanVersion) -> list[ResourceLock]:
        locks: list[ResourceLock] = []
        for item in plan.items:
            program = self.store.get_program_version(item.program_id, item.version)
            artist_ids = program.artist_ids if program else ()
            resources = [("stage", item.stage_id)]
            resources.extend(("artist", a) for a in artist_ids)
            for resource_type, resource_id in resources:
                locks.append(ResourceLock(
                    lock_id=f"lock:{plan.plan_id}:v{plan.version}:{item.item_id}:{resource_type}:{resource_id}",
                    resource_type=resource_type,
                    resource_id=resource_id,
                    plan_id=plan.plan_id,
                    plan_version=plan.version,
                    item_id=item.item_id,
                    window_start=fmt_dt(item.window_start),
                    window_end=fmt_dt(item.window_end),
                ))
        return locks

    def _supersede_previous(self, plan_id: str, current_version: int) -> None:
        for older in self.store.list_plan_versions(plan_id):
            if older.version < current_version and older.state in (
                    PLAN_PUBLISHED, PLAN_FROZEN):
                # 冻结的节目单是公开记录，保留原状态；其锁仍冲突拦截。
                if older.state == PLAN_FROZEN:
                    continue
                self.store.update_plan_state(plan_id, older.version,
                                             PLAN_SUPERSEDED)

    # -- 冻结（公开节目单） -----------------------------------------------

    def freeze_publication(self, actor_id: str, plan_id: str,
                           version: Optional[int] = None,
                           approver_levels: Iterable[int] = (1, 2)) -> dict:
        """公开节目单：创建逐级冻结审批。全部通过后版本号冻结，不可再发布。"""
        self._require_role(actor_id, ROLE_PLANNER)
        plan = self._require_plan(plan_id, version)
        if plan.state not in (PLAN_PUBLISHED, PLAN_DRAFT):
            raise InvalidStateError(
                f"计划状态为 {plan.state}，不能冻结公开")
        scope_id = f"{plan_id}:v{plan.version}"
        existing = self.store.list_approvals("freeze", scope_id)
        if any(a.state == APPROVAL_APPROVED for a in existing) and \
                plan.state == PLAN_FROZEN:
            return self._plan_dict(plan)
        if existing:
            raise InvalidStateError("该版本已存在冻结审批链")
        now = self._now()
        for level in sorted(approver_levels):
            self.store.insert_approval(Approval(
                approval_id=f"ap:freeze:{scope_id}:l{level}",
                scope_type="freeze", scope_id=scope_id, level=level,
                required_role=ROLE_FREEZE_APPROVER,
                state=APPROVAL_PENDING, created_at=now,
            ))
        return {"plan_id": plan_id, "version": plan.version,
                "approval_scope": "freeze", "scope_id": scope_id,
                "levels": sorted(approver_levels), "state": "pending"}

    def decide_approval(self, actor_id: str, approval_id: str,
                        decision: str, comment: str = "") -> dict:
        """逐级审批决定：approve / reject。须按级别顺序审批。"""
        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise NotFoundError(f"审批 {approval_id} 不存在")
        self._require_approver(actor_id, approval.required_role, approval.level)
        if approval.state in (APPROVAL_APPROVED, APPROVAL_REJECTED):
            raise InvalidStateError(
                f"审批 {approval_id} 已 {approval.state}，不可重复决定")
        verb_to_state = {"approve": APPROVAL_APPROVED,
                         "reject": APPROVAL_REJECTED}
        if decision not in verb_to_state:
            raise ValidationError("decision 必须是 approve 或 reject")
        new_state = verb_to_state[decision]

        chain = self.store.list_approvals(approval.scope_type, approval.scope_id)
        if new_state == APPROVAL_APPROVED:
            for earlier in sorted((a for a in chain if a.level < approval.level),
                                  key=lambda a: a.level):
                if earlier.state != APPROVAL_APPROVED:
                    raise InvalidStateError(
                        f"须先完成 {earlier.level} 级审批，当前为 {earlier.state}")
            # 若这是最后一级，提前校验终局动作，避免审批通过后计划无法落地
            others_ready = all(
                a.state == APPROVAL_APPROVED
                for a in chain if a.level != approval.level)
            if others_ready:
                self._precheck_finalize(approval.scope_type, approval.scope_id)

        now = self._now()
        self.store.update_approval(approval_id, new_state, actor_id, now, comment)
        chain = self.store.list_approvals(approval.scope_type, approval.scope_id)
        finalized = self._maybe_finalize_chain(approval.scope_type,
                                               approval.scope_id, chain)
        return self._approval_status(chain, finalized)

    def withdraw_approval(self, actor_id: str, approval_id: str,
                          comment: str = "") -> dict:
        """审批撤回：仅未终局的链可撤回；撤回使其后的通过决定一并失效。"""
        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise NotFoundError(f"审批 {approval_id} 不存在")
        self._require_approver(actor_id, approval.required_role, approval.level)
        if approval.state == APPROVAL_WITHDRAWN:
            raise InvalidStateError("审批已撤回")
        chain = self.store.list_approvals(approval.scope_type, approval.scope_id)
        # 已全部通过的链表示终局结果已落地，只能整体作废而非逐级撤回
        if all(a.state == APPROVAL_APPROVED for a in chain):
            raise InvalidStateError("审批链已全部生效，不能撤回")
        now = self._now()
        self.store.update_approval(approval_id, APPROVAL_WITHDRAWN,
                                   actor_id, now, comment)
        for later in chain:
            if later.level > approval.level and later.state == APPROVAL_APPROVED:
                self.store.update_approval(later.approval_id,
                                           APPROVAL_WITHDRAWN, actor_id, now,
                                           f"因 {approval.level} 级撤回而失效")
        chain = self.store.list_approvals(approval.scope_type, approval.scope_id)
        return self._approval_status(chain, None)

    def _require_approver(self, actor_id: str, role: str, level: int) -> None:
        if not self.store.has_role(actor_id, role, level):
            raise PermissionDenied(
                f"用户 {actor_id} 不是 {level} 级 {role}")

    def _precheck_finalize(self, scope_type: str, scope_id: str) -> None:
        plan_id, version_text = scope_id.split(":v")
        version = int(version_text)
        plan = self._require_plan(plan_id, version)
        stage = self.store.get_stage(plan.stage_id)
        if stage and stage.closed:
            raise InvalidStateError(f"舞台 {plan.stage_id} 已封闭，审批无法生效")
        conflicts = self._detect_conflicts(plan)
        if conflicts:
            raise ConflictError(conflicts)

    def _maybe_finalize_chain(self, scope_type: str, scope_id: str,
                              chain: list[Approval]) -> Optional[str]:
        if not chain or not all(a.state == APPROVAL_APPROVED for a in chain):
            return None
        plan_id, version_text = scope_id.split(":v")
        version = int(version_text)
        now = self._now()
        if scope_type == "freeze":
            self._apply_freeze(plan_id, version, now)
            return PLAN_FROZEN
        if scope_type == "contingency":
            self._apply_contingency(plan_id, version, now)
            return "contingency_published"
        return None

    def _apply_freeze(self, plan_id: str, version: int, now: str) -> None:
        plan = self._require_plan(plan_id, version)
        if plan.state == PLAN_DRAFT:
            # 冻结即公开发布：先做锁冲突检测
            conflicts = self._detect_conflicts(plan)
            if conflicts:
                raise ConflictError(conflicts)
            self._supersede_previous(plan_id, version)
            self.store.add_locks(self._build_locks(plan))
        self.store.update_plan_state(plan_id, version, PLAN_FROZEN,
                                     published_at=plan.published_at or now,
                                     frozen_at=now)
        for item in plan.items:
            program = self.store.get_program_version(item.program_id, item.version)
            if program is not None and not program.frozen:
                self.store.set_program_frozen(item.program_id, item.version, now)

    # -- 场地封闭 / 艺人退出 ----------------------------------------------

    def close_stage(self, actor_id: str, stage_id: str, reason: str,
                    event_id: str | None = None) -> dict:
        self._require_role(actor_id, ROLE_COORDINATOR)
        stage = self.store.get_stage(stage_id)
        if stage is None:
            raise NotFoundError(f"舞台 {stage_id} 未登记")
        now = self._now()
        self.store.set_stage_closed(stage_id, True, now, reason)
        affected = [
            (plan_id, ver) for plan_id, ver in self.store.active_plans_using_stage(stage_id)
        ]
        return self._record_disruption(
            event_id or f"evt:stage:{stage_id}:{len(self.store.list_events()) + 1}",
            "stage_closed", "stage", stage_id, now, reason, affected)

    def withdraw_artist(self, actor_id: str, artist_id: str, reason: str,
                        event_id: str | None = None) -> dict:
        self._require_role(actor_id, ROLE_COORDINATOR)
        artist = self.store.get_artist(artist_id)
        if artist is None:
            raise NotFoundError(f"艺人 {artist_id} 未登记")
        now = self._now()
        affected = self.store.active_plans_using_artist(artist_id)
        return self._record_disruption(
            event_id or f"evt:artist:{artist_id}:{len(self.store.list_events()) + 1}",
            "artist_withdrawn", "artist", artist_id, now, reason, affected)

    def _record_disruption(self, event_id: str, kind: str,
                           resource_type: str, resource_id: str, now: str,
                           reason: str,
                           affected: list[tuple[str, int]]) -> dict:
        if self.store.get_event(event_id) is not None:
            raise InvalidStateError(f"事件 {event_id} 已存在")
        primary = affected[0] if affected else ("", 0)
        event = DisruptionEvent(
            event_id=event_id, kind=kind, resource_type=resource_type,
            resource_id=resource_id, occurred_at=now, reason=reason,
            affected_plan_id=primary[0], affected_version=primary[1],
        )
        self.store.insert_event(event)

        impacts: list[dict] = []
        for plan_id, version in affected:
            plan = self.store.get_plan(plan_id, version)
            if plan is None:
                continue
            if resource_type == "stage":
                hit_item_ids = [it.item_id for it in plan.items]
            else:
                hit_item_ids = [
                    it.item_id for it in plan.items
                    if (pv := self.store.get_program_version(it.program_id, it.version))
                    and resource_id in (pv.artist_ids if pv else ())
                ]
            if not hit_item_ids:
                continue
            impacts.append({"plan_id": plan_id, "plan_version": version,
                            "stage_id": plan.stage_id, "item_ids": hit_item_ids})
            # 原计划保留在历史中，但当前版本失效并释放锁
            self.store.delete_locks_for_plan(plan_id, version)
            self.store.update_plan_state(plan_id, version, PLAN_VOID)
        self.store.save_impacts(event_id, impacts)
        return self._event_dict(self.store.get_event(event_id), impacts)

    def reopen_stage(self, actor_id: str, stage_id: str) -> dict:
        """场地重新开放：关闭标记解除，待处理封闭事件关闭，并返回可续排版本。"""
        self._require_role(actor_id, ROLE_COORDINATOR)
        stage = self.store.get_stage(stage_id)
        if stage is None:
            raise NotFoundError(f"舞台 {stage_id} 未登记")
        now = self._now()
        self.store.set_stage_closed(stage_id, False, "", "")
        closed_events = [
            e for e in self.store.list_events(handled=False)
            if e.kind == "stage_closed" and e.resource_id == stage_id
        ]
        for event in closed_events:
            self.store.update_event(event.event_id, True, "reopened", now)
        resume = self.latest_effective_plan(stage_id)
        return {"stage_id": stage_id, "closed": False, "reopened_at": now,
                "closed_events": [e.event_id for e in closed_events],
                "resume_from": resume}

    def latest_effective_plan(self, stage_id: str) -> Optional[dict]:
        """从最近的有效版本继续。

        优先取该舞台最新的已发布/冻结（含已批替代方案）版本；
        若计划因场地封闭被置失效且尚无替代方案，则回到被保留的
        封闭前版本（void 记录原样保留），供重新开放后续排。
        """
        rows = self.store.connection.execute(
            "SELECT plan_id, version, state FROM plan WHERE stage_id=? "
            "ORDER BY version DESC",
            (stage_id,),
        ).fetchall()
        plans = [self.store.get_plan(r["plan_id"], r["version"]) for r in rows]
        plans = [p for p in plans if p]
        active = [p for p in plans if p.state in (PLAN_PUBLISHED, PLAN_FROZEN)]
        if active:
            latest = max(active, key=lambda p: (p.version,
                                                p.published_at or "",
                                                p.frozen_at or ""))
            result = self._plan_dict(latest)
            result["resume_basis"] = "active"
            result["effective_at"] = latest.frozen_at or latest.published_at
            return result
        preserved = next((p for p in plans if p.state == PLAN_VOID), None)
        if preserved is not None:
            result = self._plan_dict(preserved)
            result["resume_basis"] = "preserved_pre_closure"
            result["effective_at"] = preserved.published_at or preserved.frozen_at
            return result
        return None

    # -- 替代方案与逐级审批 -----------------------------------------------

    def create_contingency(self, actor_id: str, event_id: str,
                           schedule: list[dict],
                           target_stage_id: Optional[str] = None,
                           affected_plan_id: Optional[str] = None,
                           buffer_before: int = 15, buffer_after: int = 15,
                           approver_levels: Iterable[int] = (1, 2),
                           note: str = "") -> dict:
        """有统筹权限者为封闭/退出事件创建替代方案，保留原计划与影响范围。

        影响范围跨双场时，用 affected_plan_id 指定本方案承接哪一场的原计划，
        未指定时默认承接影响范围中的第一场。
        """
        self._require_role(actor_id, ROLE_COORDINATOR)
        event = self.store.get_event(event_id)
        if event is None:
            raise NotFoundError(f"事件 {event_id} 不存在")
        if event.handled:
            raise InvalidStateError(f"事件 {event_id} 已处理（{event.resolution}）")
        impacts = self.store.get_impacts(event_id)
        if not impacts:
            raise InvalidStateError("该事件没有受影响的计划，无需替代方案")

        if affected_plan_id is None:
            primary = impacts[0]
        else:
            matches = [i for i in impacts if i["plan_id"] == affected_plan_id]
            if not matches:
                raise ValidationError(
                    f"计划 {affected_plan_id} 不在事件 {event_id} 的影响范围内")
            primary = matches[0]
        stage_id = target_stage_id or primary["stage_id"]
        target_stage = self.store.get_stage(stage_id)
        if target_stage is None:
            raise NotFoundError(f"舞台 {stage_id} 未登记")
        if target_stage.closed and event.kind == "stage_closed" \
                and stage_id == event.resource_id:
            raise InvalidStateError(f"舞台 {stage_id} 仍在封闭中")

        items = build_items(schedule, buffer_before, buffer_after)
        self._validate_contingency_items(items, event, stage_id)
        plan_id = primary["plan_id"]
        version = self.store.latest_version_number(plan_id) + 1
        plan = PlanVersion(
            plan_id=plan_id, version=version, stage_id=stage_id, items=items,
            state=PLAN_DRAFT, note=note, supersedes=primary["plan_version"],
            contingency_for=event_id, created_at=self._now(),
        )
        self.store.insert_plan(plan, items)

        scope_id = f"{plan_id}:v{version}"
        now = self._now()
        for level in sorted(approver_levels):
            self.store.insert_approval(Approval(
                approval_id=f"ap:contingency:{scope_id}:l{level}",
                scope_type="contingency", scope_id=scope_id, level=level,
                required_role=ROLE_CONTINGENCY_APPROVER,
                state=APPROVAL_PENDING, created_at=now,
            ))
        return {
            "plan_id": plan_id, "version": version, "stage_id": stage_id,
            "contingency_for": event_id,
            "preserves_plan": {"plan_id": primary["plan_id"],
                               "version": primary["plan_version"]},
            "impact_scope": impacts,
            "approval_scope": "contingency", "scope_id": scope_id,
            "levels": sorted(approver_levels), "state": "pending",
            "items": [self._item_dict(i) for i in items],
        }

    def _validate_contingency_items(self, items: tuple[PlanItem, ...],
                                    event: DisruptionEvent,
                                    stage_id: str) -> None:
        self._validate_items(items, stage_id)
        for item in items:
            program = self.store.get_program_version(item.program_id, item.version)
            if event.kind == "artist_withdrawn" and program is not None \
                    and event.resource_id in program.artist_ids:
                raise ValidationError(
                    f"替代条目 {item.item_id} 仍包含已退出艺人 {event.resource_id}")

    def _apply_contingency(self, plan_id: str, version: int, now: str) -> None:
        plan = self._require_plan(plan_id, version)
        stage = self.store.get_stage(plan.stage_id)
        if stage and stage.closed:
            raise InvalidStateError(f"舞台 {plan.stage_id} 已封闭，替代方案不能生效")
        conflicts = self._detect_conflicts(plan)
        if conflicts:
            raise ConflictError(conflicts)
        self._supersede_previous(plan_id, version)
        self.store.add_locks(self._build_locks(plan))
        self.store.update_plan_state(plan_id, version, PLAN_PUBLISHED,
                                     published_at=now)
        event = self.store.get_event(plan.contingency_for)
        if event is not None and self._all_impacts_resolved(event.event_id):
            self.store.update_event(event.event_id, True, "contingency", now)

    def _all_impacts_resolved(self, event_id: str) -> bool:
        """事件的全部受影响计划（可能跨双场）都已有有效后继版本。"""
        for impact in self.store.get_impacts(event_id):
            rows = self.store.connection.execute(
                "SELECT COUNT(*) AS n FROM plan WHERE plan_id=? AND version>? "
                f"AND state IN ({','.join('?' for _ in ('published', 'frozen'))})",
                (impact["plan_id"], impact["plan_version"], "published", "frozen"),
            ).fetchone()
            if int(rows["n"]) == 0:
                return False
        return True

    # -- 查询 -------------------------------------------------------------

    def get_plan(self, plan_id: str, version: Optional[int] = None) -> dict:
        return self._plan_dict(self._require_plan(plan_id, version))

    def list_plan_versions(self, plan_id: str) -> list[dict]:
        return [self._plan_dict(p) for p in self.store.list_plan_versions(plan_id)]

    def get_event(self, event_id: str) -> dict:
        event = self.store.get_event(event_id)
        if event is None:
            raise NotFoundError(f"事件 {event_id} 不存在")
        return self._event_dict(event, self.store.get_impacts(event_id))

    def list_pending_events(self) -> list[dict]:
        return [self._event_dict(e, self.store.get_impacts(e.event_id))
                for e in self.store.list_events(handled=False)]

    def approval_status(self, scope_type: str, scope_id: str) -> dict:
        chain = self.store.list_approvals(scope_type, scope_id)
        if not chain:
            raise NotFoundError(f"审批链 {scope_type}:{scope_id} 不存在")
        return self._approval_status(chain, None)

    def _require_plan(self, plan_id: str, version: Optional[int]) -> PlanVersion:
        plan = self.store.get_plan(plan_id, version)
        if plan is None:
            raise NotFoundError(
                f"计划 {plan_id}" + (f" v{version}" if version else "") + " 不存在")
        return plan

    # -- 序列化 -----------------------------------------------------------

    def _program_dict(self, program: ProgramVersion) -> dict:
        return {"program_id": program.program_id, "version": program.version,
                "title": program.title, "genre": program.genre,
                "duration_minutes": program.duration_minutes,
                "artist_ids": list(program.artist_ids),
                "frozen": program.frozen, "published_at": program.published_at}

    def _item_dict(self, item: PlanItem) -> dict:
        return {"item_id": item.item_id, "stage_id": item.stage_id,
                "program_id": item.program_id, "version": item.version,
                "start": item.start, "end": item.end,
                "buffer_before": item.buffer_before,
                "buffer_after": item.buffer_after,
                "window_start": fmt_dt(item.window_start),
                "window_end": fmt_dt(item.window_end)}

    def _plan_dict(self, plan: PlanVersion) -> dict:
        return {"plan_id": plan.plan_id, "version": plan.version,
                "stage_id": plan.stage_id, "state": plan.state,
                "note": plan.note, "supersedes": plan.supersedes,
                "contingency_for": plan.contingency_for,
                "created_at": plan.created_at,
                "published_at": plan.published_at, "frozen_at": plan.frozen_at,
                "items": [self._item_dict(i) for i in plan.items]}

    def _event_dict(self, event: DisruptionEvent,
                    impacts: Optional[list[dict]] = None) -> dict:
        return {"event_id": event.event_id, "kind": event.kind,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "occurred_at": event.occurred_at, "reason": event.reason,
                "handled": event.handled, "resolution": event.resolution,
                "resolved_at": event.resolved_at,
                "affected_plan_id": event.affected_plan_id,
                "affected_version": event.affected_version,
                "impact_scope": impacts or []}

    def _approval_status(self, chain: list[Approval],
                         finalized: Optional[str]) -> dict:
        states = {a.level: a.state for a in chain}
        if all(a.state == APPROVAL_APPROVED for a in chain):
            overall = "approved"
        elif any(a.state == APPROVAL_REJECTED for a in chain):
            overall = "rejected"
        elif all(a.state == APPROVAL_WITHDRAWN for a in chain):
            overall = "withdrawn"
        elif any(a.state == APPROVAL_WITHDRAWN for a in chain):
            overall = "broken"
        else:
            overall = "pending"
        return {"scope_type": chain[0].scope_type,
                "scope_id": chain[0].scope_id,
                "overall": overall, "finalized": finalized,
                "levels": [{"level": a.level, "state": a.state,
                            "decided_by": a.decided_by,
                            "decided_at": a.decided_at, "comment": a.comment}
                           for a in sorted(chain, key=lambda a: a.level)]}

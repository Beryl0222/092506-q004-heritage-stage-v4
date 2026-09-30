"""演出排期应用服务。

覆盖统筹人员的完整工作流：

* 登记艺人、舞台与节目版本；
* 生成带缓冲时间的整场计划，发布时对舞台/艺人时间窗加锁，冲突拒绝发布；
* 已发布（公开）计划与其节目版本按版本号冻结，不可修改；
* 场地封闭 / 艺人临时退出时登记事件与影响范围，仅持统筹角色者可创建替代方案；
* 替代方案保留原计划，走逐级审批，可在终审前撤回；
* 审批通过后生成新版本计划并接管资源锁；场地重开后可从最近的有效版本继续。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .domain import (
    APPROVAL_APPROVE,
    APPROVAL_DENIED,
    APPROVAL_DONE,
    APPROVAL_PENDING,
    APPROVAL_REJECT,
    Clock,
    ConflictError,
    CONTINGENCY_APPROVED,
    CONTINGENCY_PROPOSED,
    CONTINGENCY_REJECTED,
    CONTINGENCY_WITHDRAWN,
    Incident,
    ImpactedItem,
    InvalidTransitionError,
    NotFoundError,
    PermissionDeniedError,
    PLAN_DRAFT,
    PLAN_PUBLISHED,
    PLAN_SUPERSEDED,
    ApprovalStep,
    Artist,
    Contingency,
    DuplicateError,
    Plan,
    ProgramVersion,
    Record,
    ResourceLock,
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_COORDINATOR,
    Slot,
    Stage,
    User,
    ValidationError,
    format_time,
    parse_time,
)
from .store import Store

DEFAULT_BUFFER_MINUTES = 15


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Service:
    def __init__(self, store: Store | None = None, clock: Clock | None = None) -> None:
        self.store = store or Store()
        self.clock = clock or Clock()

    def _now(self) -> str:
        return format_time(self.clock.now())

    # ------------------------------------------------------------ 基线能力

    def health(self) -> dict[str, str]:
        return {"service": "heritage_stage", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str]:
        record = self.store.save(Record(record_id, owner_id))
        return {"record_id": record.record_id, "owner_id": record.owner_id,
                "state": record.state, "created_at": record.created_at}

    def find(self, record_id: str) -> dict[str, str] | None:
        record = self.store.get(record_id)
        return record.__dict__.copy() if record else None

    # ------------------------------------------------------------ 人员

    def register_user(self, user_id: str, name: str = "",
                      roles: Sequence[str] = ()) -> dict[str, Any]:
        user = User(user_id, name, tuple(roles))
        self.store.save_user(user, self._now())
        return self._user_dict(user)

    def _require_user(self, user_id: str) -> User:
        user = self.store.get_user(user_id)
        if user is None:
            raise NotFoundError(f"用户不存在: {user_id}")
        return user

    def _require_role(self, user_id: str, role: str) -> User:
        user = self._require_user(user_id)
        if not user.has_role(role):
            raise PermissionDeniedError(
                f"用户 {user_id} 缺少 {role} 权限，当前角色: {', '.join(user.roles)}")
        return user

    # ------------------------------------------------------------ 艺人 / 舞台

    def register_artist(self, artist_id: str, name: str, genre: str = "") -> dict[str, Any]:
        if self.store.get_artist(artist_id) is not None:
            raise DuplicateError(f"艺人已登记: {artist_id}")
        artist = self.store.save_artist(Artist(artist_id, name, genre), self._now())
        return self._artist_dict(artist)

    def register_stage(self, stage_id: str, name: str, location: str = "") -> dict[str, Any]:
        if self.store.get_stage(stage_id) is not None:
            raise DuplicateError(f"舞台已登记: {stage_id}")
        stage = self.store.save_stage(Stage(stage_id, name, location), self._now())
        return self._stage_dict(stage)

    # ------------------------------------------------------------ 节目版本

    def register_program_version(
        self,
        program_id: str,
        title: str,
        artist_ids: Sequence[str],
        duration_minutes: int,
        category: str = "",
        actor_id: str = "",
        version: int | None = None,
    ) -> dict[str, Any]:
        if duration_minutes <= 0:
            raise ValidationError("节目时长必须为正数")
        artist_ids = tuple(artist_ids)
        if not artist_ids:
            raise ValidationError("节目至少需要一名艺人")
        for artist_id in artist_ids:
            if self.store.get_artist(artist_id) is None:
                raise NotFoundError(f"艺人未登记: {artist_id}")
        latest = self.store.get_latest_program(program_id)
        if version is None:
            version = (latest.version + 1) if latest else 1
        elif latest is not None and version <= latest.version:
            raise DuplicateError(f"节目 {program_id} 的 v{version} 已存在")
        pv = ProgramVersion(
            program_id=program_id, version=version, title=title,
            artist_ids=artist_ids, duration_minutes=duration_minutes,
            category=category, published=False, created_by=actor_id,
            created_at=self._now(),
        )
        self.store.insert_program(pv)
        return self._program_dict(pv)

    def _resolve_program(self, program_id: str, version: int | None) -> ProgramVersion:
        pv = (self.store.get_program(program_id, version) if version is not None
              else self.store.get_latest_program(program_id))
        if pv is None:
            label = f"v{version}" if version is not None else "最新版本"
            raise NotFoundError(f"节目版本不存在: {program_id} {label}")
        return pv

    # ------------------------------------------------------------ 生成计划（含缓冲）

    def generate_plan(
        self,
        label: str,
        entries: Sequence[dict[str, Any]],
        actor_id: str,
        plan_start: str | None = None,
        default_buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
        stage_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """根据节目条目生成草稿计划。

        每个条目: ``program_id``、可选 ``version``（默认最新）、``stage_id``、
        可选 ``starts_at``（缺省时按舞台顺序自动铺排）、可选 ``buffer_minutes``。
        同一舞台相邻节目、以及跨舞台的同一艺人，都按缓冲时间做重叠校验。
        """
        if not entries:
            raise ValidationError("计划至少包含一个节目条目")
        if actor_id:
            self._require_user(actor_id)
        if default_buffer_minutes < 0:
            raise ValidationError("缓冲时间不能为负")

        slots: list[Slot] = []
        stage_cursor: dict[str, Any] = {}
        global_start = parse_time(plan_start) if plan_start else None
        known_stages = set(stage_ids or [])

        for index, entry in enumerate(entries):
            stage = self.store.get_stage(str(entry["stage_id"]))
            if stage is None:
                raise NotFoundError(f"舞台未登记: {entry['stage_id']}")
            known_stages.add(stage.stage_id)

            pv = self._resolve_program(str(entry["program_id"]),
                                       entry.get("version"))
            buffer_minutes = int(entry.get("buffer_minutes", default_buffer_minutes))
            if buffer_minutes < 0:
                raise ValidationError("缓冲时间不能为负")

            start_raw = entry.get("starts_at")
            if start_raw is not None:
                start = parse_time(start_raw)
            elif stage.stage_id in stage_cursor:
                start = stage_cursor[stage.stage_id]
            elif global_start is not None:
                start = global_start
            else:
                raise ValidationError(
                    f"第 {index + 1} 个条目缺少 starts_at，且没有 plan_start")

            cursor = stage_cursor.get(stage.stage_id)
            if cursor is not None and start < cursor:
                raise ConflictError(
                    f"舞台 {stage.stage_id} 上的《{pv.title}》开场早于上一个节目的缓冲结束")

            end = start + timedelta(minutes=pv.duration_minutes)
            reason = self._stage_unavailable_reason(
                stage.stage_id, start,
                end + timedelta(minutes=buffer_minutes))
            if reason:
                raise ConflictError(reason)
            slot = Slot(
                item_id=_new_id("item"), program_id=pv.program_id,
                program_version=pv.version, title=pv.title,
                artist_ids=pv.artist_ids, stage_id=stage.stage_id,
                starts_at=format_time(start), ends_at=format_time(end),
                buffer_minutes=buffer_minutes,
            )
            for prior in slots:
                shared = prior.resources() & slot.resources()
                if shared and slot.overlaps(prior):
                    raise ConflictError(
                        f"《{slot.title}》与《{prior.title}》在资源 "
                        f"{sorted(shared)} 上的时间窗（含 {buffer_minutes} 分钟缓冲）冲突")
            slots.append(slot)
            stage_cursor[stage.stage_id] = end + timedelta(minutes=buffer_minutes)

        plan = Plan(
            plan_id=_new_id("plan"), stage_ids=tuple(sorted(known_stages)),
            version=1, state=PLAN_DRAFT, label=label, slots=slots,
            created_by=actor_id, created_at=self._now(),
        )
        with self.store.transaction():
            self.store.insert_plan(plan)
            self.store.append_event(self._now(), actor_id or "system",
                                    "plan_generated",
                                    {"plan_id": plan.plan_id, "label": label})
        return self._plan_dict(plan)

    # ------------------------------------------------------------ 发布 / 锁定 / 冻结

    def publish_plan(self, plan_id: str, actor_id: str = "") -> dict[str, Any]:
        plan = self._require_plan(plan_id)
        if actor_id:
            self._require_user(actor_id)
        if plan.state == PLAN_PUBLISHED:
            raise ConflictError(f"计划 {plan_id} 已发布并冻结，不能重复发布")
        if plan.state != PLAN_DRAFT:
            raise InvalidTransitionError(
                f"计划 {plan_id} 当前状态为 {plan.state}，不能发布")
        for slot in plan.slots:
            reason = self._stage_unavailable_reason(
                slot.stage_id, slot.start, slot.buffer_end)
            if reason:
                raise ConflictError(f"{reason}，发布被拒绝")

        windows = self._lock_windows(plan.slots)
        with self.store.transaction():
            conflicts = self.store.find_lock_conflicts(windows)
            if conflicts:
                clash = conflicts[0]
                raise ConflictError(
                    f"资源 {clash.resource} 在 {clash.starts_at}~{clash.ends_at} "
                    f"已被计划 {clash.plan_id} 锁定，发布被拒绝")
            for slot in plan.slots:
                pv = self.store.get_program(slot.program_id, slot.program_version)
                if pv is not None and not pv.published:
                    self.store.mark_program_published(pv.program_id, pv.version)
                self.store.insert_lock(ResourceLock(
                    plan_id=plan.plan_id,
                    resource=f"stage:{slot.stage_id}",
                    starts_at=slot.starts_at,
                    ends_at=format_time(slot.buffer_end),
                ))
                for artist_id in slot.artist_ids:
                    self.store.insert_lock(ResourceLock(
                        plan_id=plan.plan_id, resource=f"artist:{artist_id}",
                        starts_at=slot.starts_at,
                        ends_at=format_time(slot.buffer_end),
                    ))
            self.store.update_plan_state(plan.plan_id, PLAN_PUBLISHED,
                                         published_at=self._now())
            self.store.append_event(self._now(), actor_id or "system",
                                    "plan_published",
                                    {"plan_id": plan.plan_id,
                                     "programs": sorted({s.program_id for s in plan.slots})})
        plan.state = PLAN_PUBLISHED
        plan.published_at = self._now()
        return self._plan_dict(plan)

    @staticmethod
    def _lock_windows(slots: Sequence[Slot]) -> list[tuple[str, str, str]]:
        windows: list[tuple[str, str, str]] = []
        for slot in slots:
            end = format_time(slot.buffer_end)
            windows.append((f"stage:{slot.stage_id}", slot.starts_at, end))
            for artist_id in slot.artist_ids:
                windows.append((f"artist:{artist_id}", slot.starts_at, end))
        return windows

    def _require_plan(self, plan_id: str) -> Plan:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise NotFoundError(f"计划不存在: {plan_id}")
        return plan

    def _stage_unavailable_reason(self, stage_id: str,
                                  start: datetime, end: datetime) -> str:
        """返回舞台在给定时间窗与封闭区间重叠时的原因，否则空串。

        未给出解封时刻（ends_at 为空）的封闭视为从 starts_at 起无限封闭。
        """
        stage = self.store.get_stage(stage_id)
        if stage is None:
            return f"舞台未登记: {stage_id}"
        far_future = datetime(9999, 1, 1, tzinfo=timezone.utc)
        for incident in self.store.list_open_stage_incidents(stage_id):
            window_start = parse_time(incident.starts_at)
            window_end = (parse_time(incident.ends_at)
                          if incident.ends_at else far_future)
            if start < window_end and end > window_start:
                return (f"舞台 {stage_id} 在 {incident.starts_at}"
                        f"~{incident.ends_at or '解封时间未定'} 封闭")
        return ""

    # ------------------------------------------------------------ 场地封闭 / 艺人退出

    def report_stage_closed(self, stage_id: str, plan_id: str, actor_id: str,
                            starts_at: str | None = None,
                            ends_at: str = "") -> dict[str, Any]:
        stage = self.store.get_stage(stage_id)
        if stage is None:
            raise NotFoundError(f"舞台未登记: {stage_id}")
        target = self._require_plan(plan_id)
        if target.state != PLAN_PUBLISHED:
            raise InvalidTransitionError("只能针对已发布的计划登记场地封闭")
        # 影响范围始终按当前链头（最新有效版本）计算，支持变更链上连续出事。
        head = self._chain_head(target)
        start = parse_time(starts_at) if starts_at else self.clock.now()
        impacted = [
            ImpactedItem(head.plan_id, s.item_id,
                         f"舞台 {stage_id} 自 {format_time(start)} 起封闭")
            for s in head.slots
            if s.stage_id == stage_id
            and parse_time(s.starts_at) >= start
            and (not ends_at or parse_time(s.starts_at) < parse_time(ends_at))
        ]
        incident = Incident(
            incident_id=_new_id("inc"), kind="stage_closed", ref_id=stage_id,
            plan_id=head.plan_id, starts_at=format_time(start), ends_at=ends_at,
            opened_by=actor_id, opened_at=self._now(), impacted=impacted,
        )
        with self.store.transaction():
            self.store.set_stage_closed(stage_id, True)
            self.store.insert_incident(incident)
            self.store.append_event(self._now(), actor_id, "stage_closed", {
                "stage_id": stage_id, "plan_id": head.plan_id,
                "impacted_item_ids": [i.item_id for i in impacted]})
        return self._incident_dict(incident)

    def report_artist_withdrew(self, artist_id: str, plan_id: str, actor_id: str,
                               starts_at: str | None = None) -> dict[str, Any]:
        artist = self.store.get_artist(artist_id)
        if artist is None:
            raise NotFoundError(f"艺人未登记: {artist_id}")
        target = self._require_plan(plan_id)
        if target.state != PLAN_PUBLISHED:
            raise InvalidTransitionError("只能针对已发布的计划登记艺人退出")
        head = self._chain_head(target)
        start = parse_time(starts_at) if starts_at else self.clock.now()
        impacted = [
            ImpactedItem(head.plan_id, s.item_id,
                         f"艺人 {artist_id} 自 {format_time(start)} 起退出")
            for s in head.slots
            if artist_id in s.artist_ids and parse_time(s.starts_at) >= start
        ]
        if not impacted:
            raise ValidationError(f"艺人 {artist_id} 在当前有效计划中没有受影响的后续场次")
        incident = Incident(
            incident_id=_new_id("inc"), kind="artist_withdrew", ref_id=artist_id,
            plan_id=head.plan_id, starts_at=format_time(start),
            opened_by=actor_id, opened_at=self._now(), impacted=impacted,
        )
        with self.store.transaction():
            self.store.insert_incident(incident)
            self.store.append_event(self._now(), actor_id, "artist_withdrew", {
                "artist_id": artist_id, "plan_id": head.plan_id,
                "impacted_item_ids": [i.item_id for i in impacted]})
        return self._incident_dict(incident)

    # ------------------------------------------------------------ 替代方案

    def create_contingency(self, incident_id: str, actor_id: str,
                           entries: Sequence[dict[str, Any]],
                           required_levels: int = 2,
                           note: str = "",
                           default_buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
                           plan_start: str | None = None) -> dict[str, Any]:
        """仅统筹者可创建。``entries`` 给出受影响之后的完整替代排期。"""
        coordinator = self._require_role(actor_id, ROLE_COORDINATOR)
        if required_levels < 1:
            raise ValidationError("至少需要一级审批")
        incident = self.store.get_incident(incident_id)
        if incident is None:
            raise NotFoundError(f"事件不存在: {incident_id}")
        if incident.resolved:
            raise InvalidTransitionError("事件已结束，不能再创建替代方案")
        existing = self.store.list_contingencies(incident_id)
        if any(c.state == CONTINGENCY_PROPOSED for c in existing):
            raise ConflictError("该事件已有待审批的替代方案")
        original = self._require_plan(incident.plan_id)
        head = self._chain_head(original)
        replacement_slots = self._build_contingency_slots(
            entries, incident, default_buffer_minutes, plan_start)
        # 未受影响的原条目保留，受影响条目由替代条目接管，保证整场顺序不失真。
        merged_slots = self._merge_replacement_slots(head, incident,
                                                     replacement_slots)
        # 与原计划之外的已发布锁做预检；终审通过时还会在事务内复检。
        windows = self._lock_windows(merged_slots)
        conflicts = self.store.find_lock_conflicts(windows,
                                                   exclude_plan_id=head.plan_id)
        if conflicts:
            clash = conflicts[0]
            raise ConflictError(
                f"替代排期与计划 {clash.plan_id} 对资源 {clash.resource} 的锁定冲突")

        steps = [ApprovalStep(level=lv, approver_id="", status=APPROVAL_PENDING)
                 for lv in range(1, required_levels + 1)]
        contingency = Contingency(
            contingency_id=_new_id("ctg"), incident_id=incident_id,
            plan_id=original.plan_id, created_by=coordinator.user_id,
            created_at=self._now(), required_levels=required_levels,
            state=CONTINGENCY_PROPOSED, slots=merged_slots, steps=steps,
            note=note,
        )
        with self.store.transaction():
            self.store.insert_contingency(contingency)
            self.store.append_event(self._now(), actor_id, "contingency_created", {
                "contingency_id": contingency.contingency_id,
                "incident_id": incident_id, "original_plan_id": original.plan_id,
                "head_plan_id": head.plan_id,
                "impacted_item_ids": [i.item_id for i in incident.impacted],
                "retained_item_ids": [s.item_id for s in head.slots
                                      if s.item_id not in
                                      {i.item_id for i in incident.impacted}],
                "required_levels": required_levels, "note": note})
        return self._contingency_dict(contingency)

    @staticmethod
    def _merge_replacement_slots(head: Plan, incident: Incident,
                                 replacement_slots: Sequence[Slot]) -> list[Slot]:
        """链头计划去掉本事件影响的条目，再并入替代条目并做整体校验。

        事件可能针对链上较早版本登记（双场先后变更），其影响条目在链头中
        可能已被前一个替代方案替换，按 item_id 求交时自动忽略。
        """
        impacted_ids = {i.item_id for i in incident.impacted}
        retained = [s for s in head.slots if s.item_id not in impacted_ids]
        merged = sorted(retained + list(replacement_slots),
                        key=lambda s: (s.stage_id, s.starts_at))
        for i, earlier in enumerate(merged):
            for later in merged[i + 1:]:
                shared = earlier.resources() & later.resources()
                if shared and later.overlaps(earlier):
                    raise ConflictError(
                        f"替代排期与保留条目冲突：《{later.title}》与《{earlier.title}》"
                        f"在资源 {sorted(shared)} 上的时间窗（含缓冲）重叠")
        return merged

    def _build_contingency_slots(self, entries: Sequence[dict[str, Any]],
                                 incident: Incident,
                                 default_buffer_minutes: int,
                                 plan_start: str | None) -> list[Slot]:
        if not entries:
            raise ValidationError("替代方案至少包含一个节目条目")
        slots: list[Slot] = []
        stage_cursor: dict[str, Any] = {}
        global_start = parse_time(plan_start) if plan_start else None
        for index, entry in enumerate(entries):
            stage = self.store.get_stage(str(entry["stage_id"]))
            if stage is None:
                raise NotFoundError(f"舞台未登记: {entry['stage_id']}")
            pv = self._resolve_program(str(entry["program_id"]),
                                       entry.get("version"))
            if (incident.kind == "artist_withdrew"
                    and incident.ref_id in pv.artist_ids):
                raise ConflictError(
                    f"替代节目仍包含已退出艺人 {incident.ref_id}")
            buffer_minutes = int(entry.get("buffer_minutes", default_buffer_minutes))
            start_raw = entry.get("starts_at")
            if start_raw is not None:
                start = parse_time(start_raw)
            elif stage.stage_id in stage_cursor:
                start = stage_cursor[stage.stage_id]
            elif global_start is not None:
                start = global_start
            else:
                raise ValidationError(
                    f"第 {index + 1} 个条目缺少 starts_at，且没有 plan_start")
            cursor = stage_cursor.get(stage.stage_id)
            if cursor is not None and start < cursor:
                raise ConflictError(
                    f"舞台 {stage.stage_id} 上的《{pv.title}》开场早于上一个节目的缓冲结束")
            end = start + timedelta(minutes=pv.duration_minutes)
            # 封闭按时间窗判定：可以把节目改到解封之后，或换到别的舞台。
            reason = self._stage_unavailable_reason(
                stage.stage_id, start, end + timedelta(minutes=buffer_minutes))
            if reason:
                raise ConflictError(reason)
            slot = Slot(
                item_id=_new_id("item"), program_id=pv.program_id,
                program_version=pv.version, title=pv.title,
                artist_ids=pv.artist_ids, stage_id=stage.stage_id,
                starts_at=format_time(start), ends_at=format_time(end),
                buffer_minutes=buffer_minutes, is_replacement=True,
            )
            for prior in slots:
                shared = prior.resources() & slot.resources()
                if shared and slot.overlaps(prior):
                    raise ConflictError(
                        f"替代排期中《{slot.title}》与《{prior.title}》资源冲突: "
                        f"{sorted(shared)}")
            slots.append(slot)
            stage_cursor[stage.stage_id] = end + timedelta(minutes=buffer_minutes)
        return slots

    # ------------------------------------------------------------ 逐级审批 / 撤回

    def decide_contingency(self, contingency_id: str, approver_id: str,
                           action: str, comment: str = "") -> dict[str, Any]:
        self._require_role(approver_id, ROLE_APPROVER)
        contingency = self._require_contingency(contingency_id)
        if contingency.state != CONTINGENCY_PROPOSED:
            raise InvalidTransitionError(
                f"替代方案当前状态为 {contingency.state}，不能再审批")
        if action not in (APPROVAL_APPROVE, APPROVAL_REJECT):
            raise ValidationError("审批动作只能是 approve 或 reject")
        level = contingency.current_level()
        if action == APPROVAL_REJECT:
            with self.store.transaction():
                self.store._upsert_step(
                    contingency_id,
                    ApprovalStep(level, approver_id, APPROVAL_DENIED,
                                 comment, self._now()))
                self.store.update_contingency(contingency_id,
                                              state=CONTINGENCY_REJECTED)
                self.store.append_event(self._now(), approver_id,
                                        "contingency_rejected",
                                        {"contingency_id": contingency_id,
                                         "level": level, "comment": comment})
            return self.get_contingency(contingency_id)

        return self._approve_level(contingency, approver_id, level, comment)

    def _approve_level(self, contingency: Contingency, approver_id: str,
                       level: int, comment: str) -> dict[str, Any]:
        with self.store.transaction():
            self.store._upsert_step(
                contingency.contingency_id,
                ApprovalStep(level, approver_id, APPROVAL_DONE,
                             comment, self._now()))
            self.store.append_event(self._now(), approver_id,
                                    "contingency_level_approved",
                                    {"contingency_id": contingency.contingency_id,
                                     "level": level,
                                     "required_levels": contingency.required_levels})
            if level < contingency.required_levels:
                return self.get_contingency(contingency.contingency_id)
            return self._finalize_contingency(contingency, approver_id, comment)

    def _finalize_contingency(self, contingency: Contingency,
                              approver_id: str, comment: str) -> dict[str, Any]:
        """终审通过：以最新链头为基准重新合并并复检，然后发布新版本计划。"""
        original = self._require_plan(contingency.plan_id)
        head = self._chain_head(original)

        # 封闭舞台 / 退出艺人的限制在终审时仍然生效。
        incident = self.store.get_incident(contingency.incident_id)
        if incident is None or incident.resolved:
            raise InvalidTransitionError("触发事件已结束（如场地已重开），替代方案失效")

        # 创建方案后链头可能已被另一事件的方案推进，这里基于最新链头重新合并，
        # 只带入本方案的替代条目，保证双场先后变更时整场顺序不丢不乱。
        replacement_slots = [s for s in contingency.slots if s.is_replacement]
        final_slots = self._merge_replacement_slots(head, incident,
                                                    replacement_slots)
        # 终审只校验“本事件”的约束是否仍生效；其它并发事件由各自的替代方案解决，
        # 允许中间版本暂时仍受其影响（latest_effective_plan 会跳过不可执行版本）。
        for slot in final_slots:
            if incident.kind == "stage_closed" and slot.stage_id == incident.ref_id:
                reason = self._stage_unavailable_reason(
                    slot.stage_id, slot.start, slot.buffer_end)
                if reason:
                    raise ConflictError(f"{reason}，替代方案无法生效")
            if (incident.kind == "artist_withdrew"
                    and incident.ref_id in slot.artist_ids):
                raise ConflictError(
                    f"替代方案仍包含已退出艺人 {incident.ref_id}")

        windows = self._lock_windows(final_slots)
        conflicts = self.store.find_lock_conflicts(
            windows, exclude_plan_id=head.plan_id)
        if conflicts:
            clash = conflicts[0]
            raise ConflictError(
                f"情况已变化：资源 {clash.resource} 已被计划 {clash.plan_id} 锁定，"
                "请基于当前计划重建替代方案")

        chain = self.store.list_plan_chain(head.origin_plan_id)
        next_version = max((p.version for p in chain), default=0) + 1
        new_plan = Plan(
            plan_id=_new_id("plan"),
            stage_ids=tuple(sorted({s.stage_id for s in final_slots})),
            version=next_version,
            state=PLAN_PUBLISHED, label=head.label,
            slots=final_slots, created_by=contingency.created_by,
            created_at=self._now(), published_at=self._now(),
            supersedes=head.plan_id, origin_plan_id=head.origin_plan_id,
        )
        self.store.insert_plan(new_plan)
        for slot in new_plan.slots:
            pv = self.store.get_program(slot.program_id, slot.program_version)
            if pv is not None and not pv.published:
                self.store.mark_program_published(pv.program_id, pv.version)
        for resource, starts_at, ends_at in windows:
            self.store.insert_lock(ResourceLock(
                plan_id=new_plan.plan_id, resource=resource,
                starts_at=starts_at, ends_at=ends_at))
        self.store.delete_locks(head.plan_id)
        self.store.update_plan_state(head.plan_id, PLAN_SUPERSEDED)
        self.store.update_contingency(contingency.contingency_id,
                                      state=CONTINGENCY_APPROVED,
                                      new_plan_id=new_plan.plan_id)
        self.store.append_event(self._now(), approver_id,
                                "contingency_approved",
                                {"contingency_id": contingency.contingency_id,
                                 "superseded_plan_id": head.plan_id,
                                 "new_plan_id": new_plan.plan_id,
                                 "final_comment": comment})
        return self.get_contingency(contingency.contingency_id)

    def withdraw_contingency(self, contingency_id: str,
                             actor_id: str) -> dict[str, Any]:
        """申请人在终审完成前撤回替代方案；原计划保持有效。"""
        user = self._require_user(actor_id)
        contingency = self._require_contingency(contingency_id)
        if contingency.state != CONTINGENCY_PROPOSED:
            raise InvalidTransitionError(
                f"替代方案当前状态为 {contingency.state}，不能撤回")
        if contingency.created_by != user.user_id and not user.has_role(ROLE_ADMIN):
            raise PermissionDeniedError("只有创建人可以撤回替代方案")
        with self.store.transaction():
            self.store.update_contingency(contingency_id,
                                          state=CONTINGENCY_WITHDRAWN)
            self.store.append_event(self._now(), actor_id,
                                    "contingency_withdrawn",
                                    {"contingency_id": contingency_id})
        return self.get_contingency(contingency_id)

    def _require_contingency(self, contingency_id: str) -> Contingency:
        contingency = self.store.get_contingency(contingency_id)
        if contingency is None:
            raise NotFoundError(f"替代方案不存在: {contingency_id}")
        return contingency

    def _chain_head(self, plan: Plan) -> Plan:
        """返回计划所在替代链上当前最新的已发布计划。"""
        chain = self.store.list_plan_chain(plan.origin_plan_id or plan.plan_id)
        published = [p for p in chain if p.state == PLAN_PUBLISHED]
        return published[-1] if published else plan

    # ------------------------------------------------------------ 重开 / 恢复

    def reopen_stage(self, stage_id: str, actor_id: str) -> dict[str, Any]:
        stage = self.store.get_stage(stage_id)
        if stage is None:
            raise NotFoundError(f"舞台未登记: {stage_id}")
        open_incidents = self.store.list_open_stage_incidents(stage_id)
        withdrawn_ids: list[str] = []
        with self.store.transaction():
            self.store.set_stage_closed(stage_id, False)
            for incident in open_incidents:
                self.store.resolve_incident(incident.incident_id, self._now())
                # 场地恢复后，针对本次封闭的待审批方案失去意义，统一撤回留痕。
                for contingency in self.store.list_contingencies(incident.incident_id):
                    if contingency.state == CONTINGENCY_PROPOSED:
                        self.store.update_contingency(
                            contingency.contingency_id,
                            state=CONTINGENCY_WITHDRAWN)
                        withdrawn_ids.append(contingency.contingency_id)
            self.store.append_event(self._now(), actor_id, "stage_reopened", {
                "stage_id": stage_id,
                "resolved_incident_ids": [i.incident_id for i in open_incidents],
                "withdrawn_contingency_ids": withdrawn_ids})
        resumed = self.latest_effective_plan()
        return {"stage_id": stage_id, "closed": False,
                "resolved_incident_ids": [i.incident_id for i in open_incidents],
                "withdrawn_contingency_ids": withdrawn_ids,
                "resumed_plan_id": resumed["plan_id"] if resumed else "",
                "reopened_at": self._now()}

    def latest_effective_plan(self, origin_plan_id: str | None = None) -> dict[str, Any] | None:
        """场地重开后，从最近的有效版本继续。

        沿替代链从新到旧，返回第一个当前可执行的已发布计划：
        其所有舞台均已开放，且没有未解决事件与其条目冲突。
        """
        if origin_plan_id is None:
            plan = self.store.latest_published_plan()
            if plan is None:
                return None
            origin_plan_id = plan.origin_plan_id or plan.plan_id
        chain = self.store.list_plan_chain(origin_plan_id)
        open_incidents = self.store.list_incidents(include_resolved=False)
        for plan in reversed(chain):
            if plan.state == PLAN_PUBLISHED and self._plan_is_executable(plan, open_incidents):
                return self._plan_dict(plan)
        # 该链上没有可执行版本时，回退到全局最近的可执行已发布计划。
        for plan in reversed(self.store.list_plans()):
            if (plan.state == PLAN_PUBLISHED
                    and (plan.origin_plan_id or plan.plan_id) != origin_plan_id
                    and self._plan_is_executable(plan, open_incidents)):
                return self._plan_dict(plan)
        return None

    @staticmethod
    def _plan_is_executable(plan: Plan, open_incidents: Sequence[Incident]) -> bool:
        far_future = datetime(9999, 1, 1, tzinfo=timezone.utc)
        for slot in plan.slots:
            for incident in open_incidents:
                if incident.kind == "stage_closed" and incident.ref_id == slot.stage_id:
                    window_end = (parse_time(incident.ends_at)
                                  if incident.ends_at else far_future)
                    if slot.start < window_end and slot.buffer_end > parse_time(incident.starts_at):
                        return False
                if (incident.kind == "artist_withdrew"
                        and incident.ref_id in slot.artist_ids
                        and slot.start >= parse_time(incident.starts_at)):
                    return False
        return True

    # ------------------------------------------------------------ 查询

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self._plan_dict(self._require_plan(plan_id))

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        incident = self.store.get_incident(incident_id)
        if incident is None:
            raise NotFoundError(f"事件不存在: {incident_id}")
        return self._incident_dict(incident)

    def get_contingency(self, contingency_id: str) -> dict[str, Any]:
        return self._contingency_dict(self._require_contingency(contingency_id))

    def list_audit_events(self) -> list[dict[str, Any]]:
        return self.store.list_events()

    # ------------------------------------------------------------ 序列化

    @staticmethod
    def _user_dict(user: User) -> dict[str, Any]:
        return {"user_id": user.user_id, "name": user.name,
                "roles": list(user.roles)}

    @staticmethod
    def _artist_dict(artist: Artist) -> dict[str, Any]:
        return {"artist_id": artist.artist_id, "name": artist.name,
                "genre": artist.genre}

    @staticmethod
    def _stage_dict(stage: Stage) -> dict[str, Any]:
        return {"stage_id": stage.stage_id, "name": stage.name,
                "location": stage.location, "closed": stage.closed}

    @staticmethod
    def _program_dict(pv: ProgramVersion) -> dict[str, Any]:
        return {"program_id": pv.program_id, "version": pv.version,
                "title": pv.title, "artist_ids": list(pv.artist_ids),
                "duration_minutes": pv.duration_minutes,
                "category": pv.category, "published": pv.published,
                "ref": pv.ref, "created_by": pv.created_by,
                "created_at": pv.created_at}

    def _plan_dict(self, plan: Plan) -> dict[str, Any]:
        return {
            "plan_id": plan.plan_id, "label": plan.label, "version": plan.version,
            "state": plan.state, "stage_ids": list(plan.stage_ids),
            "slots": [self._slot_dict(s) for s in plan.ordered_slots()],
            "created_by": plan.created_by, "created_at": plan.created_at,
            "published_at": plan.published_at, "supersedes": plan.supersedes,
            "origin_plan_id": plan.origin_plan_id,
            "spans_midnight": plan.spans_midnight(),
        }

    @staticmethod
    def _slot_dict(slot: Slot) -> dict[str, Any]:
        return {"item_id": slot.item_id, "program_id": slot.program_id,
                "program_version": slot.program_version, "title": slot.title,
                "artist_ids": list(slot.artist_ids), "stage_id": slot.stage_id,
                "starts_at": slot.starts_at, "ends_at": slot.ends_at,
                "buffer_minutes": slot.buffer_minutes,
                "is_replacement": slot.is_replacement,
                "buffer_end": format_time(slot.buffer_end)}

    @staticmethod
    def _incident_dict(incident: Incident) -> dict[str, Any]:
        return {
            "incident_id": incident.incident_id, "kind": incident.kind,
            "ref_id": incident.ref_id, "plan_id": incident.plan_id,
            "starts_at": incident.starts_at, "ends_at": incident.ends_at,
            "opened_by": incident.opened_by, "opened_at": incident.opened_at,
            "resolved": incident.resolved,
            "impacted_items": [
                {"plan_id": i.plan_id, "item_id": i.item_id, "reason": i.reason}
                for i in incident.impacted],
        }

    @staticmethod
    def _contingency_dict(contingency: Contingency) -> dict[str, Any]:
        return {
            "contingency_id": contingency.contingency_id,
            "incident_id": contingency.incident_id,
            "plan_id": contingency.plan_id,
            "created_by": contingency.created_by,
            "created_at": contingency.created_at,
            "required_levels": contingency.required_levels,
            "state": contingency.state,
            "new_plan_id": contingency.new_plan_id,
            "note": contingency.note,
            "current_level": contingency.current_level()
            if contingency.state == CONTINGENCY_PROPOSED else None,
            "slots": [Service._slot_dict(s) for s in contingency.slots],
            "approval_steps": [
                {"level": s.level, "approver_id": s.approver_id,
                 "status": s.status, "comment": s.comment,
                 "decided_at": s.decided_at}
                for s in contingency.steps],
        }

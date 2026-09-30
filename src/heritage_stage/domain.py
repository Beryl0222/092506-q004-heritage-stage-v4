"""演出排期领域对象、时间处理与业务异常。

时间一律使用带时区的 ``datetime``，存储与传输时统一为 ISO-8601 字符串。
``Clock`` 抽象让测试可以用 :class:`FakeClock` 精确驱动跨日演出与审批时序。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any


# --------------------------------------------------------------------------- 时间

class Clock:
    """真实时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """测试用模拟时钟：可手动推进、可跳转到指定时刻。"""

    def __init__(self, start: datetime | str | None = None) -> None:
        if start is None:
            current = datetime.now(timezone.utc)
        elif isinstance(start, str):
            current = parse_time(start)
        else:
            current = start
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        self._now = current.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: Any) -> datetime:
        """按 ``timedelta`` 参数推进，例如 ``advance(days=1, minutes=30)``。"""
        self._now += timedelta(**kwargs)
        return self._now

    def jump_to(self, target: datetime | str) -> datetime:
        if isinstance(target, str):
            target = parse_time(target)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        self._now = target.astimezone(timezone.utc)
        return self._now


def parse_time(value: datetime | str) -> datetime:
    """解析 ISO-8601 字符串，裸时间按 UTC 处理。"""
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------- 异常

class SchedulingError(Exception):
    """业务规则冲突的基类（资源锁定、冻结计划等）。"""

    code = "scheduling_error"


class NotFoundError(SchedulingError):
    code = "not_found"


class DuplicateError(SchedulingError):
    code = "duplicate"


class ConflictError(SchedulingError):
    """资源已被锁定或计划处于不可变更状态。"""

    code = "conflict"


class PermissionDeniedError(SchedulingError):
    code = "permission_denied"


class ValidationError(SchedulingError):
    code = "validation_error"


class InvalidTransitionError(SchedulingError):
    code = "invalid_transition"


# --------------------------------------------------------------------------- 兼容基线的通用记录

@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str = "draft"
    created_at: str = ""

    def with_timestamp(self) -> "Record":
        return Record(self.record_id, self.owner_id, self.state,
                      self.created_at or datetime.now(timezone.utc).isoformat())


# --------------------------------------------------------------------------- 人员与资源

ROLE_COORDINATOR = "coordinator"   # 统筹者：可创建替代方案
ROLE_APPROVER = "approver"         # 审批人：逐级审批
ROLE_ADMIN = "admin"               # 管理员：兼具备份与最高审批权
ROLE_VIEWER = "viewer"


@dataclass(frozen=True)
class User:
    user_id: str
    name: str = ""
    roles: tuple[str, ...] = (ROLE_VIEWER,)

    def has_role(self, role: str) -> bool:
        return role in self.roles or ROLE_ADMIN in self.roles


@dataclass(frozen=True)
class Artist:
    artist_id: str
    name: str
    genre: str = ""

    @property
    def lock_key(self) -> str:
        return f"artist:{self.artist_id}"


@dataclass(frozen=True)
class Stage:
    stage_id: str
    name: str
    location: str = ""
    closed: bool = False

    @property
    def lock_key(self) -> str:
        return f"stage:{self.stage_id}"


@dataclass(frozen=True)
class ProgramVersion:
    """节目版本。``version`` 递增；发布后的版本即为冻结快照。"""

    program_id: str
    version: int
    title: str
    artist_ids: tuple[str, ...]
    duration_minutes: int
    category: str = ""
    published: bool = False
    created_by: str = ""
    created_at: str = ""

    @property
    def ref(self) -> str:
        return f"{self.program_id}@v{self.version}"

    def edited_copy(self, **changes: Any) -> "ProgramVersion":
        return replace(self, **changes)


# --------------------------------------------------------------------------- 排期条目与计划

@dataclass(frozen=True)
class Slot:
    """单个节目在计划中的排期片段（含缓冲）。"""

    item_id: str
    program_id: str
    program_version: int
    title: str
    artist_ids: tuple[str, ...]
    stage_id: str
    starts_at: str
    ends_at: str
    buffer_minutes: int
    is_replacement: bool = False

    @property
    def start(self) -> datetime:
        return parse_time(self.starts_at)

    @property
    def end(self) -> datetime:
        return parse_time(self.ends_at)

    @property
    def buffer_end(self) -> datetime:
        """缓冲结束时刻：下一个节目最早可以开始的时刻。"""
        return self.end + timedelta(minutes=self.buffer_minutes)

    def overlaps(self, other: "Slot") -> bool:
        return self.start < other.buffer_end and other.start < self.buffer_end

    def resources(self) -> set[str]:
        keys = {f"stage:{self.stage_id}"}
        keys.update(f"artist:{a}" for a in self.artist_ids)
        return keys


# 计划生命周期
PLAN_DRAFT = "draft"            # 草稿：可反复生成
PLAN_PUBLISHED = "published"    # 已发布（冻结）：资源被锁定
PLAN_SUPERSEDED = "superseded"  # 被新版本取代
PLAN_CANCELED = "canceled"      # 因场地封闭等整体取消

# 替代方案生命周期
CONTINGENCY_PROPOSED = "proposed"
CONTINGENCY_APPROVED = "approved"
CONTINGENCY_REJECTED = "rejected"
CONTINGENCY_WITHDRAWN = "withdrawn"

# 场地事件类型
INCIDENT_STAGE_CLOSED = "stage_closed"
INCIDENT_ARTIST_WITHDREW = "artist_withdrew"
INCIDENT_STAGE_REOPENED = "stage_reopened"

# 审批动作
APPROVAL_APPROVE = "approve"
APPROVAL_REJECT = "reject"
APPROVAL_WITHDRAW = "withdraw"   # 申请人撤回替代方案
APPROVAL_PENDING = "pending"
APPROVAL_DONE = "approved"
APPROVAL_DENIED = "rejected"


@dataclass
class Plan:
    """一次生成的整场演出计划。"""

    plan_id: str
    stage_ids: tuple[str, ...]
    version: int
    state: str = PLAN_DRAFT
    label: str = ""
    slots: list[Slot] = field(default_factory=list)
    created_by: str = ""
    created_at: str = ""
    published_at: str = ""
    supersedes: str = ""           # 本计划取代的上一版 plan_id
    origin_plan_id: str = ""       # 替代链追溯到的最初计划

    @property
    def frozen(self) -> bool:
        return self.state == PLAN_PUBLISHED

    def ordered_slots(self) -> list[Slot]:
        return sorted(self.slots, key=lambda s: (s.stage_id, s.starts_at))

    def spans_midnight(self) -> bool:
        """是否存在跨日（UTC 日期变化）的条目。"""
        return any(s.start.date() != s.end.date() for s in self.slots)


@dataclass(frozen=True)
class ResourceLock:
    """发布成功后对舞台/艺人时间窗持有的锁。"""

    plan_id: str
    resource: str
    starts_at: str
    ends_at: str


# --------------------------------------------------------------------------- 场地事件与替代方案

@dataclass(frozen=True)
class ImpactedItem:
    """受事件影响的计划条目。"""

    plan_id: str
    item_id: str
    reason: str


@dataclass(frozen=True)
class ApprovalStep:
    """逐级审批中的一级结果。"""

    level: int
    approver_id: str
    status: str = APPROVAL_PENDING
    comment: str = ""
    decided_at: str = ""


@dataclass
class Incident:
    """场地封闭 / 艺人临时退出 / 重新开放。"""

    incident_id: str
    kind: str
    ref_id: str                  # stage_id 或 artist_id
    plan_id: str                 # 针对哪一版已发布计划
    starts_at: str
    ends_at: str = ""            # 封闭持续到何时（空表示未确定）
    opened_by: str = ""
    opened_at: str = ""
    resolved: bool = False
    impacted: list[ImpactedItem] = field(default_factory=list)


@dataclass
class Contingency:
    """有权限的统筹者针对事件创建的替代方案。"""

    contingency_id: str
    incident_id: str
    plan_id: str                 # 原计划（被保留，不修改）
    created_by: str
    created_at: str
    required_levels: int
    state: str = CONTINGENCY_PROPOSED
    slots: list[Slot] = field(default_factory=list)
    steps: list[ApprovalStep] = field(default_factory=list)
    new_plan_id: str = ""        # 审批通过后生成的新计划
    note: str = ""

    def current_level(self) -> int:
        """下一个待审批级别（从 1 开始）。"""
        decided = sum(1 for s in self.steps if s.status != APPROVAL_PENDING)
        return decided + 1

    def is_fully_approved(self) -> bool:
        decided = [s for s in self.steps if s.status != APPROVAL_PENDING]
        return (
            len(decided) >= self.required_levels
            and all(s.status == APPROVAL_DONE for s in decided)
        )

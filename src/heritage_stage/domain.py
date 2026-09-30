"""演出计划与舞台资源的基础领域对象与纯规则。

规则不依赖数据库与系统时钟，所有时间均通过 :class:`Clock` 取得，
便于测试用模拟时钟推进跨日场景。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

# ---------------------------------------------------------------------------
# 时钟
# ---------------------------------------------------------------------------


class Clock:
    """可被测试替换的时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock(Clock):
    """模拟时钟：测试显式推进时间。"""

    def __init__(self, start: datetime | str) -> None:
        if isinstance(start, str):
            start = parse_dt(start)
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> datetime:
        self.current += timedelta(**kwargs)
        return self.current

    def set(self, value: datetime | str) -> datetime:
        self.current = parse_dt(value) if isinstance(value, str) else value
        return self.current


def parse_dt(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 领域对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str = "draft"
    created_at: str = ""

    def with_timestamp(self, now: str = "") -> "Record":
        return Record(self.record_id, self.owner_id, self.state,
                      self.created_at or now or datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class Artist:
    artist_id: str
    name: str


@dataclass(frozen=True)
class Stage:
    stage_id: str
    name: str
    closed: bool = False
    closed_since: str = ""
    closed_reason: str = ""


@dataclass(frozen=True)
class ProgramVersion:
    """节目版本（如《牡丹亭》v3）。版本号一旦建立即不可变。"""

    program_id: str
    version: int
    title: str
    genre: str
    duration_minutes: int
    artist_ids: tuple[str, ...] = field(default_factory=tuple)
    frozen: bool = False
    published_at: str = ""


@dataclass(frozen=True)
class PlanItem:
    """单条排期：节目版本在某舞台上的一段占用（含前后缓冲）。"""

    item_id: str
    stage_id: str
    program_id: str
    version: int
    start: str
    end: str
    buffer_before: int = 0
    buffer_after: int = 0

    @property
    def window_start(self) -> datetime:
        return parse_dt(self.start) - timedelta(minutes=self.buffer_before)

    @property
    def window_end(self) -> datetime:
        return parse_dt(self.end) + timedelta(minutes=self.buffer_after)


@dataclass(frozen=True)
class PlanVersion:
    """演出计划版本。

    state: draft（草稿）/ published（已发布）/ frozen（节目单已公开冻结）/
           superseded（被新版本替代）/ void（因场地封闭或艺人退出失效）
    """

    plan_id: str
    version: int
    stage_id: str
    items: tuple[PlanItem, ...] = field(default_factory=tuple)
    state: str = "draft"
    note: str = ""
    supersedes: int = 0
    contingency_for: str = ""
    created_at: str = ""
    published_at: str = ""
    frozen_at: str = ""


@dataclass(frozen=True)
class ResourceLock:
    """发布时对资源（舞台或艺人）在占用窗口上的锁定。"""

    lock_id: str
    resource_type: str  # stage / artist
    resource_id: str
    plan_id: str
    plan_version: int
    item_id: str
    window_start: str
    window_end: str


@dataclass(frozen=True)
class DisruptionEvent:
    """场地封闭或艺人临时退出事件。"""

    event_id: str
    kind: str  # stage_closed / artist_withdrawn
    resource_type: str
    resource_id: str
    occurred_at: str
    reason: str = ""
    affected_plan_id: str = ""
    affected_version: int = 0
    handled: bool = False
    resolution: str = ""  # contingency / reopened
    resolved_at: str = ""


@dataclass(frozen=True)
class Approval:
    """逐级审批记录。

    state: pending / approved / rejected / withdrawn
    """

    approval_id: str
    scope_type: str  # freeze / contingency
    scope_id: str
    level: int
    required_role: str
    state: str = "pending"
    decided_by: str = ""
    decided_at: str = ""
    comment: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class Conflict:
    resource_type: str
    resource_id: str
    other_plan_id: str
    other_version: int
    other_item_id: str
    window_start: str
    window_end: str

    def as_dict(self) -> dict:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "other_plan_id": self.other_plan_id,
            "other_version": self.other_version,
            "other_item_id": self.other_item_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
        }


# ---------------------------------------------------------------------------
# 纯规则
# ---------------------------------------------------------------------------


def overlaps(start_a: datetime, end_a: datetime,
             start_b: datetime, end_b: datetime) -> bool:
    """半开区间 [start, end) 是否重叠。缓冲相接不算冲突。"""
    return start_a < end_b and start_b < end_a


def build_items(schedule: Iterable[dict],
                buffer_before: int = 15,
                buffer_after: int = 15) -> tuple[PlanItem, ...]:
    """按顺序生成带缓冲的排期条目。

    schedule 每项形如::

        {"item_id", "stage_id", "program_id", "version", "start",
         "duration_minutes", ("buffer_before", "buffer_after")}

    条目顺序即整场顺序，相邻节目共享的缓冲不会压缩演出时长，
    仅用于冲突检测（见 :func:`item_conflicts`）。
    """
    items: list[PlanItem] = []
    for index, entry in enumerate(schedule):
        start = parse_dt(entry["start"])
        duration = int(entry["duration_minutes"])
        end = start + timedelta(minutes=duration)
        items.append(PlanItem(
            item_id=str(entry.get("item_id") or f"item-{index + 1}"),
            stage_id=str(entry["stage_id"]),
            program_id=str(entry["program_id"]),
            version=int(entry["version"]),
            start=fmt_dt(start),
            end=fmt_dt(end),
            buffer_before=int(entry.get("buffer_before", buffer_before)),
            buffer_after=int(entry.get("buffer_after", buffer_after)),
        ))
    return tuple(items)


def item_conflicts(target: PlanItem,
                   other: PlanItem,
                   target_artists: Iterable[str],
                   other_artists: Iterable[str]) -> list[str]:
    """返回两个条目冲突的资源键列表（舞台 + 共同艺人），空则不冲突。"""
    conflicts: list[str] = []
    if target.stage_id == other.stage_id and overlaps(
            target.window_start, target.window_end,
            other.window_start, other.window_end):
        conflicts.append(f"stage:{target.stage_id}")
    shared = set(target_artists) & set(other_artists)
    if shared and overlaps(target.window_start, target.window_end,
                           other.window_start, other.window_end):
        conflicts.extend(f"artist:{artist_id}" for artist_id in sorted(shared))
    return conflicts


def cross_day_boundaries(items: Iterable[PlanItem]) -> list[str]:
    """条目跨越的 UTC 日期边界（用于跨日演出核对）。"""
    boundaries: list[str] = []
    for item in items:
        start_day = parse_dt(item.start).date()
        end_day = parse_dt(item.end).date()
        if start_day != end_day:
            boundaries.append(item.item_id)
    return boundaries


def item_artists(item: PlanItem, programs: dict[tuple[str, int], ProgramVersion]
                 ) -> tuple[str, ...]:
    program = programs.get((item.program_id, item.version))
    return program.artist_ids if program else ()

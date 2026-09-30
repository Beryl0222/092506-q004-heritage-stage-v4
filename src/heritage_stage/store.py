"""演出计划与舞台资源的本地持久化边界。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from .domain import (
    Approval,
    Artist,
    DisruptionEvent,
    PlanItem,
    PlanVersion,
    ProgramVersion,
    Record,
    ResourceLock,
    Stage,
)

ACTIVE_STATES = ("published", "frozen")


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_tables()
        self.connection.commit()

    def _create_tables(self) -> None:
        c = self.connection
        c.execute("""
            CREATE TABLE IF NOT EXISTS performance (
                record_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS artist (
                artist_id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS stage (
                stage_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0,
                closed_since TEXT NOT NULL DEFAULT '',
                closed_reason TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS program_version (
                program_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                title TEXT NOT NULL,
                genre TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                artist_ids TEXT NOT NULL,
                frozen INTEGER NOT NULL DEFAULT 0,
                published_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (program_id, version)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS plan (
                plan_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                stage_id TEXT NOT NULL,
                state TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                supersedes INTEGER NOT NULL DEFAULT 0,
                contingency_for TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                frozen_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (plan_id, version)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS plan_item (
                plan_id TEXT NOT NULL,
                plan_version INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                stage_id TEXT NOT NULL,
                program_id TEXT NOT NULL,
                program_version INTEGER NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL,
                buffer_before INTEGER NOT NULL,
                buffer_after INTEGER NOT NULL,
                PRIMARY KEY (plan_id, plan_version, item_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS resource_lock (
                lock_id TEXT PRIMARY KEY,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                plan_version INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_lock_resource
            ON resource_lock(resource_type, resource_id, window_start, window_end)
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS disruption_event (
                event_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                affected_plan_id TEXT NOT NULL DEFAULT '',
                affected_version INTEGER NOT NULL DEFAULT 0,
                handled INTEGER NOT NULL DEFAULT 0,
                resolution TEXT NOT NULL DEFAULT '',
                resolved_at TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS approval (
                approval_id TEXT PRIMARY KEY,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                level INTEGER NOT NULL,
                required_role TEXT NOT NULL,
                state TEXT NOT NULL,
                decided_by TEXT NOT NULL DEFAULT '',
                decided_at TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS role_assignment (
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, role, level)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS event_impact (
                event_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                plan_version INTEGER NOT NULL,
                stage_id TEXT NOT NULL,
                item_ids TEXT NOT NULL,
                PRIMARY KEY (event_id, plan_id, plan_version)
            )
        """)

    # -- 兼容基线 ---------------------------------------------------------

    def save(self, record: Record) -> Record:
        value = record.with_timestamp()
        self.connection.execute(
            "INSERT INTO performance(record_id, owner_id, state, created_at) VALUES(?,?,?,?)",
            (value.record_id, value.owner_id, value.state, value.created_at),
        )
        self.connection.commit()
        return value

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, created_at FROM performance WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    # -- 角色 -------------------------------------------------------------

    def grant_role(self, user_id: str, role: str, level: int = 0) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO role_assignment(user_id, role, level) VALUES(?,?,?)",
            (user_id, role, level),
        )
        self.connection.commit()

    def has_role(self, user_id: str, role: str, level: Optional[int] = None) -> bool:
        sql = "SELECT 1 FROM role_assignment WHERE user_id=? AND role=?"
        params: list = [user_id, role]
        if level is not None:
            sql += " AND level=?"
            params.append(level)
        return self.connection.execute(sql, params).fetchone() is not None

    # -- 艺人 / 舞台 ------------------------------------------------------

    def save_artist(self, artist: Artist) -> Artist:
        self.connection.execute(
            "INSERT INTO artist(artist_id, name) VALUES(?,?) "
            "ON CONFLICT(artist_id) DO UPDATE SET name=excluded.name",
            (artist.artist_id, artist.name),
        )
        self.connection.commit()
        return artist

    def get_artist(self, artist_id: str) -> Artist | None:
        row = self.connection.execute(
            "SELECT artist_id, name FROM artist WHERE artist_id=?", (artist_id,)
        ).fetchone()
        return Artist(**dict(row)) if row else None

    def list_artists(self) -> list[Artist]:
        rows = self.connection.execute(
            "SELECT artist_id, name FROM artist ORDER BY artist_id"
        ).fetchall()
        return [Artist(**dict(row)) for row in rows]

    def save_stage(self, stage: Stage) -> Stage:
        self.connection.execute(
            "INSERT INTO stage(stage_id, name, closed, closed_since, closed_reason) "
            "VALUES(?,?,?,?,?) ON CONFLICT(stage_id) DO UPDATE SET name=excluded.name",
            (stage.stage_id, stage.name, int(stage.closed),
             stage.closed_since, stage.closed_reason),
        )
        self.connection.commit()
        return stage

    def get_stage(self, stage_id: str) -> Stage | None:
        row = self.connection.execute(
            "SELECT stage_id, name, closed, closed_since, closed_reason "
            "FROM stage WHERE stage_id=?", (stage_id,)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["closed"] = bool(data["closed"])
        return Stage(**data)

    def list_stages(self) -> list[Stage]:
        rows = self.connection.execute(
            "SELECT stage_id, name, closed, closed_since, closed_reason "
            "FROM stage ORDER BY stage_id"
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["closed"] = bool(data["closed"])
            result.append(Stage(**data))
        return result

    def set_stage_closed(self, stage_id: str, closed: bool,
                         since: str, reason: str) -> None:
        self.connection.execute(
            "UPDATE stage SET closed=?, closed_since=?, closed_reason=? WHERE stage_id=?",
            (int(closed), since if closed else "", reason if closed else "", stage_id),
        )
        self.connection.commit()

    # -- 节目版本 ---------------------------------------------------------

    def save_program_version(self, program: ProgramVersion) -> ProgramVersion:
        self.connection.execute(
            "INSERT INTO program_version(program_id, version, title, genre, "
            "duration_minutes, artist_ids, frozen, published_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (program.program_id, program.version, program.title, program.genre,
             program.duration_minutes, json.dumps(list(program.artist_ids)),
             int(program.frozen), program.published_at),
        )
        self.connection.commit()
        return program

    def get_program_version(self, program_id: str, version: int) -> ProgramVersion | None:
        row = self.connection.execute(
            "SELECT * FROM program_version WHERE program_id=? AND version=?",
            (program_id, version),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["artist_ids"] = tuple(json.loads(data.pop("artist_ids")))
        data["frozen"] = bool(data["frozen"])
        return ProgramVersion(**data)

    def set_program_frozen(self, program_id: str, version: int, published_at: str) -> None:
        self.connection.execute(
            "UPDATE program_version SET frozen=1, published_at=? "
            "WHERE program_id=? AND version=?",
            (published_at, program_id, version),
        )
        self.connection.commit()

    # -- 计划 -------------------------------------------------------------

    def insert_plan(self, plan: PlanVersion, items: Iterable[PlanItem]) -> None:
        c = self.connection
        c.execute(
            "INSERT INTO plan(plan_id, version, stage_id, state, note, supersedes, "
            "contingency_for, created_at, published_at, frozen_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (plan.plan_id, plan.version, plan.stage_id, plan.state, plan.note,
             plan.supersedes, plan.contingency_for, plan.created_at,
             plan.published_at, plan.frozen_at),
        )
        for position, item in enumerate(items):
            c.execute(
                "INSERT INTO plan_item(plan_id, plan_version, item_id, position, "
                "stage_id, program_id, program_version, start, end, "
                "buffer_before, buffer_after) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (plan.plan_id, plan.version, item.item_id, position, item.stage_id,
                 item.program_id, item.version, item.start, item.end,
                 item.buffer_before, item.buffer_after),
            )
        c.commit()

    def _load_items(self, plan_id: str, version: int) -> list[PlanItem]:
        rows = self.connection.execute(
            "SELECT item_id, stage_id, program_id, program_version, start, end, "
            "buffer_before, buffer_after FROM plan_item "
            "WHERE plan_id=? AND plan_version=? ORDER BY position",
            (plan_id, version),
        ).fetchall()
        items = []
        for row in rows:
            data = dict(row)
            data["version"] = data.pop("program_version")
            items.append(PlanItem(**data))
        return items

    def get_plan(self, plan_id: str, version: Optional[int] = None) -> PlanVersion | None:
        if version is None:
            row = self.connection.execute(
                "SELECT * FROM plan WHERE plan_id=? ORDER BY version DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM plan WHERE plan_id=? AND version=?",
                (plan_id, version),
            ).fetchone()
        if not row:
            return None
        plan = PlanVersion(**dict(row))
        return PlanVersion(
            plan_id=plan.plan_id, version=plan.version, stage_id=plan.stage_id,
            items=tuple(self._load_items(plan.plan_id, plan.version)),
            state=plan.state, note=plan.note, supersedes=plan.supersedes,
            contingency_for=plan.contingency_for, created_at=plan.created_at,
            published_at=plan.published_at, frozen_at=plan.frozen_at,
        )

    def list_plan_versions(self, plan_id: str) -> list[PlanVersion]:
        rows = self.connection.execute(
            "SELECT plan_id, version FROM plan WHERE plan_id=? ORDER BY version",
            (plan_id,),
        ).fetchall()
        return [p for p in (self.get_plan(row["plan_id"], row["version"])
                            for row in rows) if p]

    def latest_version_number(self, plan_id: str) -> int:
        row = self.connection.execute(
            "SELECT MAX(version) AS v FROM plan WHERE plan_id=?", (plan_id,)
        ).fetchone()
        return int(row["v"] or 0)

    def update_plan_state(self, plan_id: str, version: int, state: str,
                          published_at: str = "", frozen_at: str = "") -> None:
        fields = ["state=?"]
        params: list = [state]
        if published_at:
            fields.append("published_at=?")
            params.append(published_at)
        if frozen_at:
            fields.append("frozen_at=?")
            params.append(frozen_at)
        params.extend([plan_id, version])
        self.connection.execute(
            f"UPDATE plan SET {', '.join(fields)} WHERE plan_id=? AND version=?",
            params,
        )
        self.connection.commit()

    # -- 资源锁 -----------------------------------------------------------

    def add_locks(self, locks: Iterable[ResourceLock]) -> None:
        self.connection.executemany(
            "INSERT INTO resource_lock(lock_id, resource_type, resource_id, "
            "plan_id, plan_version, item_id, window_start, window_end) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(l.lock_id, l.resource_type, l.resource_id, l.plan_id,
              l.plan_version, l.item_id, l.window_start, l.window_end)
             for l in locks],
        )
        self.connection.commit()

    def delete_locks_for_plan(self, plan_id: str, version: int) -> int:
        cursor = self.connection.execute(
            "DELETE FROM resource_lock WHERE plan_id=? AND plan_version=?",
            (plan_id, version),
        )
        self.connection.commit()
        return cursor.rowcount

    def find_active_locks(self, resource_type: str, resource_id: str,
                          window_start: str, window_end: str,
                          exclude_plan: tuple[str, int] | None = None
                          ) -> list[ResourceLock]:
        """与给定窗口（半开区间）重叠、且所属计划仍处于已发布/冻结态的锁。"""
        sql = (
            "SELECT l.* FROM resource_lock l "
            "JOIN plan p ON p.plan_id = l.plan_id AND p.version = l.plan_version "
            "WHERE l.resource_type=? AND l.resource_id=? "
            "AND l.window_start < ? AND l.window_end > ? "
            f"AND p.state IN ({','.join('?' for _ in ACTIVE_STATES)})"
        )
        params: list = [resource_type, resource_id, window_end, window_start,
                        *ACTIVE_STATES]
        if exclude_plan:
            sql += " AND NOT (l.plan_id=? AND l.plan_version=?)"
            params.extend(exclude_plan)
        rows = self.connection.execute(sql, params).fetchall()
        return [ResourceLock(**dict(row)) for row in rows]

    # -- 封闭 / 退出事件 --------------------------------------------------

    def insert_event(self, event: DisruptionEvent) -> None:
        self.connection.execute(
            "INSERT INTO disruption_event(event_id, kind, resource_type, resource_id, "
            "occurred_at, reason, affected_plan_id, affected_version, handled, "
            "resolution, resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (event.event_id, event.kind, event.resource_type, event.resource_id,
             event.occurred_at, event.reason, event.affected_plan_id,
             event.affected_version, int(event.handled), event.resolution,
             event.resolved_at),
        )
        self.connection.commit()

    def get_event(self, event_id: str) -> DisruptionEvent | None:
        row = self.connection.execute(
            "SELECT * FROM disruption_event WHERE event_id=?", (event_id,)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["handled"] = bool(data["handled"])
        return DisruptionEvent(**data)

    def list_events(self, handled: Optional[bool] = None) -> list[DisruptionEvent]:
        sql = "SELECT * FROM disruption_event"
        params: list = []
        if handled is not None:
            sql += " WHERE handled=?"
            params.append(int(handled))
        sql += " ORDER BY occurred_at, event_id"
        rows = self.connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["handled"] = bool(data["handled"])
            result.append(DisruptionEvent(**data))
        return result

    def update_event(self, event_id: str, handled: bool,
                     resolution: str, resolved_at: str) -> None:
        self.connection.execute(
            "UPDATE disruption_event SET handled=?, resolution=?, resolved_at=? "
            "WHERE event_id=?",
            (int(handled), resolution, resolved_at, event_id),
        )
        self.connection.commit()

    # -- 事件影响范围 -----------------------------------------------------

    def save_impacts(self, event_id: str, impacts: Iterable[dict]) -> None:
        self.connection.executemany(
            "INSERT INTO event_impact(event_id, plan_id, plan_version, stage_id, "
            "item_ids) VALUES(?,?,?,?,?)",
            [(event_id, i["plan_id"], i["plan_version"], i["stage_id"],
              json.dumps(i["item_ids"])) for i in impacts],
        )
        self.connection.commit()

    def get_impacts(self, event_id: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT plan_id, plan_version, stage_id, item_ids FROM event_impact "
            "WHERE event_id=?", (event_id,)
        ).fetchall()
        return [{"plan_id": r["plan_id"], "plan_version": r["plan_version"],
                 "stage_id": r["stage_id"], "item_ids": json.loads(r["item_ids"])}
                for r in rows]

    def active_plans_using_stage(self, stage_id: str) -> list[tuple[str, int]]:
        rows = self.connection.execute(
            "SELECT DISTINCT plan_id, version FROM plan WHERE stage_id=? "
            f"AND state IN ({','.join('?' for _ in ACTIVE_STATES)})",
            (stage_id, *ACTIVE_STATES),
        ).fetchall()
        return [(r["plan_id"], r["version"]) for r in rows]

    def active_plans_using_artist(self, artist_id: str) -> list[tuple[str, int]]:
        pattern = f'%"{artist_id}"%'
        rows = self.connection.execute(
            "SELECT DISTINCT i.plan_id AS plan_id, i.plan_version AS version "
            "FROM plan_item i JOIN plan p "
            "ON p.plan_id=i.plan_id AND p.version=i.plan_version "
            "JOIN program_version v ON v.program_id=i.program_id "
            "AND v.version=i.program_version "
            f"WHERE v.artist_ids LIKE ? AND p.state IN ({','.join('?' for _ in ACTIVE_STATES)})",
            (pattern, *ACTIVE_STATES),
        ).fetchall()
        return [(r["plan_id"], r["version"]) for r in rows]

    # -- 审批 -------------------------------------------------------------

    def insert_approval(self, approval: Approval) -> None:
        self.connection.execute(
            "INSERT INTO approval(approval_id, scope_type, scope_id, level, "
            "required_role, state, decided_by, decided_at, comment, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (approval.approval_id, approval.scope_type, approval.scope_id,
             approval.level, approval.required_role, approval.state,
             approval.decided_by, approval.decided_at, approval.comment,
             approval.created_at),
        )
        self.connection.commit()

    def get_approval(self, approval_id: str) -> Approval | None:
        row = self.connection.execute(
            "SELECT * FROM approval WHERE approval_id=?", (approval_id,)
        ).fetchone()
        return Approval(**dict(row)) if row else None

    def list_approvals(self, scope_type: str, scope_id: str) -> list[Approval]:
        rows = self.connection.execute(
            "SELECT * FROM approval WHERE scope_type=? AND scope_id=? ORDER BY level",
            (scope_type, scope_id),
        ).fetchall()
        return [Approval(**dict(row)) for row in rows]

    def update_approval(self, approval_id: str, state: str,
                        decided_by: str, decided_at: str, comment: str) -> None:
        self.connection.execute(
            "UPDATE approval SET state=?, decided_by=?, decided_at=?, comment=? "
            "WHERE approval_id=?",
            (state, decided_by, decided_at, comment, approval_id),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

"""SQLite 持久化边界。

保存人员、艺人、舞台、节目版本、计划与排期条目、发布后产生的资源锁、
场地/艺人事件、替代方案与逐级审批记录，以及不可变的审计事件流。
时间列统一存 UTC ISO-8601 文本（同偏移、定长，可直接做区间比较）。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .domain import (
    ApprovalStep,
    Artist,
    Contingency,
    ImpactedItem,
    Incident,
    Plan,
    ProgramVersion,
    Record,
    ResourceLock,
    Slot,
    Stage,
    User,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS performance (
    record_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    roles TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artists (
    artist_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    genre TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stages (
    stage_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    location TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS programs (
    program_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    artist_ids TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    category TEXT NOT NULL,
    published INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (program_id, version)
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    stage_ids TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    label TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT '',
    supersedes TEXT NOT NULL DEFAULT '',
    origin_plan_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS plan_items (
    item_id TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    position INTEGER NOT NULL,
    program_id TEXT NOT NULL,
    program_version INTEGER NOT NULL,
    title TEXT NOT NULL,
    artist_ids TEXT NOT NULL,
    stage_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    buffer_minutes INTEGER NOT NULL,
    is_replacement INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (plan_id, item_id)
);
CREATE TABLE IF NOT EXISTS resource_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    resource TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL DEFAULT '',
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS incident_impacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    plan_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contingencies (
    contingency_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    plan_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    required_levels INTEGER NOT NULL,
    state TEXT NOT NULL,
    new_plan_id TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS contingency_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contingency_id TEXT NOT NULL REFERENCES contingencies(contingency_id),
    position INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    program_id TEXT NOT NULL,
    program_version INTEGER NOT NULL,
    title TEXT NOT NULL,
    artist_ids TEXT NOT NULL,
    stage_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    buffer_minutes INTEGER NOT NULL,
    is_replacement INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS approval_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contingency_id TEXT NOT NULL REFERENCES contingencies(contingency_id),
    level INTEGER NOT NULL,
    approver_id TEXT NOT NULL,
    status TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL DEFAULT '',
    UNIQUE (contingency_id, level)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def _enc_ids(ids: tuple[str, ...] | list[str]) -> str:
    return json.dumps(list(ids), ensure_ascii=False)


def _dec_ids(value: str) -> tuple[str, ...]:
    return tuple(json.loads(value))


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._in_transaction = False
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    # ------------------------------------------------------------- 事务 / 审计

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """冲突检测与写入必须落在同一事务里，避免两个发布同时穿透。

        事务进行中，各写方法不再自动提交，统一由这里提交或回滚。
        """
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._in_transaction = False

    def _commit(self) -> None:
        """事务外的单步写入立即落库；事务内交由 ``transaction`` 统一提交。"""
        if not self._in_transaction:
            self.connection.commit()

    def append_event(self, at: str, actor_id: str, action: str, detail: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(at, actor_id, action, detail) VALUES(?,?,?,?)",
            (at, actor_id, action, json.dumps(detail, ensure_ascii=False)),
        )
        self._commit()

    def list_events(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT at, actor_id, action, detail FROM audit_events ORDER BY id"
        ).fetchall()
        return [
            {"at": r["at"], "actor_id": r["actor_id"], "action": r["action"],
             "detail": json.loads(r["detail"])}
            for r in rows
        ]

    # ------------------------------------------------------------- 基线记录

    def save(self, record: Record) -> Record:
        value = record.with_timestamp()
        self.connection.execute(
            "INSERT INTO performance(record_id, owner_id, state, created_at) VALUES(?,?,?,?)",
            (value.record_id, value.owner_id, value.state, value.created_at),
        )
        self._commit()
        return value

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, created_at FROM performance WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    # ------------------------------------------------------------- 人员

    def save_user(self, user: User, created_at: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO users(user_id, name, roles, created_at) VALUES(?,?,?,?)",
            (user.user_id, user.name, _enc_ids(user.roles), created_at),
        )
        self._commit()

    def get_user(self, user_id: str) -> User | None:
        row = self.connection.execute(
            "SELECT user_id, name, roles FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return User(row["user_id"], row["name"], _dec_ids(row["roles"]))

    # ------------------------------------------------------------- 艺人 / 舞台

    def save_artist(self, artist: Artist, created_at: str) -> Artist:
        self.connection.execute(
            "INSERT INTO artists(artist_id, name, genre, created_at) VALUES(?,?,?,?)",
            (artist.artist_id, artist.name, artist.genre, created_at),
        )
        self._commit()
        return artist

    def get_artist(self, artist_id: str) -> Artist | None:
        row = self.connection.execute(
            "SELECT artist_id, name, genre FROM artists WHERE artist_id=?", (artist_id,)
        ).fetchone()
        return Artist(row["artist_id"], row["name"], row["genre"]) if row else None

    def list_artists(self) -> list[Artist]:
        rows = self.connection.execute(
            "SELECT artist_id, name, genre FROM artists ORDER BY artist_id"
        ).fetchall()
        return [Artist(r["artist_id"], r["name"], r["genre"]) for r in rows]

    def save_stage(self, stage: Stage, created_at: str) -> Stage:
        self.connection.execute(
            "INSERT INTO stages(stage_id, name, location, closed, created_at) VALUES(?,?,?,?,?)",
            (stage.stage_id, stage.name, stage.location, 1 if stage.closed else 0, created_at),
        )
        self._commit()
        return stage

    def get_stage(self, stage_id: str) -> Stage | None:
        row = self.connection.execute(
            "SELECT stage_id, name, location, closed FROM stages WHERE stage_id=?", (stage_id,)
        ).fetchone()
        if not row:
            return None
        return Stage(row["stage_id"], row["name"], row["location"], bool(row["closed"]))

    def list_stages(self) -> list[Stage]:
        rows = self.connection.execute(
            "SELECT stage_id, name, location, closed FROM stages ORDER BY stage_id"
        ).fetchall()
        return [Stage(r["stage_id"], r["name"], r["location"], bool(r["closed"]))
                for r in rows]

    def set_stage_closed(self, stage_id: str, closed: bool) -> None:
        self.connection.execute(
            "UPDATE stages SET closed=? WHERE stage_id=?",
            (1 if closed else 0, stage_id),
        )
        self._commit()

    # ------------------------------------------------------------- 节目版本

    def insert_program(self, pv: ProgramVersion) -> None:
        self.connection.execute(
            """INSERT INTO programs(program_id, version, title, artist_ids,
                   duration_minutes, category, published, created_by, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (pv.program_id, pv.version, pv.title, _enc_ids(pv.artist_ids),
             pv.duration_minutes, pv.category, 1 if pv.published else 0,
             pv.created_by, pv.created_at),
        )
        self._commit()

    def mark_program_published(self, program_id: str, version: int) -> None:
        self.connection.execute(
            "UPDATE programs SET published=1 WHERE program_id=? AND version=?",
            (program_id, version),
        )
        self._commit()

    def get_program(self, program_id: str, version: int) -> ProgramVersion | None:
        row = self.connection.execute(
            """SELECT program_id, version, title, artist_ids, duration_minutes,
                      category, published, created_by, created_at
               FROM programs WHERE program_id=? AND version=?""",
            (program_id, version),
        ).fetchone()
        return self._program_from_row(row) if row else None

    def get_latest_program(self, program_id: str) -> ProgramVersion | None:
        row = self.connection.execute(
            """SELECT program_id, version, title, artist_ids, duration_minutes,
                      category, published, created_by, created_at
               FROM programs WHERE program_id=? ORDER BY version DESC LIMIT 1""",
            (program_id,),
        ).fetchone()
        return self._program_from_row(row) if row else None

    def list_program_versions(self, program_id: str) -> list[ProgramVersion]:
        rows = self.connection.execute(
            """SELECT program_id, version, title, artist_ids, duration_minutes,
                      category, published, created_by, created_at
               FROM programs WHERE program_id=? ORDER BY version""",
            (program_id,),
        ).fetchall()
        return [self._program_from_row(r) for r in rows]

    @staticmethod
    def _program_from_row(row: sqlite3.Row) -> ProgramVersion:
        return ProgramVersion(
            program_id=row["program_id"], version=row["version"], title=row["title"],
            artist_ids=_dec_ids(row["artist_ids"]),
            duration_minutes=row["duration_minutes"], category=row["category"],
            published=bool(row["published"]), created_by=row["created_by"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------- 计划

    def insert_plan(self, plan: Plan) -> None:
        self.connection.execute(
            """INSERT INTO plans(plan_id, stage_ids, version, state, label,
                   created_by, created_at, published_at, supersedes, origin_plan_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (plan.plan_id, _enc_ids(plan.stage_ids), plan.version, plan.state,
             plan.label, plan.created_by, plan.created_at, plan.published_at,
             plan.supersedes, plan.origin_plan_id or plan.plan_id),
        )
        for position, slot in enumerate(plan.slots):
            self._insert_item("plan_items", "plan_id", plan.plan_id, position, slot)
        self._commit()

    def _insert_item(self, table: str, parent_col: str, parent_id: str,
                     position: int, slot: Slot) -> None:
        self.connection.execute(
            f"""INSERT INTO {table}({parent_col}, position, item_id, program_id,
                   program_version, title, artist_ids, stage_id, starts_at,
                   ends_at, buffer_minutes, is_replacement)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (parent_id, position, slot.item_id, slot.program_id,
             slot.program_version, slot.title, _enc_ids(slot.artist_ids),
             slot.stage_id, slot.starts_at, slot.ends_at, slot.buffer_minutes,
             1 if slot.is_replacement else 0),
        )

    @staticmethod
    def _slot_from_row(row: sqlite3.Row) -> Slot:
        return Slot(
            item_id=row["item_id"], program_id=row["program_id"],
            program_version=row["program_version"], title=row["title"],
            artist_ids=_dec_ids(row["artist_ids"]), stage_id=row["stage_id"],
            starts_at=row["starts_at"], ends_at=row["ends_at"],
            buffer_minutes=row["buffer_minutes"],
            is_replacement=bool(row["is_replacement"]),
        )

    def _plan_from_row(self, row: sqlite3.Row) -> Plan:
        items = self.connection.execute(
            """SELECT item_id, program_id, program_version, title, artist_ids,
                      stage_id, starts_at, ends_at, buffer_minutes, is_replacement
               FROM plan_items WHERE plan_id=? ORDER BY position""",
            (row["plan_id"],),
        ).fetchall()
        return Plan(
            plan_id=row["plan_id"], stage_ids=_dec_ids(row["stage_ids"]),
            version=row["version"], state=row["state"], label=row["label"],
            slots=[self._slot_from_row(i) for i in items],
            created_by=row["created_by"], created_at=row["created_at"],
            published_at=row["published_at"], supersedes=row["supersedes"],
            origin_plan_id=row["origin_plan_id"],
        )

    def get_plan(self, plan_id: str) -> Plan | None:
        row = self.connection.execute(
            "SELECT * FROM plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        return self._plan_from_row(row) if row else None

    def list_plans(self) -> list[Plan]:
        rows = self.connection.execute("SELECT * FROM plans ORDER BY rowid").fetchall()
        return [self._plan_from_row(r) for r in rows]

    def list_plan_chain(self, origin_plan_id: str) -> list[Plan]:
        """同一替代链上的全部计划，按生成顺序。"""
        rows = self.connection.execute(
            "SELECT * FROM plans WHERE origin_plan_id=? ORDER BY rowid", (origin_plan_id,)
        ).fetchall()
        return [self._plan_from_row(r) for r in rows]

    def update_plan_state(self, plan_id: str, state: str, published_at: str = "") -> None:
        if published_at:
            self.connection.execute(
                "UPDATE plans SET state=?, published_at=? WHERE plan_id=?",
                (state, published_at, plan_id),
            )
        else:
            self.connection.execute(
                "UPDATE plans SET state=? WHERE plan_id=?", (state, plan_id)
            )
        self._commit()

    # ------------------------------------------------------------- 资源锁

    def insert_lock(self, lock: ResourceLock) -> None:
        self.connection.execute(
            "INSERT INTO resource_locks(plan_id, resource, starts_at, ends_at) VALUES(?,?,?,?)",
            (lock.plan_id, lock.resource, lock.starts_at, lock.ends_at),
        )
        self._commit()

    def find_lock_conflicts(self, windows: list[tuple[str, str, str]],
                            exclude_plan_id: str = "") -> list[ResourceLock]:
        """查询给定 (资源, 开始, 缓冲结束) 窗口与已发布计划锁的重叠。"""
        conflicts: list[ResourceLock] = []
        for resource, starts_at, ends_at in windows:
            row = self.connection.execute(
                """SELECT l.plan_id, l.resource, l.starts_at, l.ends_at
                   FROM resource_locks l JOIN plans p ON p.plan_id = l.plan_id
                   WHERE l.resource=? AND l.starts_at < ? AND l.ends_at > ?
                     AND p.state='published' AND l.plan_id <> ?
                   LIMIT 1""",
                (resource, ends_at, starts_at, exclude_plan_id),
            ).fetchone()
            if row:
                conflicts.append(ResourceLock(
                    plan_id=row["plan_id"], resource=row["resource"],
                    starts_at=row["starts_at"], ends_at=row["ends_at"]))
        return conflicts

    def count_locks(self, plan_id: str) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) AS n FROM resource_locks WHERE plan_id=?", (plan_id,)
        ).fetchone()["n"]

    def delete_locks(self, plan_id: str) -> None:
        """旧计划被替代方案取代时释放其持有的资源锁。"""
        self.connection.execute(
            "DELETE FROM resource_locks WHERE plan_id=?", (plan_id,)
        )
        self._commit()

    def latest_published_plan(self) -> Plan | None:
        row = self.connection.execute(
            "SELECT * FROM plans WHERE state='published' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return self._plan_from_row(row) if row else None

    # ------------------------------------------------------------- 事件与影响

    def insert_incident(self, incident: Incident) -> None:
        self.connection.execute(
            """INSERT INTO incidents(incident_id, kind, ref_id, plan_id, starts_at,
                   ends_at, opened_by, opened_at, resolved)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (incident.incident_id, incident.kind, incident.ref_id, incident.plan_id,
             incident.starts_at, incident.ends_at, incident.opened_by,
             incident.opened_at, 1 if incident.resolved else 0),
        )
        for impact in incident.impacted:
            self.connection.execute(
                """INSERT INTO incident_impacts(incident_id, plan_id, item_id, reason)
                   VALUES(?,?,?,?)""",
                (incident.incident_id, impact.plan_id, impact.item_id, impact.reason),
            )
        self._commit()

    def add_incident_impact(self, incident_id: str, impact: ImpactedItem) -> None:
        self.connection.execute(
            """INSERT INTO incident_impacts(incident_id, plan_id, item_id, reason)
               VALUES(?,?,?,?)""",
            (incident_id, impact.plan_id, impact.item_id, impact.reason),
        )
        self._commit()

    def resolve_incident(self, incident_id: str, ends_at: str) -> None:
        self.connection.execute(
            "UPDATE incidents SET resolved=1, ends_at=? WHERE incident_id=?",
            (ends_at, incident_id),
        )
        self._commit()

    def _incident_from_row(self, row: sqlite3.Row) -> Incident:
        impacts = self.connection.execute(
            "SELECT plan_id, item_id, reason FROM incident_impacts WHERE incident_id=?",
            (row["incident_id"],),
        ).fetchall()
        return Incident(
            incident_id=row["incident_id"], kind=row["kind"], ref_id=row["ref_id"],
            plan_id=row["plan_id"], starts_at=row["starts_at"], ends_at=row["ends_at"],
            opened_by=row["opened_by"], opened_at=row["opened_at"],
            resolved=bool(row["resolved"]),
            impacted=[ImpactedItem(i["plan_id"], i["item_id"], i["reason"]) for i in impacts],
        )

    def get_incident(self, incident_id: str) -> Incident | None:
        row = self.connection.execute(
            "SELECT * FROM incidents WHERE incident_id=?", (incident_id,)
        ).fetchone()
        return self._incident_from_row(row) if row else None

    def list_incidents(self, include_resolved: bool = True) -> list[Incident]:
        sql = "SELECT * FROM incidents"
        if not include_resolved:
            sql += " WHERE resolved=0"
        sql += " ORDER BY rowid"
        rows = self.connection.execute(sql).fetchall()
        return [self._incident_from_row(r) for r in rows]

    def list_open_stage_incidents(self, stage_id: str) -> list[Incident]:
        rows = self.connection.execute(
            """SELECT * FROM incidents WHERE kind='stage_closed' AND ref_id=?
                   AND resolved=0 ORDER BY rowid""",
            (stage_id,),
        ).fetchall()
        return [self._incident_from_row(r) for r in rows]

    # ------------------------------------------------------------- 替代方案

    def insert_contingency(self, contingency: Contingency) -> None:
        self.connection.execute(
            """INSERT INTO contingencies(contingency_id, incident_id, plan_id,
                   created_by, created_at, required_levels, state, new_plan_id, note)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (contingency.contingency_id, contingency.incident_id, contingency.plan_id,
             contingency.created_by, contingency.created_at,
             contingency.required_levels, contingency.state,
             contingency.new_plan_id, contingency.note),
        )
        for position, slot in enumerate(contingency.slots):
            self._insert_item("contingency_slots", "contingency_id",
                              contingency.contingency_id, position, slot)
        for step in contingency.steps:
            self._upsert_step(contingency.contingency_id, step)
        self._commit()

    def _upsert_step(self, contingency_id: str, step: ApprovalStep) -> None:
        self.connection.execute(
            """INSERT INTO approval_steps(contingency_id, level, approver_id,
                   status, comment, decided_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(contingency_id, level) DO UPDATE SET
                   approver_id=excluded.approver_id, status=excluded.status,
                   comment=excluded.comment, decided_at=excluded.decided_at""",
            (contingency_id, step.level, step.approver_id, step.status,
             step.comment, step.decided_at),
        )
        self._commit()

    def update_contingency(self, contingency_id: str, state: str | None = None,
                           new_plan_id: str | None = None) -> None:
        fields, params = [], []
        if state is not None:
            fields.append("state=?")
            params.append(state)
        if new_plan_id is not None:
            fields.append("new_plan_id=?")
            params.append(new_plan_id)
        if not fields:
            return
        params.append(contingency_id)
        self.connection.execute(
            f"UPDATE contingencies SET {', '.join(fields)} WHERE contingency_id=?",
            params,
        )

    def _contingency_from_row(self, row: sqlite3.Row) -> Contingency:
        slot_rows = self.connection.execute(
            """SELECT item_id, program_id, program_version, title, artist_ids,
                      stage_id, starts_at, ends_at, buffer_minutes, is_replacement
               FROM contingency_slots WHERE contingency_id=? ORDER BY position""",
            (row["contingency_id"],),
        ).fetchall()
        step_rows = self.connection.execute(
            """SELECT level, approver_id, status, comment, decided_at
               FROM approval_steps WHERE contingency_id=? ORDER BY level""",
            (row["contingency_id"],),
        ).fetchall()
        return Contingency(
            contingency_id=row["contingency_id"], incident_id=row["incident_id"],
            plan_id=row["plan_id"], created_by=row["created_by"],
            created_at=row["created_at"], required_levels=row["required_levels"],
            state=row["state"], new_plan_id=row["new_plan_id"], note=row["note"],
            slots=[self._slot_from_row(s) for s in slot_rows],
            steps=[ApprovalStep(s["level"], s["approver_id"], s["status"],
                                s["comment"], s["decided_at"]) for s in step_rows],
        )

    def get_contingency(self, contingency_id: str) -> Contingency | None:
        row = self.connection.execute(
            "SELECT * FROM contingencies WHERE contingency_id=?", (contingency_id,)
        ).fetchone()
        return self._contingency_from_row(row) if row else None

    def list_contingencies(self, incident_id: str = "") -> list[Contingency]:
        if incident_id:
            rows = self.connection.execute(
                "SELECT * FROM contingencies WHERE incident_id=? ORDER BY rowid",
                (incident_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM contingencies ORDER BY rowid"
            ).fetchall()
        return [self._contingency_from_row(r) for r in rows]

    def close(self) -> None:
        self.connection.close()

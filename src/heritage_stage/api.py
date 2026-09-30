"""供进程内调用的轻量请求适配层。"""
from __future__ import annotations

import json
from typing import Any, Optional

from .domain import FakeClock
from .service import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    PermissionDenied,
    Service,
    ValidationError,
)
from .store import Store


def make_service(store_path: str = ":memory:",
                 clock_start: Optional[str] = None) -> Service:
    """构造服务；clock_start 非空时使用模拟时钟，便于跨日测试。"""
    clock = FakeClock(clock_start) if clock_start else None
    return Service(Store(store_path), clock=clock)


def _require(body: dict, key: str) -> Any:
    if key not in body:
        raise ValidationError(f"缺少参数：{key}")
    return body[key]


def dispatch(service: Service, body: dict) -> Any:
    action = body.get("action")
    actor = str(body.get("actor_id", "planner-1"))

    if action == "health":
        return service.health()
    if action == "register":
        return service.register(str(body["record_id"]), str(body["owner_id"]))
    if action == "find":
        return service.find(str(body["record_id"]))

    if action == "grant_role":
        return service.grant_role(str(_require(body, "user_id")),
                                  str(_require(body, "role")),
                                  int(body.get("level", 0)))

    if action == "register_artist":
        return service.register_artist(
            actor, str(_require(body, "artist_id")), str(_require(body, "name")))
    if action == "register_stage":
        return service.register_stage(
            actor, str(_require(body, "stage_id")), str(_require(body, "name")))
    if action == "register_program_version":
        return service.register_program_version(
            actor, str(_require(body, "program_id")), int(_require(body, "version")),
            str(_require(body, "title")), str(_require(body, "genre")),
            int(_require(body, "duration_minutes")),
            list(body.get("artist_ids", [])))

    if action == "create_plan":
        return service.create_plan(
            actor, str(_require(body, "plan_id")), str(_require(body, "stage_id")),
            list(_require(body, "schedule")),
            int(body.get("buffer_before", 15)), int(body.get("buffer_after", 15)),
            str(body.get("note", "")))
    if action == "publish_plan":
        return service.publish_plan(
            actor, str(_require(body, "plan_id")),
            body.get("version") and int(body["version"]))
    if action == "get_plan":
        return service.get_plan(str(_require(body, "plan_id")),
                                body.get("version") and int(body["version"]))
    if action == "list_plan_versions":
        return service.list_plan_versions(str(_require(body, "plan_id")))

    if action == "freeze_publication":
        return service.freeze_publication(
            actor, str(_require(body, "plan_id")),
            body.get("version") and int(body["version"]),
            tuple(body.get("approver_levels", (1, 2))))
    if action == "decide_approval":
        return service.decide_approval(
            actor, str(_require(body, "approval_id")),
            str(_require(body, "decision")), str(body.get("comment", "")))
    if action == "withdraw_approval":
        return service.withdraw_approval(
            actor, str(_require(body, "approval_id")), str(body.get("comment", "")))
    if action == "approval_status":
        return service.approval_status(str(_require(body, "scope_type")),
                                       str(_require(body, "scope_id")))

    if action == "close_stage":
        return service.close_stage(
            actor, str(_require(body, "stage_id")), str(body.get("reason", "")),
            body.get("event_id") and str(body["event_id"]))
    if action == "withdraw_artist":
        return service.withdraw_artist(
            actor, str(_require(body, "artist_id")), str(body.get("reason", "")),
            body.get("event_id") and str(body["event_id"]))
    if action == "reopen_stage":
        return service.reopen_stage(actor, str(_require(body, "stage_id")))
    if action == "get_event":
        return service.get_event(str(_require(body, "event_id")))
    if action == "list_pending_events":
        return service.list_pending_events()

    if action == "create_contingency":
        return service.create_contingency(
            actor, str(_require(body, "event_id")),
            list(_require(body, "schedule")),
            body.get("target_stage_id") and str(body["target_stage_id"]),
            body.get("affected_plan_id") and str(body["affected_plan_id"]),
            int(body.get("buffer_before", 15)), int(body.get("buffer_after", 15)),
            tuple(body.get("approver_levels", (1, 2))), str(body.get("note", "")))

    raise ValueError("不支持的请求动作")


def handle(payload: str, service: Service | None = None) -> str:
    service = service or Service()
    try:
        body = json.loads(payload)
        result = dispatch(service, body)
    except ConflictError as exc:
        return json.dumps({"ok": False, "error": "conflict",
                           "message": str(exc),
                           "conflicts": [c.as_dict() for c in exc.conflicts]},
                          ensure_ascii=False)
    except PermissionDenied as exc:
        return json.dumps({"ok": False, "error": "permission_denied",
                           "message": str(exc)}, ensure_ascii=False)
    except (NotFoundError, ValidationError, InvalidStateError) as exc:
        return json.dumps({"ok": False,
                           "error": type(exc).__name__.replace("Error", "").lower(),
                           "message": str(exc)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)

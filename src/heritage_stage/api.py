"""供进程内调用的轻量请求适配层。

所有请求都是 ``{"action": ..., ...参数}`` 的 JSON 文本，返回 JSON 文本。
业务错误统一封装成 ``{"error": 错误码, "message": 中文说明}``，
不向调用方泄露异常类型；测试一般直接使用 :class:`Service` 以注入模拟时钟。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .domain import SchedulingError
from .service import Service


def handle(payload: str, service: Service | None = None) -> str:
    service = service or Service()
    body = json.loads(payload)
    action = body.get("action")
    handler = _ACTIONS.get(action)
    if handler is None:
        raise ValueError("不支持的请求动作")
    try:
        result = handler(service, body)
    except SchedulingError as exc:
        return json.dumps(
            {"error": exc.code, "message": str(exc)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


def _take(body: dict[str, Any], key: str, default: Any = None) -> Any:
    return body[key] if key in body else default


# ------------------------------------------------------------ 各动作的适配函数

def _health(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.health()


def _register(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.register(str(body["record_id"]), str(body["owner_id"]))


def _register_user(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.register_user(
        str(body["user_id"]), str(_take(body, "name", "")),
        tuple(_take(body, "roles", ())))


def _register_artist(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.register_artist(
        str(body["artist_id"]), str(body["name"]),
        str(_take(body, "genre", "")))


def _register_stage(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.register_stage(
        str(body["stage_id"]), str(body["name"]),
        str(_take(body, "location", "")))


def _register_program(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.register_program_version(
        program_id=str(body["program_id"]), title=str(body["title"]),
        artist_ids=list(body["artist_ids"]),
        duration_minutes=int(body["duration_minutes"]),
        category=str(_take(body, "category", "")),
        actor_id=str(_take(body, "actor_id", "")),
        version=int(body["version"]) if _take(body, "version") is not None else None)


def _generate_plan(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.generate_plan(
        label=str(body["label"]), entries=list(body["entries"]),
        actor_id=str(_take(body, "actor_id", "")),
        plan_start=_take(body, "plan_start"),
        default_buffer_minutes=int(_take(body, "default_buffer_minutes", 15)),
        stage_ids=list(_take(body, "stage_ids", [])))


def _publish_plan(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.publish_plan(str(body["plan_id"]),
                                str(_take(body, "actor_id", "")))


def _get_plan(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.get_plan(str(body["plan_id"]))


def _report_stage_closed(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.report_stage_closed(
        stage_id=str(body["stage_id"]), plan_id=str(body["plan_id"]),
        actor_id=str(body["actor_id"]), starts_at=_take(body, "starts_at"),
        ends_at=str(_take(body, "ends_at", "")))


def _report_artist_withdrew(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.report_artist_withdrew(
        artist_id=str(body["artist_id"]), plan_id=str(body["plan_id"]),
        actor_id=str(body["actor_id"]), starts_at=_take(body, "starts_at"))


def _create_contingency(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.create_contingency(
        incident_id=str(body["incident_id"]), actor_id=str(body["actor_id"]),
        entries=list(body["entries"]),
        required_levels=int(_take(body, "required_levels", 2)),
        note=str(_take(body, "note", "")),
        default_buffer_minutes=int(_take(body, "default_buffer_minutes", 15)),
        plan_start=_take(body, "plan_start"))


def _decide_contingency(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.decide_contingency(
        contingency_id=str(body["contingency_id"]),
        approver_id=str(body["approver_id"]), action=str(body["decision"]),
        comment=str(_take(body, "comment", "")))


def _withdraw_contingency(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.withdraw_contingency(
        str(body["contingency_id"]), str(body["actor_id"]))


def _reopen_stage(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.reopen_stage(str(body["stage_id"]), str(body["actor_id"]))


def _latest_effective_plan(service: Service, body: dict[str, Any]) -> dict[str, Any] | None:
    origin = _take(body, "origin_plan_id")
    return service.latest_effective_plan(str(origin) if origin else None)


def _get_incident(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.get_incident(str(body["incident_id"]))


def _get_contingency(service: Service, body: dict[str, Any]) -> dict[str, Any]:
    return service.get_contingency(str(body["contingency_id"]))


def _audit_events(service: Service, body: dict[str, Any]) -> list[dict[str, Any]]:
    return service.list_audit_events()


_ACTIONS: dict[str, Callable[[Service, dict[str, Any]], Any]] = {
    "health": _health,
    "register": _register,
    "register_user": _register_user,
    "register_artist": _register_artist,
    "register_stage": _register_stage,
    "register_program_version": _register_program,
    "generate_plan": _generate_plan,
    "publish_plan": _publish_plan,
    "get_plan": _get_plan,
    "report_stage_closed": _report_stage_closed,
    "report_artist_withdrew": _report_artist_withdrew,
    "create_contingency": _create_contingency,
    "decide_contingency": _decide_contingency,
    "withdraw_contingency": _withdraw_contingency,
    "reopen_stage": _reopen_stage,
    "latest_effective_plan": _latest_effective_plan,
    "get_incident": _get_incident,
    "get_contingency": _get_contingency,
    "audit_events": _audit_events,
}

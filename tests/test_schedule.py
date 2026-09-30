"""排期服务场景测试：全部通过 FakeClock 驱动模拟时钟。"""
import json
import tempfile
import unittest
from pathlib import Path

from heritage_stage.api import handle
from heritage_stage.domain import FakeClock, cross_day_boundaries
from heritage_stage.service import (
    ConflictError,
    InvalidStateError,
    PermissionDenied,
    Service,
    ValidationError,
)
from heritage_stage.store import Store

START = "2026-09-30T14:00:00+00:00"


def new_service(start: str = START) -> Service:
    return Service(Store(), clock=FakeClock(start))


def seed(service: Service) -> None:
    """登记角色、两处舞台、艺人和节目版本。"""
    # 角色：排期员、统筹者、两级冻结审批人、两级替代方案审批人
    service.grant_role("planner-1", "planner")
    service.grant_role("coord-1", "coordinator")
    service.grant_role("fa-1", "freeze_approver", 1)
    service.grant_role("fa-2", "freeze_approver", 2)
    service.grant_role("fa-3", "freeze_approver", 3)
    service.grant_role("ca-1", "contingency_approver", 1)
    service.grant_role("ca-2", "contingency_approver", 2)

    service.register_stage("planner-1", "yuyuan", "豫园舞台")
    service.register_stage("planner-1", "bund", "外滩舞台")
    service.register_stage("planner-1", "jingan", "静安备用舞台")

    service.register_artist("planner-1", "zhang", "张老师（昆曲）")
    service.register_artist("planner-1", "li", "李老师（越剧）")
    service.register_artist("planner-1", "chen", "陈老师（京剧）")
    service.register_artist("planner-1", "lisa", "DJ Lisa（电子）")

    service.register_program_version(
        "planner-1", "mudan", 1, "牡丹亭", "昆曲", 60, ["zhang"])
    service.register_program_version(
        "planner-1", "mudan", 2, "牡丹亭·精简版", "昆曲", 45, ["zhang"])
    service.register_program_version(
        "planner-1", "liangzhu", 1, "梁祝", "越剧", 45, ["li"])
    service.register_program_version(
        "planner-1", "jinmin", 1, "惊梦", "京剧", 40, ["chen"])
    service.register_program_version(
        "planner-1", "electric", 1, "浦江电音现场", "电子", 60, ["lisa"])
    # 替代节目：不含退出艺人
    service.register_program_version(
        "planner-1", "liangzhu", 2, "梁祝·下乡版", "越剧", 45, ["li"])
    service.register_program_version(
        "planner-1", "electric", 2, "浦江电音现场（双人）", "电子", 60,
        ["lisa", "chen"])


def entry(item_id, stage_id, program_id, version, start, duration=60,
          bb=15, ba=15):
    return {"item_id": item_id, "stage_id": stage_id, "program_id": program_id,
            "version": version, "start": start,
            "duration_minutes": duration,
            "buffer_before": bb, "buffer_after": ba}


def approve_chain(service, prefix, scope_id, approvers, levels):
    for level, approver in zip(levels, approvers):
        result = service.decide_approval(
            approver, f"ap:{prefix}:{scope_id}:l{level}", "approve")
    return result


class 跨日演出测试(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        seed(self.service)

    def test_跨午夜排期与锁定窗口(self):
        # 豫园 23:30 开演、60 分钟，次日 00:30 散场
        plan = self.service.create_plan(
            "planner-1", "yy-eve", "yuyuan",
            [entry("s1", "yuyuan", "mudan", 1,
                   "2026-10-01T23:30:00+00:00")],
            note="跨日场")
        stored = self.service.store.get_plan("yy-eve", 1)
        self.assertEqual(cross_day_boundaries(stored.items), ["s1"])
        item = plan["items"][0]
        self.assertEqual(item["end"], "2026-10-02T00:30:00+00:00")
        # 缓冲窗口延伸到次日 00:45
        self.assertEqual(item["window_end"], "2026-10-02T00:45:00+00:00")

        published = self.service.publish_plan("planner-1", "yy-eve", 1)
        self.assertEqual(published["state"], "published")
        self.assertEqual(published["published_at"], START)

        # 模拟时钟跨过演出日：次日凌晨同一舞台的加场仍与跨日场锁冲突
        self.service.clock.set("2026-10-02T00:30:00+00:00")
        self.service.create_plan(
            "planner-1", "yy-late", "yuyuan",
            [entry("s2", "yuyuan", "electric", 1,
                   "2026-10-02T00:30:00+00:00", duration=60, bb=0, ba=0)])
        with self.assertRaises(ConflictError) as ctx:
            self.service.publish_plan("planner-1", "yy-late", 1)
        self.assertEqual(ctx.exception.conflicts[0].resource_type, "stage")
        self.assertEqual(ctx.exception.conflicts[0].other_plan_id, "yy-eve")

        # 00:45 缓冲结束后（模拟时间推进）新场次可发布
        plan2 = self.service.create_plan(
            "planner-1", "yy-dawn", "yuyuan",
            [entry("s3", "yuyuan", "liangzhu", 1,
                   "2026-10-02T00:45:00+00:00", duration=30, bb=0, ba=0)])
        self.service.publish_plan("planner-1", "yy-dawn", 1)


class 发布与资源锁测试(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        seed(self.service)

    def test_重复发布同一版本被拒绝(self):
        self.service.create_plan(
            "planner-1", "p1", "yuyuan",
            [entry("a", "yuyuan", "mudan", 1, "2026-10-05T19:00:00+00:00")])
        self.service.publish_plan("planner-1", "p1", 1)
        with self.assertRaises(InvalidStateError):
            self.service.publish_plan("planner-1", "p1", 1)

    def test_舞台与艺人锁定后冲突发布被整体拒绝(self):
        # 豫园：张老师 19:00-20:00（缓冲至 20:15）
        self.service.create_plan(
            "planner-1", "p1", "yuyuan",
            [entry("a", "yuyuan", "mudan", 1, "2026-10-05T19:00:00+00:00")])
        self.service.publish_plan("planner-1", "p1", 1)

        # 外滩同时段同艺人：舞台不撞，但艺人跨场撞期
        self.service.create_plan(
            "planner-1", "p2", "bund",
            [entry("b", "bund", "mudan", 2, "2026-10-05T19:40:00+00:00",
                   duration=45)])
        with self.assertRaises(ConflictError) as ctx:
            self.service.publish_plan("planner-1", "p2", 1)
        resources = {(c.resource_type, c.resource_id)
                     for c in ctx.exception.conflicts}
        self.assertIn(("artist", "zhang"), resources)
        self.assertNotIn(("stage", "yuyuan"), resources)
        # 被拒后仍是草稿，没有产生锁
        self.assertEqual(self.service.get_plan("p2", 1)["state"], "draft")

        # 同舞台 20:05 开演：前缓冲 19:50 撞上一场演出
        self.service.create_plan(
            "planner-1", "p3", "yuyuan",
            [entry("c", "yuyuan", "electric", 1,
                   "2026-10-05T20:05:00+00:00")])
        with self.assertRaises(ConflictError):
            self.service.publish_plan("planner-1", "p3", 1)

        # 20:30 开演：前缓冲 20:15 与上一场缓冲末端相接，半开区间不冲突
        self.service.create_plan(
            "planner-1", "p4", "yuyuan",
            [entry("d", "yuyuan", "electric", 1,
                   "2026-10-05T20:30:00+00:00")])
        self.service.publish_plan("planner-1", "p4", 1)

    def test_同计划内缓冲重叠与艺人撞期校验(self):
        with self.assertRaises(ValidationError):
            self.service.create_plan(
                "planner-1", "bad", "yuyuan",
                [entry("x1", "yuyuan", "mudan", 1,
                       "2026-10-05T19:00:00+00:00"),
                 entry("x2", "yuyuan", "liangzhu", 1,
                       "2026-10-05T19:40:00+00:00")])
        # 同一艺人两场在异地的重叠排期也不允许进入同一计划
        with self.assertRaises(ValidationError):
            self.service.create_plan(
                "planner-1", "bad2", "yuyuan",
                [entry("y1", "yuyuan", "mudan", 1,
                       "2026-10-05T19:00:00+00:00"),
                 entry("y2", "yuyuan", "mudan", 2,
                       "2026-10-05T19:30:00+00:00", duration=45)])


class 冻结公开节目单测试(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        seed(self.service)

    def test_逐级审批通过后版本冻结(self):
        self.service.create_plan(
            "planner-1", "fz", "yuyuan",
            [entry("f", "yuyuan", "mudan", 1, "2026-10-06T19:00:00+00:00")])
        self.service.publish_plan("planner-1", "fz", 1)
        self.service.freeze_publication("planner-1", "fz", 1)
        scope = "fz:v1"

        # 跳级审批不允许
        with self.assertRaises(InvalidStateError):
            self.service.decide_approval("fa-2", f"ap:freeze:{scope}:l2",
                                         "approve")
        # 无权限者不能审批
        with self.assertRaises(PermissionDenied):
            self.service.decide_approval("fa-2", f"ap:freeze:{scope}:l1",
                                         "approve")
        self.service.decide_approval("fa-1", f"ap:freeze:{scope}:l1", "approve")
        result = self.service.decide_approval(
            "fa-2", f"ap:freeze:{scope}:l2", "approve")
        self.assertEqual(result["overall"], "approved")
        self.assertEqual(result["finalized"], "frozen")
        self.assertEqual(self.service.get_plan("fz", 1)["state"], "frozen")

        # 节目版本号随之冻结：同版本不可再登记
        with self.assertRaises(InvalidStateError):
            self.service.register_program_version(
                "planner-1", "mudan", 1, "牡丹亭", "昆曲", 60, ["zhang"])
        # 冻结计划不能重复发布
        with self.assertRaises(InvalidStateError):
            self.service.publish_plan("planner-1", "fz", 1)

        # 冻结版本的锁仍然拦截冲突发布
        self.service.create_plan(
            "planner-1", "fz2", "yuyuan",
            [entry("g", "yuyuan", "electric", 1,
                   "2026-10-06T19:30:00+00:00")])
        with self.assertRaises(ConflictError) as ctx:
            self.service.publish_plan("planner-1", "fz2", 1)
        self.assertEqual(ctx.exception.conflicts[0].other_version, 1)

        # 新版本发布后，冻结的 v1 仍原样保留
        self.service.create_plan(
            "planner-1", "fz", "yuyuan",
            [entry("h", "yuyuan", "electric", 1,
                   "2026-10-07T19:00:00+00:00")])
        self.service.publish_plan("planner-1", "fz", 2)
        self.assertEqual(self.service.get_plan("fz", 1)["state"], "frozen")
        self.assertEqual(self.service.get_plan("fz", 2)["state"], "published")


class 审批撤回测试(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        seed(self.service)
        self.service.create_plan(
            "planner-1", "wd", "yuyuan",
            [entry("w", "yuyuan", "mudan", 1, "2026-10-08T19:00:00+00:00")])
        self.service.publish_plan("planner-1", "wd", 1)
        self.service.freeze_publication("planner-1", "wd", 1,
                                        approver_levels=(1, 2, 3))
        self.scope = "wd:v1"

    def test_撤回使后续通过失效且可重走(self):
        s = self.service
        s.decide_approval("fa-1", "ap:freeze:wd:v1:l1", "approve")
        s.decide_approval("fa-2", "ap:freeze:wd:v1:l2", "approve")
        # 三级未批之前撤回一级：二级通过连带失效
        result = s.withdraw_approval("fa-1", "ap:freeze:wd:v1:l1", "材料待补")
        self.assertEqual(result["overall"], "broken")
        levels = {l["level"]: l["state"] for l in result["levels"]}
        self.assertEqual(levels[1], "withdrawn")
        self.assertEqual(levels[2], "withdrawn")
        self.assertEqual(levels[3], "pending")
        # 计划未冻结
        self.assertEqual(s.get_plan("wd", 1)["state"], "published")

        # 重新逐级审批：一级、二级重批，三级通过后冻结
        s.decide_approval("fa-1", "ap:freeze:wd:v1:l1", "approve")
        with self.assertRaises(InvalidStateError):
            # 二级失效后不能跳去批三级
            s.decide_approval("fa-3", "ap:freeze:wd:v1:l3", "approve")
        s.decide_approval("fa-2", "ap:freeze:wd:v1:l2", "approve")
        final = s.decide_approval("fa-3", "ap:freeze:wd:v1:l3", "approve")
        self.assertEqual(final["overall"], "approved")
        self.assertEqual(s.get_plan("wd", 1)["state"], "frozen")

        # 全部生效后不能再撤回
        with self.assertRaises(InvalidStateError):
            s.withdraw_approval("fa-1", "ap:freeze:wd:v1:l1")

    def test_驳回终局(self):
        s = self.service
        s.decide_approval("fa-1", "ap:freeze:wd:v1:l1", "reject", "信息不全")
        status = s.approval_status("freeze", "wd:v1")
        self.assertEqual(status["overall"], "rejected")
        self.assertEqual(s.get_plan("wd", 1)["state"], "published")
        # 已驳回不可重复决定
        with self.assertRaises(InvalidStateError):
            s.decide_approval("fa-1", "ap:freeze:wd:v1:l1", "approve")


class 封闭退出与替代方案测试(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        seed(self.service)

    def test_无统筹权限不能封闭或建替代方案(self):
        self.service.create_plan(
            "planner-1", "yy", "yuyuan",
            [entry("k", "yuyuan", "mudan", 1, "2026-10-09T19:00:00+00:00")])
        self.service.publish_plan("planner-1", "yy", 1)
        with self.assertRaises(PermissionDenied):
            self.service.close_stage("planner-1", "yuyuan", "防汛")
        self.service.close_stage("coord-1", "yuyuan", "防汛封闭")
        with self.assertRaises(PermissionDenied):
            self.service.create_contingency(
                "planner-1", self.service.list_pending_events()[0]["event_id"],
                [entry("r", "jingan", "liangzhu", 1,
                       "2026-10-09T19:00:00+00:00")],
                target_stage_id="jingan")

    def test_封闭后替代方案保留原计划与影响范围(self):
        s = self.service
        s.create_plan(
            "planner-1", "yy", "yuyuan",
            [entry("k1", "yuyuan", "mudan", 1,
                   "2026-10-09T19:00:00+00:00"),
             entry("k2", "yuyuan", "electric", 1,
                   "2026-10-09T20:30:00+00:00")])
        s.publish_plan("planner-1", "yy", 1)
        original = s.get_plan("yy", 1)

        # 模拟时钟走到演出当天，场地封闭
        s.clock.set("2026-10-09T12:00:00+00:00")
        event = s.close_stage("coord-1", "yuyuan", "防汛封闭")
        self.assertEqual(event["kind"], "stage_closed")
        self.assertEqual(len(event["impact_scope"]), 1)
        self.assertEqual(event["impact_scope"][0]["item_ids"], ["k1", "k2"])

        # 原计划保留但失效并释放锁
        preserved = s.get_plan("yy", 1)
        self.assertEqual(preserved["state"], "void")
        self.assertEqual(len(preserved["items"]), 2)  # 内容原样保留
        self.assertEqual(preserved["items"][0]["program_id"], "mudan")
        # 封闭期间旧窗口已无锁，其他计划可占用
        s.create_plan(
            "planner-1", "other", "yuyuan",
            [entry("o", "yuyuan", "liangzhu", 1,
                   "2026-10-09T19:30:00+00:00")])
        # 但舞台封闭本身仍阻止发布
        with self.assertRaises(InvalidStateError):
            s.publish_plan("planner-1", "other", 1)

        # 统筹者在备用舞台创建替代方案
        proposal = s.create_contingency(
            "coord-1", event["event_id"],
            [entry("r1", "jingan", "liangzhu", 1,
                   "2026-10-09T19:00:00+00:00"),
             entry("r2", "jingan", "jinmin", 1,
                   "2026-10-09T20:30:00+00:00", duration=40)],
            target_stage_id="jingan", note="移师静安")
        self.assertEqual(proposal["preserves_plan"],
                         {"plan_id": "yy", "version": 1})
        self.assertEqual(proposal["contingency_for"], event["event_id"])
        scope = proposal["scope_id"]  # yy:v2

        # 替代方案不能直接发布，必须走逐级审批
        with self.assertRaises(InvalidStateError):
            s.publish_plan("planner-1", "yy", 2)
        s.decide_approval("ca-1", f"ap:contingency:{scope}:l1", "approve",
                          "舞台经理确认")
        final = s.decide_approval(
            "ca-2", f"ap:contingency:{scope}:l2", "approve", "总监批准")
        self.assertEqual(final["overall"], "approved")
        self.assertEqual(final["finalized"], "contingency_published")

        # 替代方案成为当前有效计划，事件结案；v1 仍保留
        self.assertEqual(s.get_plan("yy", 2)["state"], "published")
        self.assertEqual(s.get_plan("yy", 1)["state"], "void")
        self.assertEqual(s.get_event(event["event_id"])["handled"], True)
        self.assertEqual(s.get_event(event["event_id"])["resolution"],
                         "contingency")
        self.assertEqual(s.latest_effective_plan("jingan")["version"], 2)

        # 替代方案的锁开始拦截冲突
        s.create_plan(
            "planner-1", "clash", "jingan",
            [entry("z", "jingan", "electric", 1,
                   "2026-10-09T19:40:00+00:00")])
        with self.assertRaises(ConflictError):
            s.publish_plan("planner-1", "clash", 1)

    def test_重新开放后从最近有效版本继续(self):
        s = self.service
        s.create_plan(
            "planner-1", "rs", "yuyuan",
            [entry("q", "yuyuan", "mudan", 1,
                   "2026-10-10T19:00:00+00:00")])
        s.publish_plan("planner-1", "rs", 1)
        event = s.close_stage("coord-1", "yuyuan", "设备检修")
        self.assertEqual(s.get_plan("rs", 1)["state"], "void")

        # 重新开放：事件关闭，续排指向被保留的封闭前版本
        s.clock.advance(days=2)
        reopened = s.reopen_stage("coord-1", "yuyuan")
        self.assertFalse(reopened["closed"])
        self.assertIn(event["event_id"], reopened["closed_events"])
        resume = reopened["resume_from"]
        self.assertEqual(resume["plan_id"], "rs")
        self.assertEqual(resume["version"], 1)
        self.assertEqual(resume["resume_basis"], "preserved_pre_closure")

        # 从该版本续排出新版本并发布
        basis_items = [
            {**{"item_id": it["item_id"], "stage_id": it["stage_id"],
                "program_id": it["program_id"], "version": it["version"],
                "start": it["start"], "duration_minutes": 60}}
            for it in resume["items"]
        ]
        s.create_plan("planner-1", "rs", "yuyuan", basis_items,
                      note="重开后续排")
        s.publish_plan("planner-1", "rs", 2)
        self.assertEqual(s.latest_effective_plan("yuyuan")["version"], 2)
        self.assertEqual(s.latest_effective_plan("yuyuan")["resume_basis"],
                         "active")


class 双场同时变更测试(unittest.TestCase):
    def setUp(self):
        self.service = new_service()
        seed(self.service)

    def test_双场替代方案逐级审批后的最终计划(self):
        s = self.service
        s.create_plan(
            "planner-1", "yy-night", "yuyuan",
            [entry("yy1", "yuyuan", "mudan", 1,
                   "2026-10-11T19:00:00+00:00")])
        s.publish_plan("planner-1", "yy-night", 1)
        s.create_plan(
            "planner-1", "bd-night", "bund",
            [entry("bd1", "bund", "mudan", 2,
                   "2026-10-11T21:00:00+00:00", duration=45)])
        s.publish_plan("planner-1", "bd-night", 1)

        event = s.withdraw_artist("coord-1", "zhang", "突发伤病")
        event_id = event["event_id"]

        # 含退出艺人的替代案必须被拒
        with self.assertRaises(ValidationError):
            s.create_contingency(
                "coord-1", event_id,
                [entry("bad", "yuyuan", "mudan", 1,
                       "2026-10-11T19:00:00+00:00")],
                affected_plan_id="yy-night")

        # 豫园替代案：换李老师的梁祝
        yy_plan = s.create_contingency(
            "coord-1", event_id,
            [entry("yy-r", "yuyuan", "liangzhu", 1,
                   "2026-10-11T19:00:00+00:00")],
            affected_plan_id="yy-night", note="豫园换戏")
        # 外滩替代案：陈老师的惊梦（40 分钟）
        bd_plan = s.create_contingency(
            "coord-1", event_id,
            [entry("bd-r", "bund", "jinmin", 1,
                   "2026-10-11T21:00:00+00:00", duration=40)],
            affected_plan_id="bd-night", note="外滩换戏")

        # 先批完豫园两级：事件因外滩尚未解决而保持待处理
        approve_chain(s, "contingency", "yy-night:v2",
                      ["ca-1", "ca-2"], [1, 2])
        self.assertEqual(s.get_event(event_id)["handled"], False)
        self.assertEqual(s.get_plan("yy-night", 2)["state"], "published")

        # 外滩两级审批中，一级过后撤回，计划不得生效
        s.decide_approval("ca-1", "ap:contingency:bd-night:v2:l1", "approve")
        s.withdraw_approval("ca-1", "ap:contingency:bd-night:v2:l1",
                            "档期需再核")
        self.assertEqual(s.get_plan("bd-night", 2)["state"], "draft")
        # 重新走两级
        s.decide_approval("ca-1", "ap:contingency:bd-night:v2:l1", "approve")
        approve_chain(s, "contingency", "bd-night:v2",
                      ["ca-2"], [2])

        # 全部影响范围解决后事件才结案
        self.assertEqual(s.get_event(event_id)["handled"], True)

        # 最终计划核对：两场各自的新版本生效、原计划保留
        yy_final = s.latest_effective_plan("yuyuan")
        bd_final = s.latest_effective_plan("bund")
        self.assertEqual(yy_final["version"], 2)
        self.assertEqual(yy_final["items"][0]["program_id"], "liangzhu")
        self.assertEqual(bd_final["version"], 2)
        self.assertEqual(bd_final["items"][0]["program_id"], "jinmin")
        history_yy = {v["version"]: v["state"]
                      for v in s.list_plan_versions("yy-night")}
        history_bd = {v["version"]: v["state"]
                      for v in s.list_plan_versions("bd-night")}
        self.assertEqual(history_yy, {1: "void", 2: "published"})
        self.assertEqual(history_bd, {1: "void", 2: "published"})

        # 最终锁表：张老师不再被锁；李老师豫园、陈老师外滩锁定
        s.create_plan(
            "planner-1", "probe-zhang", "bund",
            [entry("pz", "bund", "mudan", 2,
                   "2026-10-11T21:05:00+00:00", duration=45)])
        # 张老师锁随 v1 失效已释放，但外滩舞台仍被陈老师的替代案占用
        with self.assertRaises(ConflictError) as ctx:
            s.publish_plan("planner-1", "probe-zhang", 1)
        conflict_resources = {c.resource_id for c in ctx.exception.conflicts}
        self.assertIn("bund", conflict_resources)
        self.assertNotIn("zhang", conflict_resources)


class 持久化与JSON适配测试(unittest.TestCase):
    def test_关闭后重开存储仍保留版本链(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "season.db"
            svc = Service(Store(path), clock=FakeClock(START))
            seed(svc)
            svc.create_plan(
                "planner-1", "persist", "yuyuan",
                [entry("p", "yuyuan", "mudan", 1,
                       "2026-10-12T19:00:00+00:00")])
            svc.publish_plan("planner-1", "persist", 1)
            svc.close_stage("coord-1", "yuyuan", "临时封闭")
            svc.reopen_stage("coord-1", "yuyuan")

            reopened = Service(Store(path), clock=FakeClock(START))
            versions = reopened.list_plan_versions("persist")
            self.assertEqual([v["state"] for v in versions], ["void"])
            self.assertEqual(versions[0]["items"][0]["program_id"], "mudan")

    def test_json_冲突返回结构化错误(self):
        svc = new_service()
        seed(svc)
        svc.create_plan(
            "planner-1", "j1", "yuyuan",
            [entry("j", "yuyuan", "mudan", 1,
                   "2026-10-13T19:00:00+00:00")])
        svc.publish_plan("planner-1", "j1", 1)
        svc.create_plan(
            "planner-1", "j2", "yuyuan",
            [entry("j", "yuyuan", "electric", 1,
                   "2026-10-13T19:30:00+00:00")])
        payload = json.dumps(
            {"action": "publish_plan", "actor_id": "planner-1",
             "plan_id": "j2", "version": 1})
        response = json.loads(handle(payload, svc))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "conflict")
        self.assertTrue(response["conflicts"])

    def test_json_健康检查与登记基线(self):
        svc = new_service()
        health = json.loads(handle(json.dumps({"action": "health"}), svc))
        self.assertEqual(health["status"], "ok")

    def test_json_端到端冻结链路(self):
        svc = new_service()
        seed(svc)

        def call(body):
            return json.loads(handle(json.dumps(body), svc))

        plan = call({"action": "create_plan", "actor_id": "planner-1",
                     "plan_id": "e2e", "stage_id": "bund",
                     "schedule": [entry("e", "bund", "electric", 1,
                                        "2026-10-14T20:00:00+00:00")]})
        self.assertEqual(plan["version"], 1)
        self.assertEqual(call({"action": "publish_plan", "actor_id": "planner-1",
                               "plan_id": "e2e", "version": 1})["state"],
                         "published")
        call({"action": "freeze_publication", "actor_id": "planner-1",
              "plan_id": "e2e", "version": 1, "approver_levels": [1, 2]})
        l1 = call({"action": "decide_approval", "actor_id": "fa-1",
                   "approval_id": "ap:freeze:e2e:v1:l1", "decision": "approve"})
        self.assertEqual(l1["overall"], "pending")
        l2 = call({"action": "decide_approval", "actor_id": "fa-2",
                   "approval_id": "ap:freeze:e2e:v1:l2", "decision": "approve"})
        self.assertEqual(l2["finalized"], "frozen")
        frozen = call({"action": "get_plan", "plan_id": "e2e", "version": 1})
        self.assertEqual(frozen["state"], "frozen")
        self.assertTrue(frozen["frozen_at"])

        # 模拟时钟推进不影响已冻结版本的内容
        svc.clock.advance(days=30)
        self.assertEqual(
            call({"action": "get_plan", "plan_id": "e2e", "version": 1})["items"][0]["start"],
            "2026-10-14T20:00:00+00:00")

        # 无权限调用返回结构化错误
        denied = call({"action": "close_stage", "actor_id": "planner-1",
                       "stage_id": "bund", "reason": "x"})
        self.assertEqual(denied["error"], "permission_denied")


if __name__ == "__main__":
    unittest.main()

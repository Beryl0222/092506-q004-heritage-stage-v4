"""场地封闭、艺人退出、替代方案逐级审批与撤回的测试。"""
import unittest

from heritage_stage.domain import (
    ConflictError,
    InvalidTransitionError,
    PermissionDeniedError,
    ValidationError,
)
from tests._fixtures import build_festival


def 双舞台计划(f, start_date="2026-10-08"):
    plan = f.service.generate_plan(
        "音乐季正场",
        [
            {"program_id": "p-opera", "stage_id": "yuyuan",
             "starts_at": f"{start_date}T19:00:00+00:00",
             "buffer_minutes": 20},
            {"program_id": "p-elec", "stage_id": "bund",
             "starts_at": f"{start_date}T20:00:00+00:00",
             "buffer_minutes": 30},
        ],
        f.coord)
    return f.service.publish_plan(plan["plan_id"], f.coord)


class 场地封闭与影响范围测试(unittest.TestCase):
    def setUp(self):
        self.f = build_festival()
        self.plan = 双舞台计划(self.f)

    def test_封闭只影响该舞台条目(self):
        inc = self.f.service.report_stage_closed(
            "yuyuan", self.plan["plan_id"], self.f.coord,
            starts_at="2026-10-08T18:00:00+00:00",
            ends_at="2026-10-08T23:00:00+00:00")
        impacted = inc["impacted_items"]
        self.assertEqual(len(impacted), 1)
        impacted_item = impacted[0]["item_id"]
        opera_item = next(s for s in self.plan["slots"]
                          if s["stage_id"] == "yuyuan")["item_id"]
        self.assertEqual(impacted_item, opera_item)

    def test_封闭前的条目不在影响范围(self):
        inc = self.f.service.report_stage_closed(
            "yuyuan", self.plan["plan_id"], self.f.coord,
            starts_at="2026-10-08T20:00:00+00:00",
            ends_at="2026-10-08T23:00:00+00:00")
        self.assertEqual(inc["impacted_items"], [])

    def test_未发布计划不能登记封闭事件(self):
        draft = self.f.service.generate_plan(
            "草稿", [{"program_id": "p-folk", "stage_id": "bund",
                     "starts_at": "2026-10-20T19:00:00+00:00"}],
            self.f.coord)
        with self.assertRaises(InvalidTransitionError):
            self.f.service.report_stage_closed(
                "bund", draft["plan_id"], self.f.coord,
                starts_at="2026-10-20T18:00:00+00:00")


class 替代方案权限测试(unittest.TestCase):
    def setUp(self):
        self.f = build_festival()
        self.plan = 双舞台计划(self.f)
        self.inc = self.f.service.report_stage_closed(
            "yuyuan", self.plan["plan_id"], self.f.coord,
            starts_at="2026-10-08T18:00:00+00:00",
            ends_at="2026-10-08T23:00:00+00:00")

    def test_无统筹权限不能创建替代方案(self):
        with self.assertRaises(PermissionDeniedError):
            self.f.service.create_contingency(
                self.inc["incident_id"], self.f.viewer,
                [{"program_id": "p-opera", "stage_id": "bund",
                  "starts_at": "2026-10-09T19:00:00+00:00"}],
                required_levels=1)

    def test_封闭时段内仍不能使用该舞台(self):
        with self.assertRaises(ConflictError):
            self.f.service.create_contingency(
                self.inc["incident_id"], self.f.coord,
                [{"program_id": "p-opera", "stage_id": "yuyuan",
                  "starts_at": "2026-10-08T21:00:00+00:00"}],
                required_levels=1)

    def test_解封后可以沿用原舞台(self):
        ctg = self.f.service.create_contingency(
            self.inc["incident_id"], self.f.coord,
            [{"program_id": "p-opera", "stage_id": "yuyuan",
              "starts_at": "2026-10-08T23:30:00+00:00",
              "buffer_minutes": 20}],
            required_levels=1)
        self.assertEqual(ctg["state"], "proposed")

    def test_同一事件只能有一个待审批方案(self):
        self.f.service.create_contingency(
            self.inc["incident_id"], self.f.coord,
            [{"program_id": "p-opera", "stage_id": "bund",
              "starts_at": "2026-10-09T19:00:00+00:00"}],
            required_levels=1)
        with self.assertRaises(ConflictError):
            self.f.service.create_contingency(
                self.inc["incident_id"], self.f.coord,
                [{"program_id": "p-opera", "stage_id": "bund",
                  "starts_at": "2026-10-09T20:00:00+00:00"}],
                required_levels=1)


class 逐级审批与撤回测试(unittest.TestCase):
    def setUp(self):
        self.f = build_festival()
        self.plan = 双舞台计划(self.f)
        self.inc = self.f.service.report_stage_closed(
            "yuyuan", self.plan["plan_id"], self.f.coord,
            starts_at="2026-10-08T18:00:00+00:00",
            ends_at="2026-10-08T23:00:00+00:00")
        self.replacement = [
            {"program_id": "p-opera", "stage_id": "bund",
             "starts_at": "2026-10-09T19:00:00+00:00",
             "buffer_minutes": 15}]

    def _创建两级方案(self):
        return self.f.service.create_contingency(
            self.inc["incident_id"], self.f.coord, self.replacement,
            required_levels=2, note="豫园封场，昆曲挪至外滩次日")

    def test_逐级审批通过后生成新版本且保留原计划(self):
        ctg = self._创建两级方案()
        first = self.f.service.decide_contingency(
            ctg["contingency_id"], self.f.boss1, "approve", "主管同意")
        self.assertEqual(first["state"], "proposed")
        self.assertEqual(first["current_level"], 2)
        self.assertEqual(first["approval_steps"][0]["status"], "approved")
        self.assertEqual(first["approval_steps"][1]["status"], "pending")

        self.f.clock.advance(hours=3)
        second = self.f.service.decide_contingency(
            ctg["contingency_id"], self.f.boss2, "approve", "总监批准")
        self.assertEqual(second["state"], "approved")
        self.assertTrue(second["new_plan_id"])

        new_plan = self.f.service.get_plan(second["new_plan_id"])
        self.assertEqual(new_plan["version"], 2)
        self.assertEqual(new_plan["supersedes"], self.plan["plan_id"])
        # 替代条目 + 未受影响的外滩电音条目都在，整场顺序不失真
        stages = [(s["stage_id"], s["title"]) for s in new_plan["slots"]]
        self.assertEqual(len(stages), 2)
        self.assertIn(("bund", "浦江电音之夜"), stages)
        self.assertTrue(all(s["is_replacement"]
                            for s in new_plan["slots"]
                            if s["title"] == "牡丹亭"))

        # 原计划原样保留、只改状态为 superseded
        original = self.f.service.get_plan(self.plan["plan_id"])
        self.assertEqual(original["state"], "superseded")
        self.assertEqual(len(original["slots"]), 2)
        yuyuan_slot = next(s for s in original["slots"]
                           if s["stage_id"] == "yuyuan")
        self.assertEqual(yuyuan_slot["starts_at"],
                         "2026-10-08T19:00:00+00:00")

    def test_任一级驳回则方案作废且原计划不动(self):
        ctg = self._创建两级方案()
        self.f.service.decide_contingency(
            ctg["contingency_id"], self.f.boss1, "reject", "安全不达标")
        rejected = self.f.service.get_contingency(ctg["contingency_id"])
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(rejected["approval_steps"][0]["status"], "rejected")
        # 原计划仍是已发布（尽管条目受事件影响），等待新方案
        self.assertEqual(
            self.f.service.get_plan(self.plan["plan_id"])["state"],
            "published")

    def test_申请人撤回后可重建方案(self):
        ctg = self._创建两级方案()
        withdrawn = self.f.service.withdraw_contingency(
            ctg["contingency_id"], self.f.coord)
        self.assertEqual(withdrawn["state"], "withdrawn")
        # 已撤回方案不能再审批
        with self.assertRaises(InvalidTransitionError):
            self.f.service.decide_contingency(
                ctg["contingency_id"], self.f.boss1, "approve")
        # 撤回后允许统筹者重新创建方案并走完审批
        rebuilt = self.f.service.create_contingency(
            self.inc["incident_id"], self.f.coord, self.replacement,
            required_levels=1)
        result = self.f.service.decide_contingency(
            rebuilt["contingency_id"], self.f.boss1, "approve")
        self.assertEqual(result["state"], "approved")

    def test_非创建人不能撤回(self):
        ctg = self._创建两级方案()
        # boss1 是审批人但不是创建人，不能代替申请人撤回
        with self.assertRaises(PermissionDeniedError):
            self.f.service.withdraw_contingency(
                ctg["contingency_id"], self.f.boss1)

    def test_无审批权限不能决策(self):
        ctg = self._创建两级方案()
        with self.assertRaises(PermissionDeniedError):
            self.f.service.decide_contingency(
                ctg["contingency_id"], self.f.coord, "approve")

    def test_终审时封闭已解除则方案失效(self):
        ctg = self._创建两级方案()
        self.f.service.decide_contingency(
            ctg["contingency_id"], self.f.boss1, "approve")
        # 主管批完后场地重开，事件结束
        self.f.service.reopen_stage("yuyuan", self.f.coord)
        with self.assertRaises(InvalidTransitionError):
            self.f.service.decide_contingency(
                ctg["contingency_id"], self.f.boss2, "approve")
        refreshed = self.f.service.get_contingency(ctg["contingency_id"])
        self.assertEqual(refreshed["state"], "withdrawn")

    def test_艺人退出后替代节目不能仍含该艺人(self):
        inc = self.f.service.report_artist_withdrew(
            "a-dj", self.plan["plan_id"], self.f.coord,
            starts_at="2026-10-08T18:00:00+00:00")
        with self.assertRaises(ConflictError):
            self.f.service.create_contingency(
                inc["incident_id"], self.f.coord,
                [{"program_id": "p-elec", "stage_id": "bund",
                  "starts_at": "2026-10-09T20:00:00+00:00"}],
                required_levels=1)
        # 换成不含该艺人的民乐节目可以通过
        ctg = self.f.service.create_contingency(
            inc["incident_id"], self.f.coord,
            [{"program_id": "p-folk", "stage_id": "bund",
              "starts_at": "2026-10-09T20:00:00+00:00",
              "buffer_minutes": 15}],
            required_levels=1)
        result = self.f.service.decide_contingency(
            ctg["contingency_id"], self.f.boss1, "approve")
        self.assertEqual(result["state"], "approved")


class 重开恢复测试(unittest.TestCase):
    def setUp(self):
        self.f = build_festival()
        self.plan = 双舞台计划(self.f)

    def test_重开后从最近有效版本继续(self):
        # 封闭 -> 替代方案 v2（挪到次日外滩）
        inc = self.f.service.report_stage_closed(
            "yuyuan", self.plan["plan_id"], self.f.coord,
            starts_at="2026-10-08T18:00:00+00:00",
            ends_at="2026-10-08T23:00:00+00:00")
        ctg = self.f.service.create_contingency(
            inc["incident_id"], self.f.coord,
            [{"program_id": "p-opera", "stage_id": "bund",
              "starts_at": "2026-10-09T19:00:00+00:00",
              "buffer_minutes": 15}],
            required_levels=1)
        approved = self.f.service.decide_contingency(
            ctg["contingency_id"], self.f.boss1, "approve")
        v2 = self.f.service.get_plan(approved["new_plan_id"])

        self.f.clock.advance(days=2)
        result = self.f.service.reopen_stage("yuyuan", self.f.coord)
        self.assertIn(inc["incident_id"], result["resolved_incident_ids"])
        # 最近有效版本是 v2，而不是回到被取代的 v1
        resumed = self.f.service.latest_effective_plan(v2["origin_plan_id"])
        self.assertEqual(resumed["plan_id"], v2["plan_id"])
        self.assertEqual(resumed["version"], 2)
        self.assertEqual(result["resumed_plan_id"], v2["plan_id"])

    def test_无替代方案时重开恢复原计划(self):
        inc = self.f.service.report_stage_closed(
            "yuyuan", self.plan["plan_id"], self.f.coord,
            starts_at="2026-10-08T18:00:00+00:00",
            ends_at="2026-10-08T23:00:00+00:00")
        self.f.service.reopen_stage("yuyuan", self.f.coord)
        resumed = self.f.service.latest_effective_plan(
            self.plan["origin_plan_id"])
        self.assertEqual(resumed["plan_id"], self.plan["plan_id"])


if __name__ == "__main__":
    unittest.main()

"""进程内 JSON 适配层的端到端测试（含错误封装）。"""
import json
import unittest

from heritage_stage.api import handle
from heritage_stage.service import Service
from heritage_stage.store import Store
from heritage_stage.domain import FakeClock


def call(service, **body):
    return json.loads(handle(json.dumps(body, ensure_ascii=False), service))


class Api适配测试(unittest.TestCase):
    def setUp(self):
        clock = FakeClock("2026-10-01T08:00:00+00:00")
        self.service = Service(Store(), clock)

    def test_health与基线register(self):
        self.assertEqual(call(self.service, action="health")["status"], "ok")
        record = call(self.service, action="register",
                      record_id="r-1", owner_id="owner-1")
        self.assertEqual(record["state"], "draft")
        self.assertEqual(
            call(self.service, action="health")["service"], "heritage_stage")

    def test_排期全流程走json(self):
        call(self.service, action="register_user", user_id="coord",
             name="统筹", roles=["coordinator"])
        call(self.service, action="register_user", user_id="boss",
             name="主管", roles=["approver"])
        call(self.service, action="register_artist", artist_id="a1",
             name="昆剧团", genre="戏曲")
        call(self.service, action="register_stage", stage_id="yy",
             name="豫园", location="豫园")
        program = call(self.service, action="register_program_version",
                       program_id="p1", title="牡丹亭", artist_ids=["a1"],
                       duration_minutes=90, actor_id="coord")
        self.assertEqual(program["ref"], "p1@v1")

        plan = call(self.service, action="generate_plan", label="正场",
                    actor_id="coord", entries=[
                        {"program_id": "p1", "stage_id": "yy",
                         "starts_at": "2026-10-05T19:00:00+00:00",
                         "buffer_minutes": 20}])
        published = call(self.service, action="publish_plan",
                         plan_id=plan["plan_id"], actor_id="coord")
        self.assertEqual(published["state"], "published")

        # 重复发布 -> 结构化业务错误，而不是抛出异常
        dup = call(self.service, action="publish_plan",
                   plan_id=plan["plan_id"], actor_id="coord")
        self.assertEqual(dup["error"], "conflict")
        self.assertIn("冻结", dup["message"])

        incident = call(self.service, action="report_stage_closed",
                        stage_id="yy", plan_id=plan["plan_id"],
                        actor_id="coord",
                        starts_at="2026-10-05T18:00:00+00:00",
                        ends_at="2026-10-05T23:00:00+00:00")
        # 无权限创建替代方案
        denied = call(self.service, action="create_contingency",
                      incident_id=incident["incident_id"], actor_id="boss",
                      required_levels=1,
                      entries=[{"program_id": "p1", "stage_id": "yy",
                                "starts_at": "2026-10-05T23:30:00+00:00"}])
        self.assertEqual(denied["error"], "permission_denied")

        # 统筹者创建、主管单级审批
        ctg = call(self.service, action="create_contingency",
                   incident_id=incident["incident_id"], actor_id="coord",
                   required_levels=1,
                   entries=[{"program_id": "p1", "stage_id": "yy",
                             "starts_at": "2026-10-05T23:30:00+00:00",
                             "buffer_minutes": 20}])
        decision = call(self.service, action="decide_contingency",
                        contingency_id=ctg["contingency_id"],
                        approver_id="boss", decision="approve",
                        comment="同意")
        self.assertEqual(decision["state"], "approved")
        new_plan = call(self.service, action="get_plan",
                        plan_id=decision["new_plan_id"])
        self.assertEqual(new_plan["version"], 2)

        reopened = call(self.service, action="reopen_stage",
                        stage_id="yy", actor_id="coord")
        self.assertEqual(reopened["resumed_plan_id"],
                         decision["new_plan_id"])

        events = call(self.service, action="audit_events")
        actions = [e["action"] for e in events]
        self.assertIn("contingency_approved", actions)
        self.assertIn("stage_reopened", actions)

    def test_未知动作抛value_error(self):
        with self.assertRaises(ValueError):
            handle(json.dumps({"action": "nope"}), self.service)


if __name__ == "__main__":
    unittest.main()

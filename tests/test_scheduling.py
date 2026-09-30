"""排期生成、缓冲时间、跨日演出、资源锁定与版本冻结的测试。"""
import unittest
from datetime import datetime, timezone

from heritage_stage.domain import (
    ConflictError,
    DuplicateError,
    FakeClock,
    InvalidTransitionError,
    ValidationError,
    parse_time,
)
from tests._fixtures import build_festival


class 模拟时钟测试(unittest.TestCase):
    def test_时钟可推进并跳转跨日(self):
        clock = FakeClock("2026-10-03T23:30:00+00:00")
        self.assertEqual(clock.now().day, 3)
        clock.advance(minutes=40)
        self.assertEqual(clock.now().day, 4)
        clock.jump_to("2026-11-20T09:00:00+00:00")
        self.assertEqual(clock.now().month, 11)

    def test_裸时间按utc处理(self):
        moment = parse_time("2026-10-03T23:30:00")
        self.assertEqual(moment.tzinfo, timezone.utc)


class 节目版本登记测试(unittest.TestCase):
    def setUp(self):
        self.f = build_festival()

    def test_节目版本自动递增(self):
        v2 = self.f.service.register_program_version(
            "p-opera", "牡丹亭·修订版", ["a-opera"], 85, "南北戏曲",
            self.f.coord)
        self.assertEqual(v2["version"], 2)
        self.assertFalse(v2["published"])

    def test_重复版本号被拒绝(self):
        with self.assertRaises(DuplicateError):
            self.f.service.register_program_version(
                "p-opera", "牡丹亭", ["a-opera"], 90, "南北戏曲",
                self.f.coord, version=1)

    def test_未登记艺人不能进入节目(self):
        from heritage_stage.domain import NotFoundError
        with self.assertRaises(NotFoundError):
            self.f.service.register_program_version(
                "p-x", "神秘节目", ["a-ghost"], 60, actor_id=self.f.coord)

    def test_时长与艺人校验(self):
        with self.assertRaises(ValidationError):
            self.f.service.register_program_version(
                "p-bad", "零时长", ["a-opera"], 0, actor_id=self.f.coord)
        with self.assertRaises(ValidationError):
            self.f.service.register_program_version(
                "p-bad", "空节目", [], 30, actor_id=self.f.coord)


class 跨日演出与缓冲测试(unittest.TestCase):
    def setUp(self):
        self.f = build_festival()
        self.svc = self.f.service

    def _publish_双舞台跨夜计划(self):
        plan = self.svc.generate_plan(
            "音乐季开幕夜",
            [
                {"program_id": "p-opera", "stage_id": "yuyuan",
                 "starts_at": "2026-10-03T19:00:00+00:00",
                 "buffer_minutes": 20},
                {"program_id": "p-elec", "stage_id": "bund",
                 "starts_at": "2026-10-03T23:00:00+00:00",
                 "buffer_minutes": 30},
            ],
            self.f.coord)
        return self.svc.publish_plan(plan["plan_id"], self.f.coord)

    def test_跨日条目被识别且缓冲延伸到次日(self):
        published = self._publish_双舞台跨夜计划()
        self.assertTrue(published["spans_midnight"])
        bund = next(s for s in published["slots"] if s["stage_id"] == "bund")
        # 23:00 + 120 分钟演出 = 次日 01:00，再加 30 分钟缓冲 = 01:30
        self.assertEqual(bund["ends_at"], "2026-10-04T01:00:00+00:00")
        self.assertEqual(bund["buffer_end"], "2026-10-04T01:30:00+00:00")
        yuyuan = next(s for s in published["slots"] if s["stage_id"] == "yuyuan")
        self.assertEqual(yuyuan["buffer_end"], "2026-10-03T20:50:00+00:00")

    def test_模拟时钟跨越整个音乐季验证计划仍有效(self):
        published = self._publish_双舞台跨夜计划()
        # 时钟从 10-01 走到演出日、再走到散场之后
        self.f.clock.jump_to("2026-10-03T22:00:00+00:00")
        self.assertEqual(self.svc.get_plan(published["plan_id"])["state"],
                         "published")
        self.f.clock.advance(hours=4)  # 次日 02:00，外滩散场后
        self.assertTrue(
            self.svc.latest_effective_plan(published["origin_plan_id"])
            ["plan_id"], published["plan_id"])

    def test_同舞台相邻节目落入缓冲被拒(self):
        self._publish_双舞台跨夜计划()
        clash = self.svc.generate_plan(
            "豫园加场",
            [{"program_id": "p-folk", "stage_id": "yuyuan",
              "starts_at": "2026-10-03T20:40:00+00:00",
              "buffer_minutes": 10}],
            self.f.coord)
        with self.assertRaises(ConflictError):
            self.svc.publish_plan(clash["plan_id"], self.f.coord)

    def test_缓冲结束之后允许发布(self):
        self._publish_双舞台跨夜计划()
        ok = self.svc.generate_plan(
            "豫园深夜场",
            [{"program_id": "p-folk", "stage_id": "yuyuan",
              "starts_at": "2026-10-03T20:50:00+00:00",
              "buffer_minutes": 0}],
            self.f.coord)
        published = self.svc.publish_plan(ok["plan_id"], self.f.coord)
        self.assertEqual(published["state"], "published")

    def test_同一艺人跨舞台赶场被缓冲拦截(self):
        # 梅剧团先在豫园 19:00~20:30（缓冲 60 分钟），已发布
        first = self.svc.generate_plan(
            "豫园场",
            [{"program_id": "p-opera", "stage_id": "yuyuan",
              "starts_at": "2026-10-05T19:00:00+00:00",
              "buffer_minutes": 60}],
            self.f.coord)
        self.svc.publish_plan(first["plan_id"], self.f.coord)
        # 又被排到外滩 21:00（与艺人锁定窗口 19:00~21:30 重叠），发布被拒
        second = self.svc.generate_plan(
            "外滩场",
            [{"program_id": "p-opera", "stage_id": "bund",
              "starts_at": "2026-10-05T21:00:00+00:00",
              "buffer_minutes": 10}],
            self.f.coord)
        with self.assertRaises(ConflictError):
            self.svc.publish_plan(second["plan_id"], self.f.coord)

    def test_同一计划内赶场在生成阶段即被拦截(self):
        with self.assertRaises(ConflictError):
            self.svc.generate_plan(
                "跨场冲突计划",
                [
                    {"program_id": "p-opera", "stage_id": "yuyuan",
                     "starts_at": "2026-10-05T19:00:00+00:00",
                     "buffer_minutes": 60},
                    {"program_id": "p-opera", "stage_id": "bund",
                     "starts_at": "2026-10-05T21:00:00+00:00",
                     "buffer_minutes": 10},
                ],
                self.f.coord)


class 发布锁定与冻结测试(unittest.TestCase):
    def setUp(self):
        self.f = build_festival()
        self.svc = self.f.service
        self.plan = self.svc.generate_plan(
            "音乐季开幕夜",
            [{"program_id": "p-opera", "stage_id": "yuyuan",
              "starts_at": "2026-10-03T19:00:00+00:00",
              "buffer_minutes": 20}],
            self.f.coord)
        self.published = self.svc.publish_plan(self.plan["plan_id"],
                                               self.f.coord)

    def test_重复发布被拒绝(self):
        with self.assertRaises(ConflictError):
            self.svc.publish_plan(self.plan["plan_id"], self.f.coord)

    def test_发布后资源已锁定并冻结节目版本(self):
        from heritage_stage.store import Store
        self.assertGreaterEqual(
            self.svc.store.count_locks(self.plan["plan_id"]), 2)  # 舞台 + 艺人
        slot = self.published["slots"][0]
        self.assertEqual(slot["program_version"], 1)
        # 即便之后登记了 v2，已公开的节目单仍然冻结在 v1
        self.svc.register_program_version(
            "p-opera", "牡丹亭·新版", ["a-opera"], 80, "南北戏曲",
            self.f.coord)
        frozen = self.svc.get_plan(self.plan["plan_id"])
        self.assertEqual(frozen["slots"][0]["program_version"], 1)
        self.assertEqual(frozen["state"], "published")

    def test_被取代的计划不能再次发布(self):
        # 先制造一次封闭并走完替代方案，使原计划变为 superseded
        inc = self.svc.report_stage_closed(
            "yuyuan", self.plan["plan_id"], self.f.coord,
            starts_at="2026-10-03T18:00:00+00:00",
            ends_at="2026-10-03T23:00:00+00:00")
        ctg = self.svc.create_contingency(
            inc["incident_id"], self.f.coord,
            [{"program_id": "p-opera", "stage_id": "bund",
              "starts_at": "2026-10-04T19:00:00+00:00",
              "buffer_minutes": 15}],
            required_levels=1)
        self.svc.decide_contingency(ctg["contingency_id"],
                                    self.f.boss1, "approve")
        with self.assertRaises(InvalidTransitionError):
            self.svc.publish_plan(self.plan["plan_id"], self.f.coord)

    def test_草稿自动铺排也带缓冲(self):
        draft = self.svc.generate_plan(
            "民乐连台",
            [
                {"program_id": "p-folk", "stage_id": "bund",
                 "buffer_minutes": 15},
                {"program_id": "p-folk", "stage_id": "bund",
                 "buffer_minutes": 15},
            ],
            self.f.coord, plan_start="2026-10-10T14:00:00+00:00")
        slots = draft["slots"]
        self.assertEqual(slots[0]["starts_at"], "2026-10-10T14:00:00+00:00")
        # 第二场开场 = 14:00 + 75 分钟 + 15 分钟缓冲
        self.assertEqual(slots[1]["starts_at"], "2026-10-10T15:30:00+00:00")


if __name__ == "__main__":
    unittest.main()

"""豫园、外滩双场同时变更后的最终计划收敛测试。

两场事件（豫园封闭、电子艺人退出）几乎同时发生，各自的替代方案
由不同审批链先后通过。无论终审顺序如何，最终计划都必须同时满足：

* 不使用封闭时段的豫园舞台；
* 不含已退出的电子艺人；
* 保留未受影响条目，整场顺序不失真；
* 版本沿同一条替代链递增，原计划全部留存。
"""
import unittest

from tests._fixtures import build_festival


def 发布v1(f):
    plan = f.service.generate_plan(
        "音乐季正场",
        [
            {"program_id": "p-opera", "stage_id": "yuyuan",
             "starts_at": "2026-10-12T19:00:00+00:00",
             "buffer_minutes": 20},
            {"program_id": "p-elec", "stage_id": "bund",
             "starts_at": "2026-10-12T20:00:00+00:00",
             "buffer_minutes": 30},
        ],
        f.coord)
    return f.service.publish_plan(plan["plan_id"], f.coord)


def 双事件场景(f):
    v1 = 发布v1(f)
    closed = f.service.report_stage_closed(
        "yuyuan", v1["plan_id"], f.coord,
        starts_at="2026-10-12T18:00:00+00:00",
        ends_at="2026-10-12T23:00:00+00:00")
    withdrew = f.service.report_artist_withdrew(
        "a-dj", v1["plan_id"], f.coord,
        starts_at="2026-10-12T18:00:00+00:00")
    return v1, closed, withdrew


def 两个待审批方案(f, closed, withdrew, levels=2):
    # 方案 A：豫园昆曲挪到次日外滩
    ctg_a = f.service.create_contingency(
        closed["incident_id"], f.coord,
        [{"program_id": "p-opera", "stage_id": "bund",
          "starts_at": "2026-10-13T19:00:00+00:00",
          "buffer_minutes": 15}],
        required_levels=levels, note="豫园封场，昆曲改次日外滩")
    # 方案 B：电音艺人退出，换成江南丝竹
    ctg_b = f.service.create_contingency(
        withdrew["incident_id"], f.coord,
        [{"program_id": "p-folk", "stage_id": "bund",
          "starts_at": "2026-10-12T20:00:00+00:00",
          "buffer_minutes": 30}],
        required_levels=levels, note="电子组合退出，丝竹补位")
    return ctg_a, ctg_b


def 审批通过(f, ctg):
    approvers = (f.boss1, f.boss2)
    for level in range(1, ctg["required_levels"] + 1):
        f.clock.advance(hours=1)
        ctg = f.service.decide_contingency(
            ctg["contingency_id"], approvers[level - 1], "approve",
            f"第{level}级同意")
    return ctg


def 最终签名(service, plan_id):
    plan = service.get_plan(plan_id)
    return [
        (s["title"], s["stage_id"], s["starts_at"], s["buffer_end"],
         tuple(sorted(s["artist_ids"])))
        for s in plan["slots"]
    ]


class 双场同时变更测试(unittest.TestCase):
    def test_先批豫园方案再批艺人方案(self):
        f = build_festival()
        v1, closed, withdrew = 双事件场景(f)
        ctg_a, ctg_b = 两个待审批方案(f, closed, withdrew)

        done_a = 审批通过(f, ctg_a)
        v2 = f.service.get_plan(done_a["new_plan_id"])
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["supersedes"], v1["plan_id"])

        f.clock.advance(hours=5)
        done_b = 审批通过(f, ctg_b)
        v3 = f.service.get_plan(done_b["new_plan_id"])

        self.断言最终计划(f, v1, v2, v3)

    def test_先批艺人方案再批豫园方案(self):
        f = build_festival()
        v1, closed, withdrew = 双事件场景(f)
        ctg_a, ctg_b = 两个待审批方案(f, closed, withdrew)

        done_b = 审批通过(f, ctg_b)
        v2 = f.service.get_plan(done_b["new_plan_id"])
        self.assertEqual(v2["version"], 2)
        # v2 中昆曲仍在豫园（该事件尚未处理），电音已换成丝竹
        titles_v2 = {s["title"]: s["stage_id"] for s in v2["slots"]}
        self.assertEqual(titles_v2["牡丹亭"], "yuyuan")
        self.assertEqual(titles_v2["江南丝竹专场"], "bund")

        f.clock.advance(hours=5)
        done_a = 审批通过(f, ctg_a)
        v3 = f.service.get_plan(done_a["new_plan_id"])

        self.断言最终计划(f, v1, v2, v3)

    def test_两种审批顺序收敛到同一份节目单(self):
        f1 = build_festival()
        _, ca, wa = 双事件场景(f1)
        a1, b1 = 两个待审批方案(f1, ca, wa, levels=1)
        final_a_first = 审批通过(f1, a1)
        final_a_first = 审批通过(f1, b1)
        sig_a_first = 最终签名(f1.service, final_a_first["new_plan_id"])

        f2 = build_festival()
        _, cb, wb = 双事件场景(f2)
        a2, b2 = 两个待审批方案(f2, cb, wb, levels=1)
        final_b_first = 审批通过(f2, b2)
        final_b_first = 审批通过(f2, a2)
        sig_b_first = 最终签名(f2.service, final_b_first["new_plan_id"])

        self.assertEqual(sig_a_first, sig_b_first)
        # 最终两场都在外滩：丝竹补 12 日晚，昆曲改 13 日晚
        self.assertEqual(
            sorted((title, stage) for title, stage, *_ in sig_a_first),
            [("江南丝竹专场", "bund"), ("牡丹亭", "bund")])
        self.assertTrue(
            all("a-dj" not in row[4] for row in sig_a_first))

    def 断言最终计划(self, f, v1, v2, v3):
        # v1/v2 字典是各自版本刚生成时的快照，终态需从库里重读
        v1 = f.service.get_plan(v1["plan_id"])
        v2 = f.service.get_plan(v2["plan_id"])
        # 版本链：v1 superseded → v2 superseded → v3 published
        self.assertEqual(v3["version"], 3)
        self.assertEqual(v3["state"], "published")
        self.assertEqual(v3["supersedes"], v2["plan_id"])
        self.assertEqual(v3["origin_plan_id"], v1["origin_plan_id"])
        self.assertEqual(v2["state"], "superseded")
        self.assertEqual(v1["state"], "superseded")

        titles = {s["title"]: s for s in v3["slots"]}
        self.assertEqual(set(titles), {"牡丹亭", "江南丝竹专场"})
        # 豫园条目已彻底移出，电子艺人不再出现
        self.assertTrue(all(s["stage_id"] != "yuyuan" for s in v3["slots"]))
        self.assertTrue(all("a-dj" not in s["artist_ids"]
                            for s in v3["slots"]))
        # 时间顺序：12 日丝竹晚场，13 日昆曲晚场，互不重叠
        opera, folk = titles["牡丹亭"], titles["江南丝竹专场"]
        self.assertEqual(opera["starts_at"], "2026-10-13T19:00:00+00:00")
        self.assertEqual(folk["starts_at"], "2026-10-12T20:00:00+00:00")
        self.assertTrue(opera["is_replacement"] and folk["is_replacement"])
        # 原计划与中间版本完整保留
        self.assertEqual(len(v1["slots"]), 2)
        self.assertEqual(len(v2["slots"]), 2)

        # 重开豫园后，最近有效版本仍为 v3（不是退回 v1）
        f.service.reopen_stage("yuyuan", f.coord)
        resumed = f.service.latest_effective_plan(v3["origin_plan_id"])
        self.assertEqual(resumed["plan_id"], v3["plan_id"])


if __name__ == "__main__":
    unittest.main()

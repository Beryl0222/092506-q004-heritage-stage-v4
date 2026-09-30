"""测试共用夹具：搭建豫园 / 外滩音乐季的最小数据集。"""
from __future__ import annotations

from dataclasses import dataclass

from heritage_stage.domain import FakeClock
from heritage_stage.service import Service
from heritage_stage.store import Store


@dataclass
class Festival:
    service: Service
    clock: FakeClock
    coord: str = "coord"
    boss1: str = "boss1"
    boss2: str = "boss2"
    viewer: str = "viewer"
    opera_artist: str = "a-opera"
    dj: str = "a-dj"
    folk_artist: str = "a-folk"
    yuyuan: str = "yuyuan"
    bund: str = "bund"
    opera: str = "p-opera"
    electronic: str = "p-elec"
    folk: str = "p-folk"


def build_festival(start: str = "2026-10-01T08:00:00+00:00") -> Festival:
    clock = FakeClock(start)
    service = Service(Store(), clock)

    service.register_user("coord", "演出统筹", ["coordinator"])
    service.register_user("boss1", "舞台主管", ["approver"])
    service.register_user("boss2", "音乐季总监", ["approver"])
    service.register_user("viewer", "只读者", ["viewer"])

    service.register_artist("a-opera", "梅剧团", "南北戏曲")
    service.register_artist("a-dj", "浦江电子组合", "电子音乐")
    service.register_artist("a-folk", "江南丝社", "民乐")

    service.register_stage("yuyuan", "豫园舞台", "豫园")
    service.register_stage("bund", "外滩舞台", "外滩")

    service.register_program_version(
        "p-opera", "牡丹亭", ["a-opera"], 90, "南北戏曲", "coord")
    service.register_program_version(
        "p-elec", "浦江电音之夜", ["a-dj"], 120, "电子音乐", "coord")
    service.register_program_version(
        "p-folk", "江南丝竹专场", ["a-folk"], 75, "民乐", "coord")

    return Festival(service=service, clock=clock)

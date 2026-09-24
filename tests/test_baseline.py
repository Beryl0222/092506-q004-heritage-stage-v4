import json
import unittest
from heritage_stage.api import handle
from heritage_stage.service import Service
from heritage_stage.store import Store


class 基线行为测试(unittest.TestCase):
    def test_health(self):
        result = json.loads(handle(json.dumps({"action": "health"}), Service(Store())))
        self.assertEqual(result["status"], "ok")

    def test_register_and_find(self):
        service = Service(Store())
        created = service.register("r-1", "owner-1")
        self.assertEqual(created["state"], "draft")
        self.assertEqual(service.find("r-1")["owner_id"], "owner-1")


if __name__ == "__main__":
    unittest.main()

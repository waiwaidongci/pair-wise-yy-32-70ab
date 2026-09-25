import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store


class ReleaseReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = BatchService(Store(Path(self.tmp.name) / "r.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def rev(self, bid): return self.s.batch_detail(bid)["batch"]["revision"]

    def new_batch(self, no):
        return self.s.create_batch("operator", "operator", self.f1, no, "药片", "2026-01-01", "2028-01-01")

    def pass_test(self, bid):
        self.s.record_test("lab", "lab", self.f1, bid, "含量", 100, 95, 105, self.rev(bid))

    def test_precheck_lists_blockers_and_warnings(self):
        b = self.new_batch("B-100")
        self.s.add_deviation("operator", "operator", self.f1, b["id"], "critical", "无菌异常", self.future, self.rev(b["id"]))
        self.s.record_test("lab", "lab", self.f1, b["id"], "含量", 80, 95, 105, self.rev(b["id"]))
        self.s.plan_rework("operator", "operator", self.f1, b["id"], "重新包装", self.rev(b["id"]))
        self.s.record_supplier_change("operator", "operator", self.f1, b["id"], "供应商A", "变更产地", "原料产地变更", self.rev(b["id"]))
        pre = self.s.batch_detail(b["id"])["precheck"]
        self.assertEqual("blocked", pre["verdict"])
        self.assertTrue({"critical_deviation", "test_failed", "rework_open"} <= {x["code"] for x in pre["blockers"]})
        warn = {x["code"] for x in pre["warnings"]}
        self.assertIn("supplier_change", warn); self.assertIn("no_stability", warn)

    def test_conditional_flow_with_exception(self):
        b = self.new_batch("B-101")
        self.pass_test(b["id"])
        dev = self.s.add_deviation("operator", "operator", self.f1, b["id"], "minor", "标签轻微歪斜", self.future, self.rev(b["id"]))
        self.s.approve_exception("qa", "qa", dev["id"], "客户确认接受", self.future, self.rev(b["id"]))
        self.assertEqual("conditional", self.s.batch_detail(b["id"])["precheck"]["verdict"])
        with self.assertRaises(ApiError):
            self.s.submit_review("qa", "qa", b["id"], "release", "想正式放行", self.rev(b["id"]))
        rv = self.s.submit_review("qa", "qa", b["id"], "conditional", "例外有效，可有条件放行", self.rev(b["id"]))
        self.assertEqual(self.rev(b["id"]), rv["review"]["revision"])
        out = self.s.decide("qa", "qa", b["id"], "conditional", "按复核结论有条件放行", self.rev(b["id"]), exception_code="EX-1")
        self.assertEqual("conditional", out["batch"]["state"])
        self.assertEqual(rv["review"]["id"], self.s.batch_detail(b["id"])["decisions"][0]["review_id"])

    def test_decision_requires_valid_review_and_changes_invalidate(self):
        b = self.new_batch("B-102")
        self.pass_test(b["id"])
        with self.assertRaises(ApiError) as no_review:
            self.s.decide("qa", "qa", b["id"], "release", "未复核直接放行", self.rev(b["id"]))
        self.assertIn("复核", no_review.exception.message)
        self.s.submit_review("qa", "qa", b["id"], "release", "预检通过", self.rev(b["id"]))
        self.s.record_stability("lab", "lab", self.f1, b["id"], "25C/60RH", "3m", 99, 105, self.rev(b["id"]))
        self.assertFalse(self.s.batch_detail(b["id"])["current_review"]["valid"])
        with self.assertRaises(ApiError) as stale:
            self.s.decide("qa", "qa", b["id"], "release", "引用已失效复核", self.rev(b["id"]))
        self.assertIn("复核", stale.exception.message)
        self.s.submit_review("qa", "qa", b["id"], "release", "重新预检后复核", self.rev(b["id"]))
        out = self.s.decide("qa", "qa", b["id"], "release", "依据最新复核放行", self.rev(b["id"]))
        self.assertEqual("released", out["batch"]["state"])

    def test_each_data_change_invalidates_review(self):
        mutators = [
            lambda bid: self.s.add_deviation("operator", "operator", self.f1, bid, "minor", "新偏差", self.future, self.rev(bid)),
            lambda bid: self.s.record_test("lab", "lab", self.f1, bid, "水分", 1, 0, 2, self.rev(bid)),
            lambda bid: self.s.plan_rework("operator", "operator", self.f1, bid, "返工", self.rev(bid)),
            lambda bid: self.s.record_stability("lab", "lab", self.f1, bid, "25C", "3m", 1, 2, self.rev(bid)),
        ]
        for i, mutate in enumerate(mutators):
            b = self.new_batch(f"B-2{i}0")
            self.pass_test(b["id"])
            self.s.submit_review("qa", "qa", b["id"], "release", "复核", self.rev(b["id"]))
            self.assertTrue(self.s.batch_detail(b["id"])["current_review"]["valid"])
            mutate(b["id"])
            self.assertFalse(self.s.batch_detail(b["id"])["current_review"]["valid"])

    def test_review_role_revision_and_opinion_guards(self):
        b = self.new_batch("B-103")
        self.pass_test(b["id"])
        with self.assertRaises(ApiError):
            self.s.submit_review("operator", "operator", b["id"], "release", "越权复核", self.rev(b["id"]))
        with self.assertRaises(ApiError):
            self.s.submit_review("qa", "qa", b["id"], "release", "旧版本复核", 1)
        with self.assertRaises(ApiError):
            self.s.submit_review("qa", "qa", b["id"], "release", "  ", self.rev(b["id"]))

    def test_blocked_batch_only_allows_hold(self):
        b = self.new_batch("B-104")
        self.s.add_deviation("operator", "operator", self.f1, b["id"], "critical", "无菌异常", self.future, self.rev(b["id"]))
        with self.assertRaises(ApiError):
            self.s.submit_review("qa", "qa", b["id"], "release", "强行放行", self.rev(b["id"]))
        rv = self.s.submit_review("qa", "qa", b["id"], "hold", "存在阻断，暂缓", self.rev(b["id"]))
        self.assertEqual("hold", rv["review"]["conclusion"])


if __name__ == "__main__": unittest.main()

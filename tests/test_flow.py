import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store


class BatchFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = BatchService(Store(Path(self.tmp.name) / "b.db"))
        self.f1 = self.s.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.f2 = self.s.register_factory("qa", "qa", "F2", "二厂", "CN")["id"]
        self.future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def revision(self, batch_id):
        return self.s.batch_detail(batch_id)["batch"]["revision"]

    def test_full_investigation_retest_rework_and_release(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-1", "药片", "2026-01-01", "2028-01-01")
        dev = self.s.add_deviation("operator", "operator", self.f1, batch["id"], "minor", "装量轻微偏离", self.future, batch["revision"])
        failed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 89, 95, 105, self.revision(batch["id"]))
        self.assertFalse(failed["passed"])
        passed = self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, self.revision(batch["id"]))
        self.assertTrue(passed["passed"])
        self.s.close_deviation("qa", "qa", dev["id"], "调整灌装参数", self.revision(batch["id"]))
        rw = self.s.plan_rework("operator", "operator", self.f1, batch["id"], "返工包装", self.revision(batch["id"]))
        self.s.complete_rework("operator", "operator", self.f1, rw["id"], self.revision(batch["id"]))
        self.s.record_stability("lab", "lab", self.f1, batch["id"], "25C/60RH", "3m", 99, 105, self.revision(batch["id"]))
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual("release", detail["precheck"]["recommendation"])
        self.assertFalse(detail["precheck"]["blockers"])
        review = self.s.submit_review("qa", "qa", batch["id"], "release", "复核通过，同意放行")
        result = self.s.decide("qa", "qa", batch["id"], "release", "调查关闭，复测合格", self.revision(batch["id"]), review_id=review["id"])
        self.assertEqual("released", result["batch"]["state"])
        self.assertEqual(review["id"], result["decision"]["review_id"])
        self.assertEqual(1, len(self.s.batch_detail(batch["id"])["decisions"]))

    def test_critical_block_conditional_exception_and_factory_conflict(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-2", "胶囊", "2026-02-01", "2028-02-01")
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, batch["revision"])
        crit = self.s.add_deviation("inspector", "inspector", self.f1, batch["id"], "critical", "无菌数据异常", self.future, self.revision(batch["id"]))
        precheck = self.s.batch_detail(batch["id"])["precheck"]
        self.assertEqual("blocked", precheck["recommendation"])
        self.assertTrue(any("关键偏差" in item["message"] for item in precheck["blockers"]))
        with self.assertRaises(ApiError) as blocked:
            self.s.submit_review("qa", "qa", batch["id"], "release", "尝试放行")
        self.assertEqual(409, blocked.exception.status)
        with self.assertRaises(ApiError):
            self.s.decide("qa", "qa", batch["id"], "release", "尝试放行", self.revision(batch["id"]))
        with self.assertRaises(ApiError):
            self.s.approve_exception("qa", "qa", crit["id"], "暂时接受", self.future, self.revision(batch["id"]))
        with self.assertRaises(ApiError):
            self.s.record_test("lab", "lab", self.f2, batch["id"], "水分", 1, 0, 2, self.revision(batch["id"]))
        with self.assertRaises(ApiError) as stale:
            self.s.record_test("lab", "lab", self.f1, batch["id"], "水分", 1, 0, 2, 1)
        self.assertEqual(409, stale.exception.status)

    def test_review_saved_with_version_and_invalidated_by_changes(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-3", "注射液", "2026-03-01", "2028-03-01")
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, batch["revision"])
        with self.assertRaises(ApiError):
            self.s.submit_review("op", "operator", batch["id"], "release", "越权复核")
        review = self.s.submit_review("qa", "qa", batch["id"], "release", "资料齐全，同意放行")
        self.assertTrue(review["valid"])
        self.assertEqual(self.revision(batch["id"]), review["revision"])
        # 放行决定必须引用复核
        with self.assertRaises(ApiError) as missing:
            self.s.decide("qa", "qa", batch["id"], "release", "未引用复核", self.revision(batch["id"]))
        self.assertIn("复核", missing.exception.message)
        # 修改检验资料后旧复核失效，放行被拒绝，需重新预检复核
        self.s.record_test("lab", "lab", self.f1, batch["id"], "水分", 1.0, 0, 2, self.revision(batch["id"]))
        detail = self.s.batch_detail(batch["id"])
        self.assertFalse(detail["reviews"][0]["valid"])
        with self.assertRaises(ApiError) as stale:
            self.s.decide("qa", "qa", batch["id"], "release", "引用失效复核", self.revision(batch["id"]), review_id=review["id"])
        self.assertIn("失效", stale.exception.message)
        review2 = self.s.submit_review("qa", "qa", batch["id"], "release", "重新预检后复核")
        result = self.s.decide("qa", "qa", batch["id"], "release", "放行", self.revision(batch["id"]), review_id=review2["id"])
        self.assertEqual("released", result["batch"]["state"])
        self.assertEqual(review2["id"], result["decision"]["review_id"])

    def test_conditional_release_needs_exception_and_matching_review(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-4", "颗粒", "2026-04-01", "2028-04-01")
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 98, 95, 105, batch["revision"])
        dev = self.s.add_deviation("operator", "operator", self.f1, batch["id"], "minor", "装量偏差", self.future, self.revision(batch["id"]))
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual("blocked", detail["precheck"]["recommendation"])
        with self.assertRaises(ApiError):
            self.s.submit_review("qa", "qa", batch["id"], "conditional", "例外尚未批准")
        self.s.approve_exception("qa", "qa", dev["id"], "评估后风险可接受", self.future, self.revision(batch["id"]))
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual("conditional", detail["precheck"]["recommendation"])
        self.assertTrue(detail["precheck"]["warnings"])
        review = self.s.submit_review("qa", "qa", batch["id"], "conditional", "同意有条件放行")
        with self.assertRaises(ApiError) as mismatch:
            self.s.decide("qa", "qa", batch["id"], "release", "结论不符", self.revision(batch["id"]), review_id=review["id"])
        self.assertIn("一致", mismatch.exception.message)
        result = self.s.decide("qa", "qa", batch["id"], "conditional", "按例外放行", self.revision(batch["id"]), exception_code="EX-001", review_id=review["id"])
        self.assertEqual("conditional", result["batch"]["state"])

    def test_planned_rework_and_failed_stability_block_precheck(self):
        batch = self.s.create_batch("operator", "operator", self.f1, "B-5", "软膏", "2026-05-01", "2028-05-01")
        self.s.record_test("lab", "lab", self.f1, batch["id"], "含量", 100, 95, 105, batch["revision"])
        self.s.plan_rework("operator", "operator", self.f1, batch["id"], "重新混合", self.revision(batch["id"]))
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual("blocked", detail["precheck"]["recommendation"])
        self.assertTrue(any("返工" in item["message"] for item in detail["precheck"]["blockers"]))
        rw = detail["rework"][0]
        self.s.complete_rework("operator", "operator", self.f1, rw["id"], self.revision(batch["id"]))
        self.s.record_stability("lab", "lab", self.f1, batch["id"], "30C/65RH", "6m", 120, 105, self.revision(batch["id"]))
        detail = self.s.batch_detail(batch["id"])
        self.assertEqual("blocked", detail["precheck"]["recommendation"])
        self.assertTrue(any("稳定性" in item["message"] for item in detail["precheck"]["blockers"]))


if __name__ == "__main__": unittest.main()

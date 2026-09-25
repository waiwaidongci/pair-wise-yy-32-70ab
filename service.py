"""业务编排层：角色权限、工厂隔离、批次版本控制、放行预检调用、QA 复核与放行决定。"""
from __future__ import annotations

import json
import sqlite3

from rules import RANK, evaluate_release
from store import Store, after_now, j, now


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message); self.status, self.message = status, message


class BatchService:
    def __init__(self, store: Store): self.store, self.conn = store, store.conn

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor: raise ApiError(401, "缺少身份")
        if role not in allowed: raise ApiError(403, "角色无权执行此操作")
        return actor

    def _row(self, table: str, identity: int) -> sqlite3.Row:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        if not row: raise ApiError(404, "对象不存在")
        return row

    def _factory_check(self, actor: str, factory_id: int, batch: sqlite3.Row | None = None) -> None:
        factory = self.conn.execute("SELECT * FROM factories WHERE id=?", (factory_id,)).fetchone()
        if not factory: raise ApiError(404, "工厂不存在")
        if batch is not None and int(batch["factory_id"]) != int(factory_id):
            raise ApiError(403, "不能修改其他工厂的批次")

    def register_factory(self, actor: str | None, role: str | None, code: str, name: str, country: str) -> dict:
        actor = self._actor(actor, role, {"qa"})
        if not code or not name: raise ApiError(400, "工厂代号和名称不能为空")
        try:
            with self.conn:
                cur = self.conn.execute("INSERT INTO factories(code,name,country) VALUES(?,?,?)", (code, name, country))
                self.store.audit(actor, "factory.register", "factory", cur.lastrowid, {"code": code})
        except sqlite3.IntegrityError as exc: raise ApiError(409, "工厂代号已存在") from exc
        return {"id": cur.lastrowid, "code": code, "name": name, "country": country}

    def create_batch(self, actor: str | None, role: str | None, factory_id: int, batch_no: str, product: str, mfg_date: str, expiry_date: str) -> dict:
        actor = self._actor(actor, role, {"operator"})
        self._factory_check(actor, factory_id)
        if not batch_no.strip() or not product.strip() or expiry_date <= mfg_date: raise ApiError(400, "批号、产品或有效期不合法")
        stamp = now()
        try:
            with self.conn:
                cur = self.conn.execute("""INSERT INTO batches(factory_id,batch_no,product,mfg_date,expiry_date,state,created_by,created_at,updated_at)
                                         VALUES(?,?,?,?,?, 'manufactured',?,?,?)""",
                                        (factory_id, batch_no, product, mfg_date, expiry_date, actor, stamp, stamp))
                self.store.audit(actor, "batch.create", "batch", cur.lastrowid, {"factory_id": factory_id, "batch_no": batch_no})
        except sqlite3.IntegrityError as exc: raise ApiError(409, "该工厂批号已存在") from exc
        return self._batch_dict(self._row("batches", cur.lastrowid))

    def add_deviation(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, severity: str, title: str, due_at: str | None, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator", "inspector"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        if severity not in {"critical", "minor"} or not title.strip(): raise ApiError(400, "偏差等级或描述不合法")
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "已终态批次不能新增偏差")
        with self.conn:
            cur = self.conn.execute("""INSERT INTO deviations(batch_id,severity,title,due_at,status,created_by,created_at)
                                     VALUES(?,?,?,?,'open',?,?)""", (batch_id, severity, title, due_at, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            self.store.audit(actor, "deviation.open", "deviation", cur.lastrowid, {"batch_id": batch_id, "severity": severity})
        return self._deviation_dict(self._row("deviations", cur.lastrowid))

    def close_deviation(self, actor: str | None, role: str | None, deviation_id: int, corrective_action: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"qa"})
        deviation = self._row("deviations", deviation_id); batch = self._row("batches", deviation["batch_id"])
        if deviation["status"] != "open": raise ApiError(409, "偏差已经关闭")
        if not corrective_action.strip(): raise ApiError(400, "必须填写纠正措施")
        with self.conn:
            self.conn.execute("UPDATE deviations SET status='closed',corrective_action=?,closed_by=?,closed_at=? WHERE id=? AND status='open'",
                              (corrective_action, actor, now(), deviation_id))
            self._advance_batch(batch["id"], expected_revision, "investigation")
            self.store.audit(actor, "deviation.close", "deviation", deviation_id, {"batch_id": batch["id"], "corrective_action": corrective_action})
        return self._deviation_dict(self._row("deviations", deviation_id))

    def approve_exception(self, actor: str | None, role: str | None, deviation_id: int, reason: str, until: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"qa"})
        deviation = self._row("deviations", deviation_id); batch = self._row("batches", deviation["batch_id"])
        if deviation["severity"] == "critical": raise ApiError(409, "关键偏差不允许例外批准")
        if deviation["status"] != "open" or not reason.strip() or not after_now(until): raise ApiError(400, "例外原因或有效期不合法")
        with self.conn:
            self.conn.execute("UPDATE deviations SET exception_reason=?,exception_until=?,exception_approved_by=? WHERE id=?", (reason, until, actor, deviation_id))
            self._advance_batch(batch["id"], expected_revision, batch["state"])
            self.store.audit(actor, "deviation.exception", "deviation", deviation_id, {"batch_id": batch["id"], "reason": reason, "until": until})
        return self._deviation_dict(self._row("deviations", deviation_id))

    def record_test(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, test_type: str, result: float, spec_min: float, spec_max: float, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"lab"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        if not test_type.strip() or spec_min > spec_max: raise ApiError(400, "检验项目或标准不合法")
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "终态批次不能补录检验")
        round_no = self.conn.execute("SELECT COALESCE(MAX(round),0)+1 FROM tests WHERE batch_id=? AND test_type=?", (batch_id, test_type)).fetchone()[0]
        passed = int(spec_min <= result <= spec_max)
        with self.conn:
            cur = self.conn.execute("""INSERT INTO tests(batch_id,test_type,result,spec_min,spec_max,passed,round,recorded_by,created_at)
                                     VALUES(?,?,?,?,?,?,?,?,?)""", (batch_id, test_type, result, spec_min, spec_max, passed, round_no, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation" if (batch["state"] == "awaiting_resample" or not passed) else batch["state"])
            self.store.audit(actor, "test.record", "batch", batch_id, {"test_type": test_type, "result": result, "passed": bool(passed), "round": round_no})
        return self._test_dict(self._row("tests", cur.lastrowid))

    def plan_rework(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, description: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "终态批次不能返工")
        with self.conn:
            cur = self.conn.execute("INSERT INTO rework(batch_id,description,status,created_by,created_at) VALUES(?,?,'planned',?,?)", (batch_id, description, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            self.store.audit(actor, "rework.plan", "rework", cur.lastrowid, {"batch_id": batch_id, "description": description})
        return dict(self._row("rework", cur.lastrowid))

    def complete_rework(self, actor: str | None, role: str | None, factory_id: int, rework_id: int, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator"})
        row = self._row("rework", rework_id); batch = self._row("batches", row["batch_id"]); self._factory_check(actor, factory_id, batch)
        if row["status"] != "planned": raise ApiError(409, "返工记录已经完成")
        with self.conn:
            self.conn.execute("UPDATE rework SET status='completed',completed_by=?,completed_at=? WHERE id=?", (actor, now(), rework_id))
            self._advance_batch(batch["id"], expected_revision, "investigation")
            self.store.audit(actor, "rework.complete", "rework", rework_id, {"batch_id": batch["id"]})
        return dict(self._row("rework", rework_id))

    def record_supplier_change(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, supplier: str, change_type: str, description: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator", "qa"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        with self.conn:
            cur = self.conn.execute("INSERT INTO supplier_changes(batch_id,supplier,change_type,description,recorded_by,created_at) VALUES(?,?,?,?,?,?)",
                                    (batch_id, supplier, change_type, description, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            self.store.audit(actor, "supplier_change.record", "batch", batch_id, {"supplier": supplier, "change_type": change_type})
        return dict(self._row("supplier_changes", cur.lastrowid))

    def record_stability(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, condition: str, timepoint: str, result: float, spec_limit: float, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"lab"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        passed = int(result <= spec_limit)
        with self.conn:
            cur = self.conn.execute("INSERT INTO stability(batch_id,condition,timepoint,result,spec_limit,passed,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (batch_id, condition, timepoint, result, spec_limit, passed, actor, now()))
            self._advance_batch(batch_id, expected_revision, batch["state"])
            self.store.audit(actor, "stability.record", "batch", batch_id, {"condition": condition, "timepoint": timepoint, "passed": bool(passed)})
        return dict(self._row("stability", cur.lastrowid))

    # ---- 放行预检与 QA 复核 -------------------------------------------------

    def _quality_rows(self, batch_id: int) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
        def rows(name: str) -> list[dict]:
            return [dict(row) for row in self.conn.execute(f"SELECT * FROM {name} WHERE batch_id=? ORDER BY id", (batch_id,))]
        return rows("deviations"), rows("tests"), rows("rework"), rows("supplier_changes"), rows("stability")

    def _precheck(self, batch: sqlite3.Row) -> dict:
        return evaluate_release(self._batch_dict(batch), *self._quality_rows(batch["id"]))

    def submit_review(self, actor: str | None, role: str | None, batch_id: int, conclusion: str, comment: str) -> dict:
        """QA 保存复核结论与意见，同时冻结当前批次版本和预检快照。不推进批次版本，
        否则复核自己会立即失效；批次资料一旦被其他接口改动，版本递增即令旧复核失效。"""
        actor = self._actor(actor, role, {"qa"})
        batch = self._row("batches", batch_id)
        if conclusion not in RANK: raise ApiError(400, "复核结论不合法")
        if not comment.strip(): raise ApiError(400, "必须填写复核意见")
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "终态批次无需复核")
        precheck = self._precheck(batch)
        if RANK[conclusion] > RANK[precheck["recommendation"]]:
            raise ApiError(409, "复核结论不能宽松于预检判断")
        with self.conn:
            cur = self.conn.execute("""INSERT INTO reviews(batch_id,revision,conclusion,comment,precheck_json,reviewed_by,created_at)
                                     VALUES(?,?,?,?,?,?,?)""",
                                    (batch_id, batch["revision"], conclusion, comment, j(precheck), actor, now()))
            self.store.audit(actor, "review.submit", "review", cur.lastrowid,
                             {"batch_id": batch_id, "revision": batch["revision"], "conclusion": conclusion})
        return self._review_dict(self._row("reviews", cur.lastrowid), batch["revision"])

    def decide(self, actor: str | None, role: str | None, batch_id: int, decision: str, rationale: str, expected_revision: int, exception_code: str = "", review_id: int | None = None) -> dict:
        actor = self._actor(actor, role, {"qa"})
        batch = self._row("batches", batch_id)
        if decision not in {"release", "reject", "conditional", "resample"}: raise ApiError(400, "放行决定不合法")
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "批次已经是终态")
        if int(expected_revision) != int(batch["revision"]): raise ApiError(409, "批次已被其他工厂或质量人员修改，请刷新版本")
        if not rationale.strip(): raise ApiError(400, "必须填写决定依据")
        # 正式放行 / 有条件放行必须引用与当前版本一致、结论相符的复核
        review: sqlite3.Row | None = None
        if decision in {"release", "conditional"}:
            if review_id is None: raise ApiError(400, "放行决定必须引用复核结论")
            review = self.conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if not review or int(review["batch_id"]) != int(batch_id): raise ApiError(404, "复核记录不存在")
            if int(review["revision"]) != int(batch["revision"]): raise ApiError(409, "复核已失效，请重新预检并复核")
            if review["conclusion"] != decision: raise ApiError(409, "放行决定必须与复核结论一致")
        deviations = self.conn.execute("SELECT * FROM deviations WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        open_deviations = [d for d in deviations if d["status"] == "open"]
        latest_tests: dict[str, sqlite3.Row] = {}
        for row in self.conn.execute("SELECT * FROM tests WHERE batch_id=? ORDER BY id", (batch_id,)):
            latest_tests[row["test_type"]] = row
        if decision in {"release", "conditional"} and not latest_tests:
            raise ApiError(409, "放行前至少需要一项检验结果")
        if decision in {"release", "conditional"} and any(not row["passed"] for row in latest_tests.values()):
            raise ApiError(409, "最新检验结果仍有不合格项")
        if decision in {"release", "conditional"} and self.conn.execute(
                "SELECT id FROM rework WHERE batch_id=? AND status='planned' LIMIT 1", (batch_id,)).fetchone():
            raise ApiError(409, "存在未完成的返工，不能放行")
        if decision in {"release", "conditional"} and self.conn.execute(
                "SELECT id FROM stability WHERE batch_id=? AND passed=0 LIMIT 1", (batch_id,)).fetchone():
            raise ApiError(409, "存在不合格的稳定性数据，不能放行")
        if decision == "resample":
            if batch["state"] == "conditional": raise ApiError(409, "有条件放行后不能直接改为再取样")
            new_state = "awaiting_resample"
        elif decision == "reject":
            new_state = "rejected"
        elif any(d["severity"] == "critical" for d in open_deviations):
            raise ApiError(409, "未关闭的关键偏差阻止放行")
        elif decision == "release" and open_deviations:
            raise ApiError(409, "仍有未关闭偏差，不能正式放行")
        elif decision == "conditional":
            for deviation in open_deviations:
                if not deviation["exception_reason"] or not after_now(deviation["exception_until"]):
                    raise ApiError(409, f"偏差 {deviation['id']} 没有有效例外批准")
            if not exception_code.strip(): raise ApiError(400, "有条件放行必须提供例外编号")
            new_state = "conditional"
        else:
            new_state = "released"
        with self.conn:
            cur = self.conn.execute("""INSERT INTO decisions(batch_id,revision,decision,rationale,exception_code,review_id,decided_by,created_at)
                                     VALUES(?,?,?,?,?,?,?,?)""",
                                    (batch_id, batch["revision"], decision, rationale, exception_code or None,
                                     review["id"] if review else None, actor, now()))
            updated = self.conn.execute("UPDATE batches SET state=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                                        (new_state, now(), batch_id, expected_revision))
            if updated.rowcount != 1: raise ApiError(409, "并发放行冲突")
            self.store.audit(actor, "batch.decision", "batch", batch_id,
                             {"decision": decision, "revision": batch["revision"], "state": new_state,
                              "exception_code": exception_code, "review_id": review["id"] if review else None})
        return {"decision": dict(self._row("decisions", cur.lastrowid)), "batch": self.batch_detail(batch_id)["batch"]}

    def _advance_batch(self, batch_id: int, expected_revision: int, next_state: str) -> None:
        batch = self._row("batches", batch_id)
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "终态批次不可修改")
        if int(expected_revision) != int(batch["revision"]): raise ApiError(409, "批次版本冲突")
        cur = self.conn.execute("UPDATE batches SET state=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                                (next_state, now(), batch_id, expected_revision))
        if cur.rowcount != 1: raise ApiError(409, "并发更新冲突")

    def batch_detail(self, batch_id: int) -> dict:
        batch_row = self._row("batches", batch_id)
        batch = self._batch_dict(batch_row)
        deviations, tests, rework, supplier_changes, stability = self._quality_rows(batch_id)
        reviews = [self._review_dict(row, batch["revision"])
                   for row in self.conn.execute("SELECT * FROM reviews WHERE batch_id=? ORDER BY id DESC", (batch_id,))]
        decisions = [dict(row) for row in self.conn.execute("SELECT * FROM decisions WHERE batch_id=? ORDER BY id", (batch_id,))]
        precheck = evaluate_release(batch, deviations, tests, rework, supplier_changes, stability)
        return {"batch": batch, "deviations": deviations, "tests": tests, "rework": rework,
                "supplier_changes": supplier_changes, "stability": stability,
                "precheck": precheck, "reviews": reviews, "decisions": decisions}

    def _batch_dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "factory_id": row["factory_id"], "batch_no": row["batch_no"], "product": row["product"],
                "mfg_date": row["mfg_date"], "expiry_date": row["expiry_date"], "state": row["state"], "revision": row["revision"]}

    @staticmethod
    def _deviation_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "severity": row["severity"], "title": row["title"], "due_at": row["due_at"],
                "status": row["status"], "corrective_action": row["corrective_action"], "exception_reason": row["exception_reason"],
                "exception_until": row["exception_until"], "exception_approved_by": row["exception_approved_by"]}

    @staticmethod
    def _test_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "test_type": row["test_type"], "result": row["result"],
                "spec_min": row["spec_min"], "spec_max": row["spec_max"], "passed": bool(row["passed"]), "round": row["round"]}

    @staticmethod
    def _review_dict(row: sqlite3.Row, current_revision: int) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "revision": row["revision"],
                "conclusion": row["conclusion"], "comment": row["comment"],
                "reviewed_by": row["reviewed_by"], "created_at": row["created_at"],
                # 版本与批次当前版本一致才有效；偏差/检验/返工/稳定性等任何改动都会推进版本
                "valid": int(row["revision"]) == int(current_revision),
                "precheck": json.loads(row["precheck_json"])}

    def state(self) -> dict:
        return {"factories": [dict(row) for row in self.conn.execute("SELECT * FROM factories ORDER BY id")],
                "batches": [self._batch_dict(row) for row in self.conn.execute("SELECT * FROM batches ORDER BY id DESC")],
                "audits": [dict(row) for row in self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 30")]}

    def seed(self) -> None:
        if self.conn.execute("SELECT id FROM factories LIMIT 1").fetchone():
            return
        factory = self.register_factory("qa-demo", "qa", "F-DEMO", "演示工厂", "CN")
        batch = self.create_batch("op-demo", "operator", factory["id"], "B-DEMO", "演示片剂", "2026-01-01", "2028-01-01")
        self.record_test("lab-demo", "lab", factory["id"], batch["id"], "含量", 99.5, 95.0, 105.0, batch["revision"])
        revision = self.batch_detail(batch["id"])["batch"]["revision"]
        self.add_deviation("op-demo", "operator", factory["id"], batch["id"], "minor", "外包装喷码偏移", None, revision)

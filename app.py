#!/usr/bin/env python3
"""药品生产偏差与批次放行服务 —— HTTP 入口。

分层职责：
- rules.py   放行预检规则（纯函数：阻断项、提醒项、放行判断）
- store.py   SQLite 存储、迁移与审计
- service.py 业务编排（权限、版本控制、QA 复核、放行决定）
- static/index.html 首页（选批次、看预检结果、提交复核与决定）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from service import ApiError, BatchService
from store import DB_PATH, Store

__all__ = ["ApiError", "BatchService", "Store"]


class Handler(BaseHTTPRequestHandler):
    service: BatchService

    def log_message(self, fmt: str, *args: object) -> None: sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))
    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        try: return json.loads(self.rfile.read(size)) if size else {}
        except json.JSONDecodeError as exc: raise ApiError(400, "JSON 请求体无效") from exc
    def _parts(self) -> list[str]: return [p for p in urlparse(self.path).path.strip("/").split("/") if p]

    def do_GET(self) -> None:
        try:
            p = self._parts()
            if p in (["health"], ["api", "health"]): out = {"status": "ok"}
            elif p == ["api", "state"]: out = self.service.state()
            elif len(p) == 3 and p[:2] == ["api", "batches"]: out = self.service.batch_detail(int(p[2]))
            elif not p:
                page = (Path(__file__).parent / "static" / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page); return
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except Exception as exc: self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            p, b = self._parts(), self._body(); actor, role = self.headers.get("X-Actor"), self.headers.get("X-Role")
            review_id = b.get("review_id")
            if p == ["api", "factories"]: out = self.service.register_factory(actor, role, b.get("code", ""), b.get("name", ""), b.get("country", ""))
            elif p == ["api", "batches"]: out = self.service.create_batch(actor, role, int(b.get("factory_id", 0)), b.get("batch_no", ""), b.get("product", ""), b.get("mfg_date", ""), b.get("expiry_date", ""))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "deviations": out = self.service.add_deviation(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("severity", ""), b.get("title", ""), b.get("due_at"), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "deviations"] and p[3] == "close": out = self.service.close_deviation(actor, role, int(p[2]), b.get("corrective_action", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "deviations"] and p[3] == "exception": out = self.service.approve_exception(actor, role, int(p[2]), b.get("reason", ""), b.get("until", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "tests": out = self.service.record_test(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("test_type", ""), float(b.get("result", 0)), float(b.get("spec_min", 0)), float(b.get("spec_max", 0)), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "rework": out = self.service.plan_rework(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("description", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "rework"] and p[3] == "complete": out = self.service.complete_rework(actor, role, int(b.get("factory_id", 0)), int(p[2]), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "supplier-changes": out = self.service.record_supplier_change(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("supplier", ""), b.get("change_type", ""), b.get("description", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "stability": out = self.service.record_stability(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("condition", ""), b.get("timepoint", ""), float(b.get("result", 0)), float(b.get("spec_limit", 0)), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "reviews": out = self.service.submit_review(actor, role, int(p[2]), b.get("conclusion", ""), b.get("comment", ""))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "decide": out = self.service.decide(actor, role, int(p[2]), b.get("decision", ""), b.get("rationale", ""), int(b.get("expected_revision", -1)), b.get("exception_code", ""), int(review_id) if review_id else None)
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc: self._send(400, {"error": str(exc)})
        except Exception as exc: self._send(500, {"error": str(exc)})


def run(port: int, db_path: str, seed: bool) -> None:
    store = Store(db_path); service = BatchService(store)
    if seed: service.seed()
    Handler.service = service
    print(f"batch release listening on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8214); parser.add_argument("--db", default=str(DB_PATH)); parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    if args.init: Store(args.db).close()
    if args.seed or not args.init: run(args.port, args.db, args.seed)


if __name__ == "__main__": main()

"""放行预检规则：纯函数，只依赖批次资料，不接触存储与 HTTP。"""
from __future__ import annotations

from datetime import datetime, timezone

VERDICT_LABELS = {"release": "正式放行", "conditional": "有条件放行", "blocked": "暂不可放行"}
CONCLUSION_LABELS = {"release": "正式放行", "conditional": "有条件放行", "hold": "暂缓放行"}

# 预检判断允许 QA 给出的复核结论：可以比预检更保守，不能更宽松
ALLOWED_CONCLUSIONS = {
    "release": {"release", "conditional", "hold"},
    "conditional": {"conditional", "hold"},
    "blocked": {"hold"},
}


def _exception_valid(deviation: dict, moment: datetime) -> bool:
    until = deviation.get("exception_until")
    if not deviation.get("exception_reason") or not until:
        return False
    try:
        return datetime.fromisoformat(str(until).replace("Z", "+00:00")) > moment
    except ValueError:
        return False


def precheck(batch: dict, deviations: list[dict], tests: list[dict], rework: list[dict],
             supplier_changes: list[dict], stability: list[dict], moment: datetime | None = None) -> dict:
    """汇总批次的阻断项与提醒项，并给出正式放行/有条件放行/暂不可放行的判断。"""
    moment = moment or datetime.now(timezone.utc)
    blockers: list[dict] = []
    warnings: list[dict] = []

    if batch["state"] in {"released", "rejected"}:
        blockers.append({"code": "terminal_state", "message": "批次已处于终态，无需再放行"})

    needs_conditional = False
    for d in deviations:
        if d["status"] != "open":
            continue
        if d["severity"] == "critical":
            blockers.append({"code": "critical_deviation", "message": f"关键偏差 #{d['id']} 未关闭：{d['title']}"})
        elif _exception_valid(d, moment):
            needs_conditional = True
            warnings.append({"code": "minor_deviation_exception",
                             "message": f"一般偏差 #{d['id']} 持有效例外（至 {d['exception_until']}），仅可有条件放行"})
        else:
            blockers.append({"code": "minor_deviation_open",
                             "message": f"一般偏差 #{d['id']} 未关闭且无有效例外：{d['title']}"})

    latest: dict[str, dict] = {}
    for t in tests:
        latest[t["test_type"]] = t
    if not latest:
        blockers.append({"code": "no_tests", "message": "尚无任何检验结果"})
    for t in latest.values():
        if not t["passed"]:
            blockers.append({"code": "test_failed",
                             "message": f"检验项目「{t['test_type']}」最新结果不合格（第 {t['round']} 轮）"})
    retested = sorted(t["test_type"] for t in latest.values() if t["round"] > 1)
    if retested:
        warnings.append({"code": "retest", "message": f"项目 {', '.join(retested)} 经过复测，确认复测程序合规"})

    planned = [r for r in rework if r["status"] == "planned"]
    if planned:
        blockers.append({"code": "rework_open", "message": f"{len(planned)} 项返工计划尚未完成"})
    if any(r["status"] == "completed" for r in rework):
        warnings.append({"code": "rework_completed", "message": "批次经过返工，确认返工后检验与评估已覆盖"})

    if not stability:
        warnings.append({"code": "no_stability", "message": "未登记稳定性数据"})
    elif any(not s["passed"] for s in stability):
        blockers.append({"code": "stability_failed", "message": "存在超标的稳定性数据"})

    if supplier_changes:
        warnings.append({"code": "supplier_change",
                         "message": f"存在 {len(supplier_changes)} 条供应商变更，确认影响评估已完成"})

    if blockers:
        verdict = "blocked"
    elif needs_conditional:
        verdict = "conditional"
    else:
        verdict = "release"
    return {"verdict": verdict, "verdict_label": VERDICT_LABELS[verdict],
            "blockers": blockers, "warnings": warnings}

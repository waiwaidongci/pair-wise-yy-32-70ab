"""放行预检规则：根据批次质量资料计算阻断项、提醒项与放行判断。

纯函数，不依赖存储与 HTTP 层；批次资料有任何变化后重新调用即完成重新预检。
"""
from __future__ import annotations

from store import after_now, now

# 放行判断的宽松程度：blocked < conditional < release
# QA 复核结论不允许比预检判断更宽松
RANK = {"blocked": 0, "conditional": 1, "release": 2}


def evaluate_release(batch: dict, deviations: list[dict], tests: list[dict], rework: list[dict],
                     supplier_changes: list[dict], stability: list[dict]) -> dict:
    blockers: list[dict] = []
    warnings: list[dict] = []

    if batch["state"] in {"released", "rejected"}:
        blockers.append({"code": "terminal_state", "message": f"批次已处于终态（{batch['state']}），不能再放行"})

    open_deviations = [d for d in deviations if d["status"] == "open"]
    for d in open_deviations:
        if d["severity"] == "critical":
            blockers.append({"code": "open_critical_deviation",
                             "message": f"未关闭的关键偏差 #{d['id']}：{d['title']}"})
        elif d["exception_reason"] and after_now(d["exception_until"]):
            warnings.append({"code": "minor_deviation_exception",
                             "message": f"一般偏差 #{d['id']} 持有效例外（至 {d['exception_until']}），仅支持有条件放行"})
        else:
            blockers.append({"code": "open_minor_deviation",
                             "message": f"未关闭的一般偏差 #{d['id']}：{d['title']}（缺少有效例外批准）"})
        if d["due_at"] and not after_now(d["due_at"]):
            warnings.append({"code": "deviation_overdue",
                             "message": f"偏差 #{d['id']} 调查已超期（到期 {d['due_at']}）"})

    latest_tests: dict[str, dict] = {}
    for row in tests:
        latest_tests[row["test_type"]] = row
    if not latest_tests:
        blockers.append({"code": "no_tests", "message": "尚无任何检验结果"})
    for row in latest_tests.values():
        if not row["passed"]:
            blockers.append({"code": "failed_test",
                             "message": f"最新检验不合格：{row['test_type']} 第 {row['round']} 轮结果 {row['result']} 超出标准 [{row['spec_min']}, {row['spec_max']}]"})
        if row["round"] > 1:
            warnings.append({"code": "retest",
                             "message": f"检验项目 {row['test_type']} 已复测至第 {row['round']} 轮，确认复测流程合规"})

    for r in rework:
        if r["status"] == "planned":
            blockers.append({"code": "rework_planned", "message": f"返工 #{r['id']} 尚未完成：{r['description']}"})
    completed = [r for r in rework if r["status"] == "completed"]
    if completed:
        warnings.append({"code": "rework_completed",
                         "message": f"存在 {len(completed)} 条已完成返工，确认返工后检验已覆盖"})

    for s in stability:
        if not s["passed"]:
            blockers.append({"code": "failed_stability",
                             "message": f"稳定性考察不合格：{s['condition']} {s['timepoint']} 结果 {s['result']} 超出限度 {s['spec_limit']}"})

    if supplier_changes:
        warnings.append({"code": "supplier_change",
                         "message": f"存在 {len(supplier_changes)} 条供应商变更记录，确认变更评估已完成"})
    if batch["state"] == "awaiting_resample":
        warnings.append({"code": "awaiting_resample", "message": "批次正在等待再取样"})
    if batch["state"] == "conditional":
        warnings.append({"code": "conditional_state", "message": "批次当前为有条件放行状态"})

    if blockers:
        recommendation = "blocked"
    elif open_deviations:  # 走到这里说明未关闭偏差均为持有效例外的一般偏差
        recommendation = "conditional"
    else:
        recommendation = "release"
    return {"recommendation": recommendation, "blockers": blockers, "warnings": warnings, "generated_at": now()}

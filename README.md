# 药品生产偏差与批次放行系统

Python 标准库 + SQLite。批次可关联关键/一般偏差、检验复测、返工、供应商变更和稳定性数据。质量人员可以拒绝、再取样、有条件放行或正式放行；关键偏差始终阻止正式放行，修改必须携带当前批次修订号。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8214`。身份通过 `X-Actor` 与 `X-Role` 模拟，角色为 `operator`、`inspector`、`lab`、`qa`。工厂人员只能修改本工厂批次。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/factories`、`POST /api/batches`：登记工厂和批次。
- `POST /api/batches/{id}/deviations`、`POST /api/deviations/{id}/close`：记录和关闭偏差。
- `POST /api/deviations/{id}/exception`：为一般偏差批准有期限例外。
- `POST /api/batches/{id}/tests`：记录检验和复测轮次。
- `POST /api/batches/{id}/rework`、`POST /api/rework/{id}/complete`：计划和完成返工。
- `POST /api/batches/{id}/supplier-changes`、`POST /api/batches/{id}/stability`：关联供应链和稳定性记录。
- `GET /api/batches/{id}`：批次详情，附带放行预检（阻断项、提醒项、正式/有条件/暂不可放行判断）和复核记录。
- `POST /api/batches/{id}/reviews`：QA 提交复核（`conclusion` 为 `release`/`conditional`/`hold`，附复核意见与当前修订号）。
- `POST /api/batches/{id}/decide`：质量决定，支持并发修订号检查；正式或有条件放行必须引用与当前修订号一致且结论相符的复核。
- `GET /api/state`、`GET /api/health`：状态和健康检查。

## 放行预检与复核

规则（`release_rules.py`）、复核存储（`review_store.py`）和页面（`static/index.html`）分开承担。预检把未关闭关键偏差、无有效例外的一般偏差、缺失或不合格的检验、未完成的返工、超标稳定性数据列为阻断项；复测、已完成返工、供应商变更、缺失稳定性数据列为提醒项。QA 复核保存结论与批次修订号；偏差、检验、返工或稳定性资料变更会推进修订号，旧复核随之失效，需重新预检复核后才能放行。首页可选择批次查看预检结果并提交复核与决定。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：规则以最新检验项目、未关闭偏差和例外有效期为核心，不等同于真实 GMP 质量体系、电子签名、验证或监管提交规范。

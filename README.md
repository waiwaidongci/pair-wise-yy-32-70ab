# 药品生产偏差与批次放行系统

Python 标准库 + SQLite。批次可关联关键/一般偏差、检验复测、返工、供应商变更和稳定性数据。系统按规则自动完成放行预检：批次详情列出阻断项与提醒项，并给出正式放行 / 有条件放行 / 不可放行的判断；QA 复核后保存结论与批次版本，放行决定必须引用仍有效的复核。偏差、检验、返工或稳定性资料一旦修改，批次版本递增，旧复核立即失效并需重新预检复核。

## 分层

- `rules.py`：放行预检规则（纯函数），输出阻断项、提醒项与放行判断。
- `store.py`：SQLite 表结构、迁移与审计日志。
- `service.py`：业务编排——角色权限、批次版本控制、复核保存与放行决定。
- `app.py`：HTTP 接口与进程入口。
- `static/index.html`：首页，选择批次查看预检结果、提交 QA 复核与放行决定。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8214`。身份通过 `X-Actor` 与 `X-Role` 模拟，角色为 `operator`、`inspector`、`lab`、`qa`。工厂人员只能修改本工厂批次。可用 `--port`、`--db` 覆盖。`--seed` 生成演示工厂和一批带未关闭偏差的演示批次。

## 放行流程

1. `GET /api/batches/{id}`：详情中 `precheck` 给出阻断项、提醒项和判断（`release` / `conditional` / `blocked`），`reviews` 列出历史复核及其是否仍然有效。
2. `POST /api/batches/{id}/reviews`：QA 提交 `conclusion` 与 `comment`，系统冻结当前批次版本并保存预检快照；结论不允许比预检判断更宽松。
3. `POST /api/batches/{id}/decide`：正式放行或有条件放行必须携带 `review_id`，复核版本须与批次当前版本一致且结论相符；拒绝与再取样无需复核。
4. 偏差、检验、返工、稳定性（及供应商变更）的任何修改都会递增批次版本，旧复核随之失效，需重新预检并复核后才能放行。

## 主要接口

- `POST /api/factories`、`POST /api/batches`：登记工厂和批次。
- `POST /api/batches/{id}/deviations`、`POST /api/deviations/{id}/close`：记录和关闭偏差。
- `POST /api/deviations/{id}/exception`：为一般偏差批准有期限例外。
- `POST /api/batches/{id}/tests`：记录检验和复测轮次。
- `POST /api/batches/{id}/rework`、`POST /api/rework/{id}/complete`：计划和完成返工。
- `POST /api/batches/{id}/supplier-changes`、`POST /api/batches/{id}/stability`：关联供应链和稳定性记录。
- `POST /api/batches/{id}/reviews`：QA 保存复核结论、意见与批次版本。
- `POST /api/batches/{id}/decide`：质量决定，支持并发修订号检查，放行类决定须引用有效复核。
- `GET /api/batches/{id}`、`GET /api/state`、`GET /api/health`：详情（含预检与复核）、状态和健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：规则以最新检验项目、未关闭偏差和例外有效期为核心，不等同于真实 GMP 质量体系、电子签名、验证或监管提交规范。

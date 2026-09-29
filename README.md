# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据和恢复许可。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计和幂等键。
- `src/service.py`：用例编排、离线记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8336
```

服务启动时自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8336/health
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/device-merges`：发起设备标识合并（仅admin）。
- `POST /api/offline-records`：同步离线端记录，旧编码经合并登记映射到保留设备。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验和未关闭整改共同限制。

## 设备标识合并

旧楼资产号（`asset_no`）与城市监管编码（`supervision_code`）并轨时，以一台设备为保留设备，另一台为源设备：

1. `POST /api/device-merges` 提交 `{"source_equipment_id","retained_equipment_id","merge_id"?}`，建表式作业进入`in_progress`，双方立即停用（`suspended`，数据置`merge_in_progress`），完成前不能恢复运行、不能新增检验/维保。
2. 对合并作业执行 `complete` 时，在同一写事务内执行守卫、迁移和发布：
   - 检验、维保、报警、恢复许可全部改挂保留设备，原记录不删除，带 `previous_equipment_id` / `migrated_by_merge` 溯源并写审计；源设备转为`merged`，保留设备恢复原状态并登记旧编码别名。
   - 旧编码（任一命名空间）被其他在用设备占用（`code_occupied`）、合并开始后出现基线外的新报警（`new_alarm`）、或迁移后双方同码活动报警会冲突（`duplicate_active_alarm_after_merge`）时，作业持久化为`blocked`，409响应的`reasons`逐条列出依据（占用设备ID、报警ID等），设备继续停用。
   - 处置完依据后，对作业执行 `retry`（刷新报警基线）后再 `complete`；或直接带原请求再次 `complete`（沿用原始基线）。同一`merge_id`/`Idempotency-Key`重试与完成后重放都收敛到原作业，失败不产生半成品。
3. 两批导入并发提交同一设备时，数据库写锁加`merge_locks`/`merge_registry`唯一约束保证只有一个成功，另一方收到409冲突；胜者完成后败者用原数据重试会直接拿到已完成作业。
4. 离线端仍按旧编码登记：同步时通过设备ID、`asset_no`、`supervision_code`与合并登记映射到保留设备。报警/救援按`(source_id, record_id)`生成稳定指纹并落库`sync_fingerprints`，救援任务另有稳定`dedupe_key`，重复上报只返回`deduped`/原记录，不会重复派出救援。合并窗口内无法处理的记录置`unmapped`，合并完成后再次同步自动重放。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。

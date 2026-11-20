# Schema Bridge —— 事件模式转换服务

在 v1（扁平结构）与 v2（信封 + 嵌套 money 对象）两种事件字段结构之间做双向转换，
支持**双写、回放、按消费者选择版本、隔离队列、下线影响报告与严格幂等**，
并提供查看模式差异、消费者绑定、失败样本与回放结果的 Web 页面。

零第三方依赖：Python 3.11 标准库 + SQLite。

## 快速开始

```bash
# 运行全部测试（37 个）
python3 -m unittest discover -s tests -v

# 写入演示数据（覆盖升级/回滚/隔离/回放/影响报告全场景）
python3 -m schema_bridge.cli seed data/demo.sqlite3

# 启动 Web 服务（页面 + JSON API）
python3 -m schema_bridge.cli serve --db data/demo.sqlite3 --port 8000
# 打开 http://127.0.0.1:8000
```

## v1 / v2 结构差异（摘要）

| 维度 | v1（legacy） | v2 |
|---|---|---|
| 形态 | 扁平字段 | `schema_version` + `event` 信封 + `event.data.order` 嵌套 |
| 金额 | `amount` / `items[].unit_price`（分） | 合并为 `money{amount_cents,currency}` / `unit_money{...}` |
| 客户 | `customer_id` | 包装为 `customer.id`（v2 中可选） |
| 命名 | `qty`、`remark` | `quantity`、`note` |
| 新增 | — | `schema_version`、明细级 `unit_money.currency` |

完整字段映射见页面 `/diff` 或 `GET /api/diff`。

## 需求与实现对照

| 需求 | 实现 |
|---|---|
| v1/v2 两种字段结构 | `schema_bridge/schemas.py` 字段文档与映射表；`converter.py` 双向校验+转换 |
| 双写 | 每个事件同时转换并落库 v1、v2 两行（`converted_events` 表，主键 `event_id+version`） |
| 回放 | `service.replay()`：单条/按 event_id 批量/全部未解决；支持提交人工修正原文，回放批次与逐条结果持久化 |
| 按消费者选择版本 | 消费者注册表带 `bound_version`，投递时按绑定取对应版本；页面可随时切换 |
| 转换失败保留原文进隔离队列 | `quarantined` 表保存原始 payload、字段级错误、尝试次数与状态（open/reopened/resolved），原文永不丢弃 |
| 下线前消费者影响报告 | `service.impact_report(version)`：受影响消费者、绑定历史、旧版本投递量、替代版本双写就绪度、阻塞性隔离事件，给出「可下线/不可下线」结论 |
| 重复转换幂等 | ① 转换为确定性纯函数，规范化 JSON 存储；② 事件/双写/隔离全部自然键 upsert；③ 投递日志 `UNIQUE(event_id, consumer)`，重复投递只跳过 |
| 部分消费者升级/回滚 | 绑定按消费者独立切换，全部变更写入 `consumer_binding_history` 审计表；切到相同版本不产生审计 |

### 幂等的边界处理

- 同 `event_id` + 同原文且已成功双写：直接返回 `duplicate`，不重复转换。
- 同 `event_id` + 同原文但仍有未解决隔离：重新执行转换并累加隔离 `attempts`（状态置 `reopened`），
  不会被幂等短路掩盖持续失败。
- 同 `event_id` + 变化后的原文：按 upsert 重新转换覆盖，结果仍由规范化 JSON 保证确定性。
- 回放提交的修正原文若改了 `event_id` 会被拒绝，防止事件身份漂移。

## 页面

| 路径 | 内容 |
|---|---|
| `/` | KPI 总览、快速摄入 |
| `/diff` | v1/v2 字段结构与映射差异 |
| `/events`、`/events/{id}` | 事件双写结果、原文、隔离与投递记录 |
| `/quarantine`、`/quarantine?id=N` | 失败样本列表；详情含保留原文、字段级错误与修正回放表单 |
| `/replay` | 触发全量/批量回放，查看历史批次与逐条结果 |
| `/consumers` | 注册消费者、切换绑定版本（升级/回滚）、查看审计 |
| `/impact?version=v1` | 版本下线消费者影响报告 |

## JSON API

| 方法与路径 | 说明 |
|---|---|
| `POST /api/ingest` | 摄入事件并双写；body 为事件对象，或 `{"payload": <事件>}` |
| `GET /api/events`、`GET /api/events/{id}` | 事件列表/详情（含双写与隔离） |
| `GET /api/quarantine?status=open` | 隔离队列 |
| `POST /api/replay` | `{"quarantine_id":N}` / `{"all_open":true}` / `{"event_ids":[...]}`，可带 `corrected` 修正原文 |
| `GET /api/replay` | 回放批次与结果 |
| `POST /api/consumers/register` | `{"name","version","description"}` |
| `POST /api/consumers/bind` | `{"name","version","reason"}` 升级或回滚 |
| `GET /api/consumers` | 消费者列表 |
| `POST /api/deliver` | `{"event_id","consumer"}` 按绑定版本投递（幂等） |
| `POST /api/deliver-all` | 投递给全部消费者 |
| `GET /api/impact?version=v1` | 下线影响报告 |
| `GET /api/diff`、`GET /api/stats` | 结构差异与统计 |

## 目录结构

```
schema_bridge/
  schemas.py    # v1/v2 字段定义、映射文档、diff 视图
  converter.py  # 版本探测、双向校验与确定性转换（纯函数）
  store.py      # SQLite 表结构、upsert、隔离/回放/审计/投递日志
  service.py    # 编排：摄入双写、隔离、回放、消费者、影响报告
  web.py        # http.server 页面 + JSON API
  seed.py       # 端到端演示场景
  cli.py        # serve / seed 命令
tests/          # converter / service / web 三层测试
```

## 设计说明

- **存储**：SQLite 单连接 + 重入锁与嵌套事务深度计数，内存库（`:memory:`）与文件库行为一致，
  适配 `ThreadingHTTPServer`。
- **失败语义**：隔离队列按 `(event_id, target_version)` 去重；结构完全无法识别的事件
  以 `target_version=unknown` 隔离，修正后经完整双写路径重新入库。
- **v2 → v1 降级**：v2 的 `customer` 可选而 v1 必填，缺失时 `customer_id` 置空并在结果中
  带 `_degraded` 标记，不静默丢语义。

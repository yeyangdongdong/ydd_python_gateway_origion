# 开发与扩展指南

面向要改代码的人：怎么跑起来、代码从哪读起、测试怎么组织、常见改动怎么做。

## 1. 环境

```powershell
cd D:\Document\gateway\python_issue\ydd_python_gateway_origion
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

本机已有一个可复用的虚拟环境：`D:\Document\gateway\python_issue\ydd-python-gateway-venv`
（Python 3.14.0，已装好全部依赖），直接用它即可，不必再建 `.venv`。

跑起来：

```powershell
& "D:\Document\gateway\python_issue\ydd-python-gateway-venv\Scripts\python.exe" run.py
```

跑测试：

```powershell
& "D:\Document\gateway\python_issue\ydd-python-gateway-venv\Scripts\python.exe" -m unittest discover -s tests -v
```

只跑单个用例：

```powershell
& "...\python.exe" -m unittest tests.test_gateway.GatewayTests.test_long_history_is_trimmed_to_budget -v
```

> 注意：一个数据库只能由一个进程服务。不要一边跑 `run.py` 一边用同一份 `data/gateway.sqlite3` 跑第二个实例。
> 测试用的是临时目录里的独立数据库，所以"跑测试"和"跑服务"可以并存。

## 2. 代码地图与阅读顺序

建议按这个顺序读，每一步都只依赖前面已经看过的内容：

| 顺序 | 文件 | 行数 | 读它是为了知道 |
| --- | --- | --- | --- |
| 1 | `run.py` | 29 | 入口、监听地址、对外监听保护 |
| 2 | `gateway/app.py` | 358 | 路由、鉴权、校验、错误如何变成状态码；`chat` / `stream` / 代理路由如何接线 |
| 3 | `gateway/service.py` | 100 | 唯一的业务编排：排队 → 取历史 → 裁剪 → 调模型 → 落库 → 去重 |
| 4 | `gateway/context.py` | 135 | token 估算与预算裁剪（纯函数，最容易改） |
| 5 | `gateway/provider.py` | 122 | demo 回显与 SSE 解析、上游错误如何归一 |
| 6 | `gateway/storage.py` | 296 | 迁移、表结构、事务边界、查询 |
| 7 | `gateway/tickets.py` | 62 | 一次性票据（30 秒、单次、绑定资源） |
| 8 | `gateway/proxy.py` | 443 | 可选反向代理：路由表校验、加权轮询与冷却、HTTP 流式转发、WS 桥接 |
| 9 | `gateway/config.py` | 105 | 配置项与启动校验 |
| 10 | `web/chat.html` | 330 | 浏览器页：取票、连 WS、流式渲染 |

合计约 1,670 行 Python（含注释与文档字符串）+ 330 行页面，不含测试。
行数会漂移，用它判断"该先读哪个"，不要当作精确指标。

不熟悉的状态机、错误分层、并发模型，先看 [architecture.md](architecture.md)。

## 3. 测试

### 3.1 结构

只有一个测试文件 `tests/test_gateway.py`（约 950 行，50 个用例），原则是**从真实入口验证**：

- 用 `fastapi.testclient.TestClient` 走真实的 HTTP 与 WebSocket 路径，不直接调用 `ChatService`；
- 模型服务用本地 `ThreadingHTTPServer` 替身（`ModelStub`），反向代理则用 `EchoStub`（HTTP）与
  `WsEchoUpstream`（独立线程里的 `websockets` 服务），只在需要"上游行为"时使用；
- 每个用例一个 `tempfile.TemporaryDirectory` + 独立 SQLite 文件，互不干扰；
- 少数纯函数（估算、裁剪、票据、配置校验）直接单测，不绕 HTTP。

### 3.2 模型替身能造出的场景

`ModelStub` 依据请求里最后一条消息的内容（`content`）决定行为：

| 输入 | 上游行为 | 用来验证 |
| --- | --- | --- |
| 普通文本 | 正常 SSE：注释行 + 多行 `data` + `finish_reason: stop` + `[DONE]` | 正常链路、SSE 边界解析 |
| `[http-error]` | 直接返回 HTTP 500 | 上游错误映射为 `502 model_http_error` |
| `[truncated]` | 发完半句就断 | 缺 `[DONE]` → `502 model_incomplete` |
| `[slow]` | 发第一个增量后阻塞，直到测试放行 | 并发、会话排队、断连中断 |

"多行 `data`"与"注释行"是刻意造的，用来覆盖真实 SSE 里容易踩的解析错误。

### 3.3 覆盖矩阵

| 领域 | 用例 |
| --- | --- |
| 多轮上下文、去重、重启复用 | `test_http_context_idempotency_and_restart` |
| WS 流式与 HTTP 重放互通 | `test_websocket_stream_and_http_replay` |
| 会话内串行 / 跨会话并行 | `test_session_serialization_across_transports_and_parallel_sessions` |
| 断连 → `interrupted` | `test_websocket_disconnect_records_interrupted_run` |
| 崩溃恢复（启动修复） | `test_restart_marks_unfinished_durable_run_interrupted` |
| 失败不污染上下文 | `test_failed_provider_requests_do_not_pollute_next_context` |
| 鉴权、非法输入、未知字段 | `test_authentication_and_invalid_inputs` |
| 管理接口（增删改查、守卫） | `test_agent_management`、`test_session_management_and_listing`、`test_management_refuses_while_generating` |
| 清空历史后复用 `request_id` | `test_clear_history_allows_reusing_request_id` |
| 分页 | `test_message_listing_pagination` |
| 票据（单次、绑定、过期） | `test_ticket_grants_websocket_access_once`、`test_ticket_is_bound_to_one_session`、`test_ticket_store_expires_and_is_single_use` |
| 页面与健康检查免鉴权 | `test_chat_page_and_health_need_no_token` |
| 上下文裁剪 | `test_estimate_tokens_is_conservative`、`test_context_keeps_newest_turn_without_splitting_pairs`、`test_long_history_is_trimmed_to_budget`、`test_short_history_reports_no_trimming`、`test_oversized_input_returns_413_and_can_retry`、`test_context_window_must_leave_room` |
| 系统代理 | `test_model_client_ignores_system_proxy_by_default`、`test_system_proxy_can_be_enabled_explicitly`、`test_system_proxy_setting_is_validated` |
| 错误形状统一 | `test_websocket_error_events_share_one_shape`、`test_websocket_gateway_error_has_status_and_ids`、`test_http_stream_error_uses_error_envelope` |
| 数据库迁移与旧库升级 | `test_schema_version_upgrades_legacy_database` |
| 反向代理（转发、剥前缀、鉴权、502/504、回环、BOM、配置校验） | `test_proxy_forwards_http_and_strips_prefix`、`test_proxy_can_keep_prefix`、`test_proxy_requires_gateway_token`、`test_proxy_stays_disabled_without_routes_file`、`test_proxy_websocket_passthrough`、`test_proxy_upstream_failures`、`test_proxy_timeout_maps_to_504`、`test_proxy_loop_guard`、`test_proxy_route_file_tolerates_bom`、`test_proxy_route_config_is_validated` |
| 代理选路与失败语义 | `test_proxy_round_robin_uses_all_upstreams`、`test_proxy_weighted_round_robin`、`test_proxy_failover_and_cooldown`、`test_proxy_post_is_not_replayed_on_connect_error` |
| 代理规则 | `test_proxy_longest_prefix_wins`、`test_proxy_methods_can_be_limited`、`test_proxy_can_add_path_prefix`、`test_proxy_websocket_can_be_disabled` |
| 代理 WS 票据（浏览器准入） | `test_proxy_ticket_grants_browser_websocket`、`test_proxy_ticket_rejects_unknown_path` |
| demo 模式零依赖 | `test_default_demo_requires_no_model_service` |

### 3.4 写新用例时的三个坑

1. **`TestClient` 的默认头会带进 WebSocket**。用 `TestClient(app, headers={"Authorization": "Bearer ..."})`
   创建时，`websocket_connect` 也会带上这个头，于是"票据鉴权"根本没被走到。要模拟浏览器必须显式覆盖：
   `client.websocket_connect(url, headers={"Authorization": ""})`。
2. **`TestClient` 自己忽略代理环境变量**，所以可以安全地在用例里 `mock.patch.dict(os.environ, ...)` 测代理行为；
   真正会走代理的是 `ModelProvider` 里的 httpx。
3. **不要用 `time.sleep` 等模型**。`ModelStub.slow_started` / `release_slow` 就是为此存在的：
   等事件、放行、再断言；并在 `finally` 里兜底 `release_slow.set()`，否则用例会挂死。

## 4. 常见改动怎么做

### 4.1 新增一个模型适配器

1. 在 `provider.py` 增加分支（或新建 `provider_xxx.py`，保持同样的 `stream(messages, model) -> AsyncIterator[str]` 契约）。
2. 只 `yield` 文本增量；不要在这里写数据库或拼历史。
3. 所有失败都映射成 `GatewayError`，并选一个已有的或新的 `code`：
   - 上游 4xx/5xx → `502/503`；
   - 超时 → `504 model_timeout`；
   - 协议不符 → `502 model_protocol_error`。
4. 新增配置项按 [4.4](#44-新增一个配置项) 走一遍。
5. 补一个或两个用例：正常流 + 至少一种失败流。

### 4.2 新增一个 HTTP 接口

```python
@app.get("/sessions/{session_id}/something", dependencies=auth)
async def something(session_id: str):
    await run_in_threadpool(store.get_session, session_id)   # 先确认存在
    return await run_in_threadpool(store.some_query, session_id)
```

- 需要校验就用 Pydantic 模型（继承 `InputModel`，`extra="forbid"` 已内置）；
- 业务失败抛 `GatewayError(status, code, message)`，不要再写 `JSONResponse`；
- 同步的数据库调用务必用 `run_in_threadpool`，否则会阻塞事件循环；
- 改完同步 [api.md](api.md) 的端点和错误码表。

### 4.3 新增事件字段

1. `service.py` 里在 `yield` 的字典中加字段（`identifiers` / `context_info` 可复用）。
2. HTTP 的 `chat` 会自动把它放进响应体；WS 的 `send_json` 会自动透传。
3. `web/chat.html` 若需要展示，在 `onmessage` 里读 `data.<字段>`。
4. 补测试断言，并在 [api.md §3.3](api.md#33-事件) 更新事件表。
5. 新字段必须**追加**，不要改名或删除已有字段（契约稳定性见 [api.md §6](api.md#6-契约稳定性)）。

### 4.4 新增一个配置项

五处都要改，漏一处就会出现"配了但不生效"：

| 文件 | 改什么 |
| --- | --- |
| `gateway/config.py` | `Settings` 加字段（带默认值）+ `__post_init__` 校验 + `from_env` 解析 |
| `.env.example` | 加一行，带注释说明取值与影响 |
| `README.md` | "配置参考"表加一行 |
| 使用处 | 从 `settings.xxx` 读，不要直接读 `os.environ` |
| `tests/` | 至少一个默认值/非法值断言 |

解析要注意：数值型用 `int/float` 并给中文错误提示；布尔型统一用 `read_flag()`（接受 `true/1/yes/on`，非法值报错而不是静默 False）。

### 4.5 新增表或字段

数据库结构由 `gateway/storage.py` 的 `MIGRATIONS` 管理：每项是 `(版本号, 语句元组)`，
`Store.initialize()` 会按版本号从小到大补齐并写入 `schema_version` 表。加字段就是在**末尾追加**一个版本：

```python
MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
    (1, (...)),                                    # 已发布，永不修改
    (2, (
        "ALTER TABLE runs ADD COLUMN token_usage INTEGER",
    )),
]
```

三条规则：

1. **只追加，不修改已发布的条目**。旧库正是靠这些语句从版本 0 逐级升到最新，改了就升不上去。
2. 新语句必须能安全作用在**已有数据**上：加列要么允许 `NULL`、要么带 `DEFAULT`；加表用 `IF NOT EXISTS`。
3. 版本号必须严格递增且不重复（`_migrate` 按顺序判断 `version > current`）。

验证方式：用旧库文件跑一次 `python run.py`，或照抄 `test_schema_version_upgrades_legacy_database` 的写法
（造一个没有 `schema_version` 的库 → `initialize` → 断言版本号与旧数据都在）。

没有回滚脚本：迁移只进不退。需要降级时只能改代码或从备份恢复，所以动生产库前先备份。

### 4.6 修改裁剪策略

`context.py` 的三个不变量不能破：

1. 发出去的消息估算总和 ≤ `budget_tokens`；
2. 历史保持"用户开头、成对出现、严格交替"；
3. 数据库里的历史不被修改（只影响本次请求）。

改动后至少同步：

- `test_context_keeps_newest_turn_without_splitting_pairs`（算法）；
- `test_long_history_is_trimmed_to_budget`（端到端与事件字段）；
- README 的公式与 [api.md §5](api.md#5-context-对象) 的字段含义。

### 4.7 把另一个项目挂到网关入口（不用改代码）

1. 确认上游已经在跑，例如 `http://127.0.0.1:9001`；
2. 复制 `routes.json.example` 为 `routes.json`，加一条 `{"prefix": "/finance/", "target": "http://127.0.0.1:9001"}`；
3. 重启网关，访问 `http://127.0.0.1:8000/finance/`。

需要给上游带凭据时加 `"headers": {"X-Api-Key": "..."}`；希望保留前缀就把 `strip_prefix` 设为 `false`；
上游要求 `/api/...` 就加 `"add_prefix": "/api"`；只开放部分方法用 `"methods": ["GET"]`。
多个实例就写 `"targets": [{"url": "...", "weight": 1}, ...]`，按权重轮询并自动冷却故障上游。
前缀不能与内置接口冲突（`/sessions/` 等），配置写错时启动会直接报错。`routes.json` 已被忽略，不会进仓库。

浏览器要连被代理的 WebSocket 时，页面先 `POST /proxy-ticket` 换票（票据绑定那条精确路径），
再以 `?ticket=` 连接；票据不会转发给上游。

若想改代理的选路或失败语义，改 `proxy.py` 的 `_attempts` / `_cool`，并同步 `test_proxy_*` 与
[api.md §2.8](api.md#28-反向代理前缀按-routesjson-动态注册)。

## 5. 调试手法

| 想确认 | 做法 |
| --- | --- |
| 接口长什么样 | 打开 `http://127.0.0.1:8000/docs` |
| 服务是否活着 | `curl http://127.0.0.1:8000/health`（或用 Invoke-RestMethod） |
| 上一轮到底发生了什么 | `GET /sessions/{id}/requests/{request_id}`，看 `status` / `error_code` |
| 历史里有什么 | `GET /sessions/{id}/messages`，注意 `run_status` |
| 数据库原始内容 | 见下面的一行式查询 |
| 模型到底收到了什么 | 在 `provider.py` 的 `payload` 上临时断点/打印（**别提交**），或用测试替身断言 |

```powershell
# 不装 sqlite3 命令行也能查：直接用 Python
& "...\python.exe" -c "import sqlite3;c=sqlite3.connect(r'data/gateway.sqlite3');c.row_factory=sqlite3.Row;[print(dict(r)) for r in c.execute('SELECT id,status,error_code,request_id FROM runs ORDER BY created_at DESC LIMIT 5')]"
```

排查顺序建议：先看运行记录的 `status` 与 `error_code`（业务是否成功），再看服务日志（异常类型），
最后才怀疑模型服务本身（用 `curl` 直接打 `MODEL_BASE_URL/chat/completions` 验证）。

## 6. 提交前检查清单

- [ ] `python -m unittest discover -s tests` 全绿（当前 50 个）
- [ ] `python -m compileall -q gateway tests examples run.py` 通过
- [ ] 改了接口/事件/错误码 → 同步 `docs/api.md`
- [ ] 改了行为或配置 → 同步 `README.md` 与 `.env.example`
- [ ] 改了架构性取舍 → 在 `docs/design-decisions.md` 补一条 ADR
- [ ] 没有把 `.env`、`data/`、`routes.json`、`__pycache__/` 带进提交（`.gitignore` 已覆盖）
- [ ] 日志里没有用户内容、上游响应或密钥
- [ ] 新增的配置项在 README 配置表里有说明

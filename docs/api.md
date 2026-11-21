# 接口参考

本文是 `ydd_python_gateway_origion` 的**契约文档**：路径、字段、状态码、事件与错误码。
内部原理见 [architecture.md](architecture.md)，安装与使用见 [README](../README.md)。

## 1. 通用约定

| 约定 | 说明 |
| --- | --- |
| 基地址 | `http://127.0.0.1:8000`（`python run.py --port 8001` 可改） |
| 编码 | UTF-8；写接口请发 `Content-Type: application/json` |
| 鉴权 | `Authorization: Bearer <GATEWAY_TOKEN>`；仅 `GET /`、`GET /health`、`/docs` 免鉴权 |
| Agent / 会话 / 运行 ID | `uuid4().hex`，32 位小写十六进制；默认 Agent 固定为 `default` |
| 消息 ID | 自增整数，越大越新 |
| 时间 | UTC ISO 8601，如 `2026-11-17T15:30:00.123456+00:00` |
| 未知字段 | 请求体**拒绝**（`extra="forbid"` → 422）；响应中的未知字段请忽略 |
| 分页 | `limit` + `offset`，按创建顺序升序；`offset` 越大越新 |
| 请求追踪 | 可带 `X-Request-ID`；网关在响应头回显（没有则生成），并写入访问日志的 `trace_id` |
| 交互式文档 | <http://127.0.0.1:8000/docs>（Swagger UI）、`/openapi.json` |

### 1.1 错误响应（两种载体，同一种内层结构）

`error` 对象**永远只有** `code` 和 `message`；会话、请求、运行编号放在外层。

| 载体 | 形状 |
| --- | --- |
| HTTP | `{"error": {"code", "message"}}`。所有失败响应都是这个形状；有编号时追加顶层 `session_id` / `request_id` / `run_id` |
| WebSocket | `{"type": "error", "status": <HTTP 等价状态码>, "session_id"?, "request_id"?, "run_id"?, "error": {"code", "message"}}` |

不存在的标识符**直接省略**，不会出现 `null`。`message` 是给人看的中文提示，**不要**用它做程序判断；
分支请用 `error.code`，HTTP 看状态行、WS 看 `status`。

### 1.2 分页参数

| 端点 | `limit` 默认 | `limit` 范围 | `offset` |
| --- | --- | --- | --- |
| `GET /agents` | 100 | 1–1000 | ≥ 0 |
| `GET /sessions` | 100 | 1–1000 | ≥ 0 |
| `GET /sessions/{id}/messages` | 1000 | 1–5000 | ≥ 0 |

越界（如 `limit=0`）返回 `422 invalid_request`。列表没有总数与游标，需要翻页就自己递增 `offset`。

## 2. HTTP 端点

### 2.1 页面与健康检查

#### `GET /`

返回内置聊天页（`text/html`，`Cache-Control: no-store`）。**不需要 token**：页面本身不含密钥，只是外壳。

#### `GET /health`

```json
{"status": "ok", "provider": "demo", "auth_required": false}
```

| 字段 | 含义 |
| --- | --- |
| `provider` | `demo`（回显）或 `openai`（真实模型） |
| `auth_required` | 是否设置了 `GATEWAY_TOKEN`；供页面提示，不是安全边界 |

不需要 token，可用于探活。

#### `GET /proxy-status`（需鉴权）

反向代理的只读状态：每个上游是否健康、是否在冷却、最近一次失败原因。

```json
{
  "enabled": true,
  "routes": [{
    "prefix": "/finance/",
    "health_enabled": true,
    "upstreams": [{
      "url": "http://127.0.0.1:9001",
      "healthy": false,
      "cooling": false,
      "cooldown_seconds": 0.0,
      "consecutive_failures": 3,
      "last_error": "HTTP 500"
    }]
  }]
}
```

未配置 `routes.json` 时返回 `{"enabled": false, "routes": []}`。
`/proxy-status` 是保留路径，不能用作代理前缀。

### 2.2 Agent

Agent 是"名称 + 系统提示词 + 模型名"的组合。默认 Agent（`id=default`）在首次启动时创建。

#### `GET /agents`

```json
[
  {
    "id": "default",
    "name": "默认助手",
    "system_prompt": "你是一个友好、准确的中文助手。",
    "model": "demo",
    "created_at": "2026-11-17T15:30:00.000000+00:00"
  }
]
```

#### `POST /agents` → `201`

```json
{"name": "Python 学习助手", "system_prompt": "请用简单的话解释。", "model": "gpt-4o-mini"}
```

| 字段 | 必填 | 约束 | 默认 |
| --- | --- | --- | --- |
| `name` | 是 | 去空白后 1–100 字符 | — |
| `system_prompt` | 否 | 1–16000 字符 | `你是一个友好、准确的中文助手。` |
| `model` | 否 | 1–200 字符 | 当前 `MODEL_NAME` |

#### `PATCH /agents/{agent_id}` → `200`

只提交要改的字段，未提交的保持原值。全部省略或传 `{}` → `422 invalid_request`。

```json
{"name": "改名后的助手", "model": "另一个模型"}
```

返回修改后的完整 Agent。修改默认 Agent 的 `name` / `system_prompt` 会保留，但它的 `model` 在下次启动时仍会跟随 `MODEL_NAME`。

#### `DELETE /agents/{agent_id}` → `204`（无响应体）

| 情况 | 结果 |
| --- | --- |
| 不存在 | `404 agent_not_found` |
| 仍有会话引用 | `409 agent_in_use` |
| `id` 为 `default` | `409 default_agent` |

### 2.3 会话

#### `GET /sessions`

返回 `id`、`agent_id`、`title`、`created_at` 的数组（不含 Agent 配置，详情请查单个会话）。

#### `POST /sessions` → `201`

```json
{"agent_id": "default", "title": "第一次对话"}
```

`agent_id`、`title` 都可省略**字段**，但请求体本身仍然必须存在——至少发 `{}`，完全不带 body 会返回 `422`。

| 字段 | 默认 | 约束 |
| --- | --- | --- |
| `agent_id` | `default` | 1–100 字符，必须存在，否则 `404 agent_not_found` |
| `title` | `新会话` | 1–200 字符 |

#### `GET /sessions/{session_id}`

```json
{
  "id": "c300da6008fb4f6db9788f41c4a587a1",
  "agent_id": "default",
  "title": "第一次对话",
  "created_at": "2026-11-17T15:31:00.000000+00:00",
  "system_prompt": "你是一个友好、准确的中文助手。",
  "model": "demo"
}
```

后两个字段来自关联 Agent，方便客户端确认"这一问会用什么配置"。

#### `PATCH /sessions/{session_id}` → `200`

```json
{"title": "改名后的会话"}
```

#### `DELETE /sessions/{session_id}` → `204`

连同该会话的消息与运行记录一起删除。会话正在生成（存在 `running` 记录）时返回 `409 session_busy`。

### 2.4 消息

#### `GET /sessions/{session_id}/messages`

```json
[
  {
    "id": 1,
    "session_id": "c300da60...",
    "run_id": "7f268604...",
    "role": "user",
    "content": "你好",
    "created_at": "2026-11-17T15:31:01.000000+00:00",
    "run_status": "completed"
  }
]
```

- `role` 只会是 `user` 或 `assistant`。
- `run_status` 是该消息所属运行的状态；**失败轮的 user 消息也会返回**（`run_status` 为 `failed` / `interrupted`），
  但它们不会进入模型上下文。
- 按 `id` 升序（最早优先），因此 `offset` 越大越新。

#### `DELETE /sessions/{session_id}/messages`

```json
{"deleted": 4}
```

清空历史，同时删除该会话的运行记录。副作用：旧 `request_id` 之后可以重新使用（不再被判为重放或冲突）。
会话正在生成时返回 `409 session_busy`。

### 2.5 提问（等待完整回答）

#### `POST /sessions/{session_id}/chat` → `200`

```json
{"content": "你好", "request_id": "question-001"}
```

| 字段 | 必填 | 约束 | 默认 |
| --- | --- | --- | --- |
| `content` | 是 | 去空白后 1–32000 字符 | — |
| `request_id` | 否 | 1–100 字符 | 服务端生成 `uuid4().hex` |

成功响应（即 `done` 事件）：

```json
{
  "type": "done",
  "session_id": "c300da6008fb4f6db9788f41c4a587a1",
  "request_id": "question-001",
  "run_id": "43c0459a16684f2080c55f24ca2d20b8",
  "reply": "[演示模式] 收到：你好",
  "replayed": false,
  "context": {"trimmed_messages": 0, "estimated_tokens": 42, "budget_tokens": 6016}
}
```

- `replayed: true` 表示命中已完成的相同 `request_id`，此时**没有** `context` 字段，也不会调用模型。
- 失败时返回对应状态码的 JSON，形状见 [1.1](#11-错误响应两种载体同一种内层结构)：`{"error": {"code", "message"}}`，
  并在外层附带 `session_id` / `request_id` / `run_id`。

| 情况 | 状态码 | `error.code` |
| --- | --- | --- |
| 输入超上下文预算 | 413 | `context_too_large`（不写运行记录，改短后可用同一 `request_id` 重试） |
| 同 `request_id` 不同内容 | 409 | `request_conflict` |
| 同 `request_id` 曾失败/中断 | 409 | `request_not_completed` |
| 模型阶段失败 | 500/502/503/504 | 见 [第 4 节](#4-错误码全表) |

### 2.6 一次性票据

#### `POST /sessions/{session_id}/ticket` → `200`

```json
{"ticket": "YFPI5a1GeI6zWv_ARbmtVuGjoKCGQGE6OFZn6qjGvRw", "session_id": "c300da60...", "expires_in": 30}
```

供浏览器连接 WebSocket 使用（浏览器无法设置请求头）。规则：

- 只能用一次，连接时核销；
- 只对签发时绑定的**资源**有效：这里是 `session:<会话编号>`；用错资源同样作废；
- 30 秒有效期（`gateway/tickets.py` 的 `TICKET_SECONDS`）；
- 只存内存，服务重启即失效。

#### `POST /proxy-ticket` → `200`

给"被反向代理的 WebSocket"签一张票据（代理没配置时返回 `404 route_not_found`）。

```json
{"path": "/finance/live"}
```

```json
{"ticket": "IKl8vC4PTwGtRdHag-...", "path": "/finance/live", "expires_in": 30}
```

- `path` 必须落在某个已配置的前缀下，否则 `404 route_not_found`；不以 `/` 开头则 `422`；
- 票据资源是 `ws:<path>`，**只对这条精确路径有效**，一次性，30 秒；
- 连接时用 `WS /finance/live?ticket=...`；网关会核销票据，但**不会**把 `ticket` 查询参数转发给上游。

### 2.7 运行记录查询

#### `GET /sessions/{session_id}/requests/{request_id}`

```json
{
  "id": "43c0459a16684f2080c55f24ca2d20b8",
  "session_id": "c300da60...",
  "request_id": "question-001",
  "input": "你好",
  "status": "completed",
  "reply": "[演示模式] 收到：你好",
  "error_code": null,
  "error_message": null,
  "created_at": "2026-11-17T15:31:01.000000+00:00",
  "finished_at": "2026-11-17T15:31:01.010000+00:00"
}
```

`status` 取值：`running` / `completed` / `failed` / `interrupted`。
请求尚未开始或不存在 → `404 request_not_found`（可安全用于"发出的请求有没有落地"的判断）。

#### `GET /runs/{run_id}`

同上结构，按运行 ID 查询。不存在 → `404 run_not_found`。

#### `GET /audit`

审计事件（新→旧），需鉴权。参数：`limit`（1–1000，默认 100）、`offset`、`action`、`result`。

```json
[{"id": "9f2c...", "ts": "2026-11-21T00:30:12.428+00:00", "trace_id": "b4c8...",
  "action": "agent.delete", "result": "ok", "reason": "", "ip": "192.168.1.20",
  "actor": {"type": "token", "id": "legacy", "name": "gateway-token"},
  "target": {"type": "agent", "id": "2b1c..."}, "details": {}}]
```

`action` 取值见 README 的"审计留痕"表；`details` 是对象（写库时存 JSON 字符串，查询接口已还原）。

### 2.8 反向代理前缀（按 `routes.json` 动态注册）

只有配置了 `routes.json` 才存在，路径由配置决定，因此不出现在 `/docs` 的固定接口里（但会作为 `/prefix/{path}` 出现）。
每个前缀同时接受 HTTP 的全部方法与 WebSocket。

```text
GET   /finance/report?x=1   →  http://127.0.0.1:9001/report?x=1     （strip_prefix: true）
WS    /finance/live         →  ws://127.0.0.1:9001/live
```

| 规则 | 说明 |
| --- | --- |
| 前缀匹配 | 最长前缀优先；`/a/b/` 不会被 `/a/` 遮蔽 |
| 选路 | 多个上游按平滑加权轮询；连接失败的上游进入冷却（`cooldown_seconds`，默认 5 秒） |
| 探活 | 顶层 `health` 或路由级 `health` 开启后，主动探测并摘除不健康上游；状态见 `GET /proxy-status` |
| 失败切换 | 只有幂等方法（GET/HEAD/OPTIONS）在连接失败时换下一个上游；POST 等不重放；响应开始后不重试 |
| 鉴权 | 与其他业务接口相同：设置了 `GATEWAY_TOKEN` 就必须带 `Authorization`；代理 WS 还可用 `/proxy-ticket` 换的票据 |
| 权限 | 路由可配 `required_scope`：API Key 必须带该 scope（或 `proxy`）才能访问；登录用户按角色放行 |
| 归属 | 会话与 Agent 按 `owner_id` 隔离；访问别人的资源返回 404；`viewer` 只读 |
| 转发内容 | 方法、路径、查询串、请求体、状态码、响应头；上游的 4xx/5xx 原样返回 |
| 路径改写 | `strip_prefix`（默认去前缀）+ `add_prefix`（默认空，可再加一层） |
| 方法限制 | `methods` 只开放指定方法，其余返回 405 |
| 不转发 | 逐跳头（`connection`、`transfer-encoding` 等）、`host`、`content-length`、网关的 `Authorization`，以及 WS 的 `ticket` 参数 |
| 注入 | 网关自动加 `X-Gateway-Proxy: 1`；路由配置里的 `headers` 会合并进上游请求 |
| 流式 | 响应边收边转发，SSE 与大文件不会被整体缓冲 |
| 观测 | 响应头 `X-Proxy-Upstream` 标出本次使用的上游 |
| 超时 | `GATEWAY_PROXY_TIMEOUT`，默认 30 秒；读超时不重试、不冷却 |
| 错误 | `502 upstream_unreachable`、`504 upstream_timeout`、`508 proxy_loop` |

代理 WS 的错误事件与会话流同形状，只是没有 `session_id`（不是会话级连接）：

```json
{"type":"error","status":502,"error":{"code":"upstream_unreachable","message":"无法连接上游 http://127.0.0.1:9001 的 WebSocket。"}}
```

### 2.9 认证（`GATEWAY_AUTH_MODE=local`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/login` | 登录页（HTML；legacy 模式返回 404） |
| POST | `/login` | `application/x-www-form-urlencoded`：`username`、`password`、可选 `next`；成功 303 + `Set-Cookie: ydd_session`，失败 303 到 `/login?error=invalid|locked` |
| POST | `/logout` | 吊销当前会话并清 Cookie（需登录；Cookie 认证时校验 CSRF 来源） |
| GET | `/auth/status` | `{"auth_mode","authenticated","user"}`，无需登录 |
| GET | `/api-keys` | 列出 API Key（管理员；不含明文，含 `days_left`、`allowed_cidrs`） |
| POST | `/api-keys` | `{"name","scopes","expires_in_days","allowed_cidrs"}` → 一次性明文；默认 180 天，`0` 表示永不过期 |
| PATCH | `/api-keys/{key_id}` | `{"name"?, "allowed_cidrs"?}`（管理员；scopes 需重建） |
| DELETE | `/api-keys/{key_id}` | 撤销（管理员）；不存在或已撤销 → 404 |
| GET | `/users` | 用户列表（管理员） |
| POST | `/users` | `{"username","password","role"}`（管理员；密码策略见 README） |
| PATCH | `/users/{user_id}` | `{"role"?, "status"?}`；最后一个管理员被保护 → 409 `last_admin` |
| POST | `/users/{user_id}/reset-password` | `{"password"}` → 重置并吊销该用户会话 |
| DELETE | `/users/{user_id}/sessions` | 吊销该用户全部会话 |
| POST | `/auth/password` | `{"current_password","new_password"}`，仅登录会话可用；保留当前会话 |

API Key 认证：`Authorization: Bearer ydd_<id>_<secret>`，按资源检查 scope：
`sessions` / `agents` / `audit` / `proxy`。缺少 scope → `403 forbidden`；Key 不能管理 Key。

登录后的浏览器请求自动携带 Cookie；脚本仍可用 `Authorization: Bearer <GATEWAY_TOKEN>`（映射为管理员）。
Cookie 认证的非幂等请求（POST/PUT/PATCH/DELETE）需要同源的 `Origin` 或 `Sec-Fetch-Site`，否则 `403 csrf_rejected`。

### 2.10 运行指标

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/metrics` | Prometheus 文本；仅管理员会话或带 `metrics` scope 的 API Key |

包含请求计数/耗时、WS 连接数、登录失败数、审计失败数、运行时长与版本。

## 3. WebSocket 协议

### 3.1 连接

```text
ws://127.0.0.1:8000/sessions/{session_id}/stream                # 携带 Authorization 头
ws://127.0.0.1:8000/sessions/{session_id}/stream?ticket=<票据>   # 浏览器路径
```

会话必须先用 HTTP 创建。准入规则：

```
GATEWAY_TOKEN 为空        → 直接放行（不看票据）
有 Authorization 头且正确 → 放行
否则看 ?ticket=           → 票据有效且属于该会话才放行，取出即作废
否则                      → close 1008
```

被反向代理的前缀是另一套准入：`Authorization` 头，或 `/proxy-ticket` 为**那条路径**签发的票据（见 [2.8](#28-反向代理前缀按-routesjson-动态注册)）。

### 3.2 请求帧

必须是**文本帧**，内容为单个 JSON 对象（与 `chat` 请求体相同）：

```json
{"content": "你好", "request_id": "ws-001"}
```

- `request_id` 可省略，服务端生成。
- 二进制帧 → `close 1003`；单帧 > 64 KiB → `close 1009`。
- 一个连接可以连续发送多条；服务端**串行**处理，建议收到 `done` / `error` 后再发下一条。
- 未处理的消息最多缓存 8 条，超过 → `close 1008`。

### 3.3 事件

| `type` | 字段 | 说明 |
| --- | --- | --- |
| `start` | `session_id`、`request_id`、`run_id`、`context` | 已保存用户输入，开始调用模型 |
| `delta` | `content` | 增量文本，需追加显示 |
| `done` | `reply`、`replayed`、`context` | 完整回答已提交入库 |
| `error` | `status`、`error.code`、`error.message` | 本轮失败；已收到的 `delta` 不构成完整结果 |

```json
{"type":"start","session_id":"c300...","request_id":"ws-001","run_id":"8fc9...","context":{"trimmed_messages":0,"estimated_tokens":63,"budget_tokens":6016}}
{"type":"delta","session_id":"c300...","request_id":"ws-001","run_id":"8fc9...","content":"[演示模式]"}
{"type":"done","session_id":"c300...","request_id":"ws-001","run_id":"8fc9...","reply":"[演示模式] 收到：你好","replayed":false,"context":{"trimmed_messages":0,"estimated_tokens":63,"budget_tokens":6016}}
{"type":"error","status":502,"session_id":"c300...","request_id":"ws-001","run_id":"8fc9...","error":{"code":"model_http_error","message":"模型服务返回 HTTP 500，请检查服务地址和模型名称。"}}
```

- `delta.content` 是增量，`done.reply` 是全文，**不要**两者拼接。
- 重放成功结果时只有 `done`（`replayed: true`），没有 `start` / `delta` / `context`。
- 所有 `error` 事件形状一致（见 [1.1](#11-错误响应两种载体同一种内层结构)）。以下情况字段会更少：
  帧无法解析时没有 `request_id`；`413` 超预算时没有 `run_id`（这一轮没写运行记录）。

### 3.4 关闭码

| 码 | 含义 | 触发 |
| --- | --- | --- |
| 1003 | 只支持文本帧 | 收到二进制帧 |
| 1008 | 策略性拒绝 | 鉴权失败、会话不存在、待处理队列满 |
| 1009 | 帧过大 | 文本帧 > 64 KiB |
| 1011 | 服务端内部错误 | 未预期异常：先发一条 `500 internal_error` 事件，再关闭；日志只记异常类型名 |

客户端断开不需要协商：服务端会把正在执行的一轮标记为 `interrupted`。

## 4. 错误码全表

| HTTP | `error.code` | 触发条件 | 客户端动作 |
| --- | --- | --- | --- |
| 401 | `unauthorized` | 缺少或错误的 `Authorization` | 检查 token（注意它和模型 API key 是两回事） |
| 404 | `agent_not_found` | 创建会话时 `agent_id` 不存在；或查询/修改的 Agent 不存在 | 先创建 Agent |
| 404 | `session_not_found` | 会话不存在（含已删除） | 重新创建会话 |
| 404 | `request_not_found` | 该 `request_id` 尚未落地 | 稍后重试查询，或确认是否真的发出 |
| 404 | `run_not_found` | 运行 ID 不存在 | 检查 ID |
| 404 | `route_not_found` | 申请代理票据的路径没有配置反向代理 | 检查 `routes.json` 前缀 |
| 409 | `request_conflict` | 同一 `request_id` 用于不同内容 | 需要新一轮生成就换编号 |
| 409 | `request_not_completed` | 同一 `request_id` 的上一轮失败/中断 | 查询记录确认后换编号重试 |
| 409 | `session_busy` | 会话正在生成，却要删除或清空 | 等这一轮结束 |
| 409 | `agent_in_use` | 删除仍有会话的 Agent | 先删会话 |
| 409 | `default_agent` | 试图删除默认助手 | 默认助手不可删 |
| 409 | `interrupted` | 本轮因断连/停机中断（出现在运行记录里） | 换编号重试 |
| 413 | `context_too_large` | 系统提示词 + 当前提问超出上下文预算 | 缩短输入或调大 `MODEL_CONTEXT_TOKENS` |
| 413 | `body_too_large` | 请求体超过 `GATEWAY_MAX_BODY_BYTES`（代理前缀不适用） | 缩小请求体 |
| 429 | `rate_limited` | 该用户每分钟聊天次数超限 | 降低频率或调大 `GATEWAY_CHAT_PER_MINUTE` |
| 429 | `too_many_concurrent` | 该用户同时进行的聊天轮数超限 | 等上一轮结束或调大 `GATEWAY_CHAT_CONCURRENCY` |
| 422 | `invalid_request` | 参数类型/长度/未知字段；`PATCH` 空请求体；分页参数越界 | 对照 `/docs` 修正 |
| 500 | `internal_error` | 未预期的内部异常 | 查服务日志 |
| 502 | `empty_reply` | 模型返回后拼接为空 | 检查模型 |
| 502 | `model_auth_error` | 上游 401/403 | 检查 `MODEL_API_KEY` 与权限 |
| 502 | `model_http_error` | 上游其他非 2xx（含上下文超限的 400） | 检查地址/模型名；本机模型注意系统代理 |
| 502 | `model_protocol_error` | SSE 无法解析、缺 `choices`、`delta` 结构错误 | 确认服务兼容 Chat Completions |
| 502 | `model_stream_error` | 流中出现 `error` 事件 | 看上游错误 |
| 502 | `model_refused` | 模型返回 `refusal` | 调整输入 |
| 502 | `model_tools_unsupported` | 模型返回工具调用（本版不执行） | 换纯文本模型或改写提示词 |
| 502 | `model_incomplete` | `finish_reason != stop`，或缺 `[DONE]` | 检查上游是否中途断开/达到上限 |
| 502 | `model_empty_reply` | 上游没有返回任何非空文本 | 检查模型是否支持文本对话 |
| 502 | `model_connection_error` | 连接失败/被中断 | 检查 `MODEL_BASE_URL`、网络、代理设置 |
| 503 | `model_rate_limited` | 上游 429 | 降低频率或检查额度 |
| 504 | `model_timeout` | 上游超时（连接/读/整轮） | 检查服务或调大 `MODEL_TIMEOUT_SECONDS` |
| 502 | `upstream_unreachable` | 反向代理连不上上游（HTTP 或 WS） | 确认上游服务在运行、端口与地址正确 |
| 504 | `upstream_timeout` | 反向代理访问上游超时 | 检查上游性能，或调大 `GATEWAY_PROXY_TIMEOUT` |
| 508 | `proxy_loop` | 上游又指回了网关自己（请求带着 `X-Gateway-Proxy`） | 修正 `routes.json` 里的目标地址 |

## 5. `context` 对象

| 字段 | 含义 |
| --- | --- |
| `trimmed_messages` | 本次被省略的历史消息条数；`0` 表示全部保留 |
| `estimated_tokens` | 估算的提示词 token 总数（含系统提示词、省略提示、历史、当前提问） |
| `budget_tokens` | 本次允许的提示词上限 = `MODEL_CONTEXT_TOKENS − MODEL_MAX_TOKENS − 128` |

只要服务端**真的调用了模型**，`estimated_tokens <= budget_tokens` 恒成立（有单测保障）。

## 6. 契约稳定性

**稳定**（改动视为破坏性变更）：

- 路径与方法、请求字段名与含义；
- 事件 `type` 取值、`reply`、`replayed`、`error.code`；
- 运行状态取值 `running` / `completed` / `failed` / `interrupted`；
- `request_id` 的去重语义。

**允许新增**（客户端应忽略未知字段）：

- 响应与事件里的新字段（例如 `context` 就是后加的）；
- 新的 `error.code`；
- `/health` 的新字段（例如 `auth_required`）。

本项目没有版本协商（没有 `/v2`）。破坏性变更会同时更新 README、本文与测试。

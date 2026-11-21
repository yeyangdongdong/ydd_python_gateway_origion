# 提案：拆分路由 + 归属检查下沉（P1-7）

- **状态**：**已实现**（A `8e1e012`、B `8a33fbc`、C 本次提交）
- **目标版本**：下一轮重构
- **预计工作量**：1.5–2 天（含测试与文档），建议 3 笔提交
- **关联**：[architecture.md](../architecture.md)、[development.md](../development.md)、ADR-018（归属用 404）

---

## 1. 背景

当前 `gateway/app.py` 已经 920 行，同时承担：

- FastAPI 应用创建、生命周期、异常处理、中间件装配；
- 认证（session / legacy token / API Key）、CSRF、登录限速；
- 资源归属检查与角色判断；
- 用户管理、API Key 管理、审计查询、代理状态；
- Agent / 会话 / 消息 / 运行 / 票据 / 聊天 / WebSocket；
- 反向代理动态路由注册。

带来三个具体问题：

1. **改动成本高**：改一个会话接口要通读整个文件；新同学找不到“新接口该写在哪”。
2. **安全边界靠调用方自觉**：归属检查 `ensure_session_access` 只在 HTTP 层调用；
   `ChatService.stream` 只拿 `session_id`，不知道 owner。将来接 OpenAI 兼容入口、
   定时任务或内部脚本直接调用 service，就可能绕过隔离。
3. **难单测**：`owns` / `session_for` / `require_admin` 都是 `create_app` 内部的闭包，
   只能通过整个 app 测；路由注册与业务对象强耦合。

这次重构只解决“结构 + 归属下沉”，**不改变任何对外行为**。

## 2. 目标与非目标

### 目标

- `app.py` 只做装配，降到约 150 行；
- 按资源拆 router，新增接口有明确落点；
- 归属检查收敛到 **storage 一处实现**，`ChatService` 显式接收 `owner_id`；
- 现有 103 个测试**不改断言**即可通过；
- 代码仍然“容易理解”：不引入 DI 框架、不做多层抽象。

### 非目标

- 不改 URL、状态码、错误码、JSON 形状；
- 不改日志/审计字段；
- 不拆 `store` 的 SQLite 连接模型（另一项 P1-9）；
- 不做用户/API Key 的端点级权限矩阵（保持现有资源级 scope + 角色）。

## 3. 目标结构

```text
gateway/
├── app.py                  # create_app：装配、lifespan、异常处理、中间件（≤150 行）
├── container.py            # AppContext：一次装配好的依赖集合（约 40 行）
├── api/
│   ├── __init__.py         # build_routers(ctx) -> list[APIRouter]
│   ├── deps.py             # 鉴权依赖、principal、admin、owner 检查、审计包装（约 140 行）
│   ├── auth.py             # /login /logout /auth/status /auth/password（约 150 行）
│   ├── users.py            # /users...（约 160 行）
│   ├── agents.py           # /agents...（约 90 行）
│   ├── sessions.py         # /sessions CRUD、messages、requests、runs、ticket（约 230 行）
│   ├── chat.py             # POST /sessions/{id}/chat、WS /stream（约 180 行）
│   └── admin.py            # /api-keys、/audit、/proxy-status（约 180 行）
└── proxy_routes.py         # 按 routes.json 动态注册 HTTP/WS 转发（约 70 行）
```

依赖方向（无环）：

```text
app.py ──> container.py ──> storage/auth/proxy/limits
   │
   └──> api/*.py ──> api/deps.py ──> container.py
                         │
                         └──> errors/storage/auth（类型与工具）
```

- `container.py` **不 import** `api/`；
- `api/deps.py` 只在 `TYPE_CHECKING` 下引用具体类型，运行时读 `request.app.state.ctx`；
- 每个 router 用工厂函数 `build_xxx_router(ctx)` 创建，避免模块级全局状态。

## 4. 关键设计

### 4.1 AppContext（替代散落的 app.state）

```python
@dataclass
class AppContext:
    settings: Settings
    store: Store
    audit: AuditTrail
    tickets: TicketStore
    routes: list[ProxyRoute]
    login_limiter: LoginLimiter
    chat_limiter: UserLimiter
    proxy: Proxy | None = None      # lifespan 中创建
    chat: ChatService | None = None # lifespan 中创建
```

- `create_app` 同步创建 ctx 并挂到 `app.state.ctx`；
- `lifespan` 只负责 `store.initialize`、创建 provider/chat/proxy、关闭资源；
- 测试仍可用 `TestClient(create_app(...))`，不需要改测试写法。

### 4.2 deps.py：认证与归属的唯一入口

```python
def get_context(request: Request) -> AppContext:
    return request.app.state.ctx

async def require_auth(request, authorization=Header(None)) -> None:
    ctx = get_context(request)
    principal = await resolve_principal(ctx, request, authorization)
    ...  # CSRF、viewer 只读、API Key scope
    request.state.principal = principal

def require_admin(request) -> None: ...
def owner_scope(principal) -> str | None:
    """admin / api_key -> None（不限制）；其他 -> user_id"""
def owns(principal, owner_id) -> bool: ...
async def owned_session(ctx, request, session_id) -> dict:
    session = await run_in_threadpool(ctx.store.get_owned_session,
                                      session_id, owner_scope(request.state.principal))
    return session
```

`deps.py` 成为唯一可以回答"这个请求能看什么"的地方；router 里只调用 `Depends(require_auth)`
和 `owned_session`。

### 4.3 归属下沉：storage + service

**storage** 新增一个方法（不改变现有 `get_session` 的语义）：

```python
def get_owned_session(self, session_id: str, owner_id: str | None) -> dict:
    """owner_id 为 None 表示不限制（管理员/API Key）；否则 owner 不匹配按 404 处理。"""
```

- 404 语义与现在一致，不泄露资源是否存在；
- 其他会话级查询（messages/runs）先拿到已校验的 session，再执行；
- `delete_session` 等写操作仍在 storage 内，但入口先经过 `get_owned_session`。

**service**：

```python
async def stream(self, session_id: str, content: str, request_id: str | None,
                 *, owner_id: str | None) -> AsyncIterator[dict]:
```

- `owner_id` 是**必填关键字参数**（无默认值），新入口忘记传会直接 TypeError；
- 内部把 `store.get_session` 换成 `store.get_owned_session`；
- HTTP 聊天、WS 聊天、未来的 OpenAI 兼容入口都传 `owner_scope(principal)`。

### 4.4 入口与检查点对照

| 入口 | 归属检查 | owner_id |
| --- | --- | --- |
| `GET/PATCH/DELETE /sessions/{id}` | `owned_session` | 是 |
| `GET/DELETE /sessions/{id}/messages` | `owned_session` | 是 |
| `POST /sessions/{id}/chat` | `owned_session` + service 内再校验 | 是 |
| `WS /sessions/{id}/stream` | 连接时 + 每轮 service 内 | 是 |
| `POST /sessions/{id}/ticket` | `owned_session` | 是 |
| `GET /sessions/{id}/requests/{rid}`、`GET /runs/{id}` | 先查 session 再校验 | 是 |
| `GET/PATCH/DELETE /agents/{id}` | `owns(agent.owner_id)` | — |
| 代理前缀 | 不涉及会话；按 `required_scope` / 角色 | — |

## 5. 行为不变清单（回归断言）

- 所有 URL、方法、状态码不变；
- 越权访问别人的会话仍是 `404 session_not_found`；
- `viewer` 写操作仍是 `403 forbidden`，`/logout` 例外；
- 默认助手 `owner_id = NULL`：所有人可用，仅管理员可改/删；
- 审计字段（action/target/actor/ip/trace_id）不变；
- 访问日志字段不变；
- OpenAPI 文档路径与 operationId 不冲突（拆分后每个函数名唯一）。

## 6. 实施步骤（每步可独立提交/回滚）

### A. 骨架搬迁（纯移动，不改逻辑）

1. 新增 `container.py`、`api/deps.py`、`api/*.py`、`proxy_routes.py`；
2. 把路由函数逐段搬过去，闭包改为 `build_xxx_router(ctx)`；
3. `app.py` 只保留 apply middleware、exception handler、include_router、lifespan；
4. 保持所有断言不变。

**验收**：103 测试全绿；`app.py` ≤ 200 行；`git diff` 以移动为主。

### B. 归属下沉

1. `store.get_owned_session`；
2. `ChatService.stream` 增加必填 `owner_id`；
3. HTTP/WS 入口改传 `owner_scope(principal)`；
4. 删除 `app.py` 内旧的 `ensure_session_access` / `session_for`（如有重复）；
5. 新增测试：
   - 直接调用 `ChatService.stream` 且 owner 不匹配 → `404`；
   - owner 匹配 → 正常；
   - `owner_id=None`（管理员）→ 正常；
   - 其他成员仍拿不到别人的会话（复用现有 HTTP 用例即可）。

**验收**：106 测试全绿。

### C. 收尾

1. 删除 `app.py` 中的死代码与重复 helper；
2. 新增“路由清单”测试：遍历 `app.routes`，断言关键路径存在且方法正确；
3. 更新 `docs/architecture.md`（模块表、请求链路）、`docs/development.md`（代码地图、行数、测试矩阵）、README 代码树；
4. 记录本提案状态为“已实现”。

**验收**：107 测试全绿；文档链接检查通过。

## 7. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 循环导入 | container 不 import api；deps 运行时只读 `app.state` |
| 路由函数重名导致 OpenAPI operationId 冲突 | 每个资源一个模块，函数名保持唯一；C 步加路由清单测试 |
| `app.state.ctx` 在 startup 前不可用 | ctx 在 `create_app` 同步创建；lifespan 只补 chat/proxy |
| 测试直接访问 `app.state.chat` | 现有测试都在 TestClient 上下文内；若发现直接访问，改为 `app.state.ctx.chat` |
| 移动过程中漏改 import | 每一步跑全量测试；A 步不改逻辑，失败即回滚 |
| 代码更“抽象”反而难读 | 工厂函数 + 显式 ctx，不用 DI 框架、不引入基类；每个 router 仍是普通 FastAPI 写法 |

## 8. 工作量拆解

| 步骤 | 内容 | 预估 |
| --- | --- | --- |
| A | 建包、搬路由、app.py 瘦身 | 0.5–1 天 |
| B | get_owned_session + service owner_id + 测试 | 0.5 天 |
| C | 清理、路由清单测试、文档 | 0.5 天 |
| 合计 | | **1.5–2 天** |

## 9. 实施记录

| 步骤 | 提交 | 结果 |
| --- | --- | --- |
| A 骨架搬迁 | `8e1e012` | `app.py` 930 → 122 行；新增 `container.py`、`api/`（7 个模块）、`proxy_routes.py`；103 测试全绿 |
| B 归属下沉 | `8a33fbc` | 新增 `Store.get_owned_session`；`ChatService.stream(..., *, owner_id)`；HTTP/WS 全部传 `owner_scope`；新增直接调用 service 的归属测试；104 测试全绿 |
| C 收尾 | 本次 | 路由清单测试、文档与 ADR-019；105 测试全绿 |

最终结构：`gateway/api/deps.py`（255 行）是唯一鉴权/归属入口；
`app.py`（122 行）只做装配；行为与 URL 未变。

## 10. 决定（评审后）

1. 包名采用 `gateway/api/`；
2. `owner_id=None` 表示管理员 / 服务级 API Key 不限制；
3. `net.py`、`limits.py` 保持原位，不额外收进 `security/`。

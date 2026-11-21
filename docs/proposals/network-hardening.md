# 提案：API Key 过期/IP 白名单、CORS、WebSocket 心跳

- **状态**：**已实现**（四项，见实施记录）
- **范围**：三项互相独立，可分别决定做/不做
- **关联**：[api.md](../api.md)、[architecture.md](../architecture.md)、ADR-017（API Key）、ADR-022（CSP/请求体）

---

## 0. 一页概览

| 项 | 用户价值 | 成本 | 风险 | 建议 |
| --- | --- | --- | --- | --- |
| API Key 默认过期 | 高（减少长期泄露面） | 0.5 天 | 低（老 Key 行为不变） | **做**，默认 90 天 |
| API Key IP 白名单 | 中（限制 Key 被拷走后滥用） | 1–1.5 天 | 中（NAT/代理配置错会锁死服务） | **做**，默认关闭、按 Key 配置 |
| CORS | 中（外部 Web 前端能直连） | 0.5–1 天 | 中（配置错会放大跨站面） | **做**，默认关闭、精确 origin |
| WS 心跳 | 低（现有默认已工作） | 0.5 天 | 低 | **只做显式配置**，不做应用层心跳 |

三项合计约 2.5–3 天。建议顺序：API Key 过期 → CORS → IP 白名单 → WS 配置。

---

## 1. API Key 默认过期

### 1.1 现状

- `api_keys` 表已有 `expires_at`，创建接口 `expires_in_days` 可选；
- 不传就**永不过期**；鉴权时只检查 `revoked_at` 与 `expires_at`；
- 列表接口会返回 `expires_at`，但没有“还剩多少天”的提示。

### 1.2 方案

**默认过期**

- 新增 `GATEWAY_API_KEY_DEFAULT_DAYS`（默认 `90`，`0` 表示默认永不过期）；
- `POST /api-keys` 的 `expires_in_days` 语义：
  - 不传 → 用服务端默认值；
  - `0` → 明确“永不过期”（需要把现在的 `ge=1` 改成 `ge=0`）；
  - `N` → N 天后过期。
- 过期 Key 的鉴权结果与撤销一致：**401 `unauthorized`**，不泄露“Key 存在但过期”；
- `GET /api-keys` 返回 `expires_at` + `days_left`（`null` 表示永不过期）；
- 创建审计 `api_key.create` 的 details 增加 `expires_at`。

**不增加“过期提醒任务”**：被动过期已经满足安全目标；定时扫描只增加审计噪声。
若以后需要，可在 `/api-keys` 列表里显示 `days_left <= 14` 的告警，不动后台任务。

### 1.3 兼容性

- 旧 Key 的 `expires_at` 仍为 `NULL`，行为不变；
- 只有“新建且未指定”的 Key 才套用默认值；
- 如果希望旧 Key 也逐步过期，需要管理员重建（不在本次范围）。

### 1.4 测试

- 不传 `expires_in_days` → `expires_at ≈ now + 默认天数`；
- `0` → `expires_at = null`；
- `N` → 精确 N 天；
- 过期 Key → 401；撤销 Key → 401；列表 `days_left` 正确。

**工作量：0.5 天。**

---

## 2. API Key IP 白名单

### 2.1 现状与风险

- Key 一旦泄露，任何来源都能用；
- 现有鉴权入口 `api_key_from_header(ctx, authorization)` 不接收请求上下文，拿不到客户端 IP。

### 2.2 方案

**数据**

迁移 v6：`api_keys` 增加 `allowed_cidrs TEXT`（JSON 数组，默认 `"[]"`）。
空数组 = 不限制（保持老 Key 行为）。

**创建接口**

```json
{"name": "报表服务", "scopes": ["sessions"],
 "expires_in_days": 90,
 "allowed_cidrs": ["192.168.1.0/24", "10.0.0.5/32"]}
```

- 用标准库 `ipaddress.ip_network(strict=False)` 校验，最多 20 条；
- 只接受 IP/CIDR，不接受域名（避免 DNS 解析与漂移）；
- 存储规范化字符串（`192.168.1.0/24`）。

**鉴权**

- `authenticate()` 把客户端 IP 传进 `api_key_from_header`；
  HTTP 用现有 `request_ip()`（信任回环的 X-Forwarded-For），WS 用同一套逻辑；
- IPv4-mapped IPv6（`::ffff:192.168.1.5`）先归一成 IPv4 再匹配；
- 不匹配 → **401**（不是 403，避免确认 Key 有效）；
- 记录一次 WARN 日志和指标 `gateway_api_key_ip_rejections_total`；
  是否写审计：默认**不写**（避免被刷日志），需要时加配置。

**管理接口**

- 当前只有创建/撤销；建议加 `PATCH /api-keys/{id}`，只允许改 `name` 与 `allowed_cidrs`；
  scopes 变更仍要求重建（避免一把 Key 权限悄悄变大）；
- 如果不想加 PATCH，就“撤销 + 重建”，本提案按**加 PATCH**设计。

### 2.3 注意事项

- **NAT/代理**：网关在 Caddy 后面时，`request_ip()` 已经取真实客户端 IP；
  若前面还有一层负载均衡，需要它设置 `X-Forwarded-For`，否则白名单会看到代理 IP；
- **本机脚本**：`127.0.0.1/32` 与 `::1/128` 要显式写，不能靠 `localhost`；
- **测试**：`TestClient` 的对端是 `testclient`（非 IP），不能直接做集成测试；
  方案是把匹配逻辑抽成纯函数 `ip_allowed(ip, cidrs)` 做单测，
  再用一个带 `client=("127.0.0.1", 1234)` 的自定义 ASGI scope 做一次集成测试。

### 2.4 决策点

1. 不匹配返回 401（推荐）还是 403？
2. 默认不写审计、只记 WARN + 指标，是否接受？
3. 允许 `PATCH` 改白名单，还是只支持重建？

**工作量：1–1.5 天（含迁移、接口、测试）。**

---

## 3. CORS

### 3.1 现状

- 没有 CORS 中间件；浏览器只能同源访问；
- 外部 Web 前端要直连网关时，预检请求会被 405/401 挡住；
- Cookie 认证还叠加 CSRF 校验（跨站 Origin 会被拒绝）。

### 3.2 方案

使用 Starlette 内置 `CORSMiddleware`，但**按路径生效**：

- 配置 `GATEWAY_CORS_ORIGINS`（逗号分隔，精确 origin，默认空 = 关闭）；
- 只对网关自己的接口生效；**代理前缀不加 CORS**，避免和上游的 CORS 头重复；
- 允许方法：`GET/POST/PATCH/DELETE/OPTIONS`；
- 允许头：`Authorization, Content-Type, X-Request-ID`；
- 暴露头：`X-Request-ID, X-Proxy-Upstream`；
- `allow_credentials=True`（Cookie 会话需要），因此**禁止 `*`**；
- 预检 `max_age=600`。

配置校验：

- 只接受 `http://` / `https://` 的 scheme+host+port，拒绝 path/query/`*`；
- 末尾斜杠自动去掉；重复项去重。

### 3.3 与 CSRF 的关系（关键）

Cookie 认证的非幂等请求现在会校验 `Origin == Host`，跨源前端会被 403。
两种选择：

- **A（推荐）**：把配置里的 CORS origin 视为可信来源，`enforce_csrf` 放行这些 origin；
  浏览器侧仍受 CORS 限制（只有白名单 origin 能读响应），CSRF 面没有扩大；
- B：CORS 只支持 Bearer/API Key，Cookie 会话不跨源。实现更简单，但外部前端不能用登录态。

推荐 A，并在 README 明确“CORS origin 同时是 CSRF 白名单”。

### 3.4 测试

- 未配置 → 响应没有 `Access-Control-Allow-Origin`；
- 配置 `http://localhost:5173` → 预检 204，带允许头；实际请求带 origin 头；
- 未列入的 origin → 无 CORS 头；
- 配置 `*` 或带 path 的 origin → 启动失败；
- 代理前缀的响应不受网关 CORS 影响；
- Cookie 会话从白名单 origin 发 POST → 不被 CSRF 拒绝；非白名单 origin → 403。

**工作量：0.5–1 天。**

---

## 4. WebSocket 心跳

### 4.1 现状（重要）

心跳**大部分已经存在**，只是没有显式配置：

- 服务端 uvicorn 默认 `ws_ping_interval=20s`、`ws_ping_timeout=20s`，会主动 ping 浏览器；
- 代理连接上游用 `websockets.connect`，默认同样每 20s ping 上游；
- 浏览器/客户端协议层自动回 pong，所以“死连接”在约 20–40s 内会被发现；
- 应用层没有 `{"type":"ping"}` 消息，也**不需要**：浏览器无法用 JS 收到协议层 ping，应用层心跳只会增加复杂度。

### 4.2 真正的缺口

1. 这些默认值没有写进配置和文档，运维不知道；
2. 无法按部署环境调整（例如经过会掐长连接的防火墙）；
3. 没有验证过“客户端不回 pong 时多久断开”。

### 4.3 方案

**推荐：只做显式配置（A）**

- 新增 `GATEWAY_WS_PING_INTERVAL`（默认 20）、`GATEWAY_WS_PING_TIMEOUT`（默认 20）；
- `run.py` 传给 uvicorn；
- 代理的 `websockets.connect(...)` 传同样两个值；
- `0` 表示关闭（不推荐，文档写明风险）；
- 不新增应用层 ping/pong、不改前端。

**可选：B 空闲超时（不建议现在做）**

- 聊天 WS 如果长时间既无客户端消息也无生成，可主动关闭；
- 但“生成中”与“空闲”需要额外状态机，且慢模型容易被误杀；
- 收益低、误伤风险高，暂不做。

**可选：C 应用层心跳（不推荐）**

- 需要改前端协议、处理重连与去重，和协议层 ping 重复；
- 只在“中间设备过滤 WebSocket 控制帧”时才需要，当前部署（Caddy）不需要。

### 4.4 验证方法

- 单测：mock `uvicorn.run` 断言 ping 参数被传入；mock `websockets.connect` 断言代理参数；
- 手工：起真实服务，用脚本建立 WS 后**不回复 pong**（直接停读），观察服务端在
  `ping_interval + ping_timeout` 左右关闭连接，并在日志里看到断开；
- 正常聊天链路回归（现有测试已覆盖）。

**工作量：0.5 天（配置 + 代理参数 + 测试 + 文档）。**

---

## 5. 组合与顺序

| 顺序 | 项 | 依赖 | 说明 |
| --- | --- | --- | --- |
| 1 | API Key 默认过期 | 无 | 最小改动，先降低长期 Key 风险 |
| 2 | CORS | 无（若选 A 需改 CSRF） | 独立，默认关闭 |
| 3 | API Key IP 白名单 | 迁移 v6；需要把请求 IP 传进鉴权 | 风险最高，放最后 |
| 4 | WS 心跳配置 | 无 | 只改配置与代理参数 |

如果只做一项：**默认过期**性价比最高。
如果只做两项：**默认过期 + WS 显式配置**（都是低风险）。
CORS 与 IP 白名单都涉及“谁能访问”的边界，建议一起评审后再动。

## 6. 待你决定

（评审后已定：默认 180 天；IP 不匹配 401；CORS origin 同时放行 CSRF；WS 只做显式配置；四项全做。）

## 7. 实施记录

| 项 | 提交 | 结果 |
| --- | --- | --- |
| API Key 默认过期（180 天） | `4b1ea9f` | 不传按默认；`0` 永不过期；列表返回 `days_left` |
| CORS（精确 origin + CSRF 联动） | `3ad1d40` | 默认关闭；代理前缀不加 CORS；配置 origin 同时放行 CSRF |
| API Key IP 白名单 | `7d1d94c` | 迁移 v6；`allowed_cidrs`；`PATCH /api-keys/{id}`；401 + 指标 |
| WS 心跳显式配置 | 随本文档提交 | `GATEWAY_WS_PING_INTERVAL/TIMEOUT`，uvicorn 与代理上游共用 |

全量测试从 120 增至 132，全部通过。

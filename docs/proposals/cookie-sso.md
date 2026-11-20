# 浏览器 SSO：Cookie 登录

**状态：未实现（提案）**

## 1. 设计

**问题**：现在浏览器连被代理的 WebSocket 需要页面自己调 `POST /proxy-ticket`。
这对"内部工具集合"不友好——每个工具都得改前端，而且用户每次打开都要经过一段取票逻辑。

**目标**：登录一次，所有同源页面（含被代理的应用）自动携带凭证；页面零改动。

| 方案 | 体验 | 代价 |
| --- | --- | --- |
| Cookie 登录（推荐） | 一次登录、可登出、页面自动携带 | 需要登录页 + CSRF 防护 + 签名 |
| Basic Auth | 浏览器弹窗、页面零改动 | 无法登出、凭据常驻、体验差 |
| OAuth / OIDC | 最规范 | 需要 IdP、回调、会话管理，体量翻倍 |

**定位**：它替换的是"每页自己取票"的临时方案；Bearer 保留给脚本与 CLI。
仍然是**单用户单 token**，只是换了载体，**不做多用户隔离**（那需要独立的鉴权模型）。

## 2. 技术方案

### 2.1 新增配置

```dotenv
GATEWAY_COOKIE_SECRET=至少 32 字节的随机串
GATEWAY_COOKIE_SECURE=false        # 走 HTTPS 时应设为 true
GATEWAY_SESSION_SECONDS=604800     # 7 天
```

`GATEWAY_COOKIE_SECRET` 是硬要求：没有它就不能开启 Cookie 登录（拒绝启动），
避免用 token 派生出一个弱密钥。

### 2.2 Cookie 格式

```text
v1.<expires_epoch>.<base64url(hmac_sha256(secret, "v1|" + expires))>
```

- 属性：`HttpOnly; SameSite=Lax; Path=/; Max-Age=...`，HTTPS 下追加 `Secure`；
- 只存过期时间与签名，不存 token 本身；
- 校验：签名一致 + 未过期；错误一律当未登录。

### 2.3 接口

```text
GET  /login         极简表单（放 web/login.html，与聊天页同一套无构建风格）
POST /login         表单提交 token；成功 303 到 next，失败 401 回表单并提示
POST /logout        清 cookie，303 回 /
GET  /auth/status   {"authenticated": true}（供页面判断，不泄露细节）
```

`next` 只允许**同源相对路径**（`/` 开头且不以 `//` 开头），防开放重定向。

### 2.4 鉴权顺序

```text
HTTP:  Bearer 正确 → 放行
       Cookie 有效 → 放行（非幂等方法额外做 CSRF 校验）
       否则 401

WS:    Authorization 头正确 → 放行
       Cookie 有效 → 放行（浏览器握手自动带 cookie，无需票据）
       否则看 ?ticket= → 核销
       否则 close 1008
```

票据机制保留：它适合"没有登录态、只有 token 的一次性场景"（如分享链接），
与 Cookie 登录并存，不互相替代。

### 2.5 CSRF（Cookie 的必然代价）

- `SameSite=Lax` 挡住跨站表单/脚本的非 GET 请求；
- 非幂等方法（POST/PUT/PATCH/DELETE）额外校验 `Origin` 或 `Sec-Fetch-Site: same-origin`，
  缺失或不匹配 → `403 csrf_rejected`；
- 被代理页面与网关同源，正常 fetch/XHR 天然通过，**不需要改页面**；
- 登录表单本身也要校验来源（或在表单里带一次性 nonce）。

### 2.6 与现有代码的接入点

| 位置 | 改动 |
| --- | --- |
| `gateway/app.py` | `require_auth` 增加 `Request` 参数读 cookie；WS 的 `authorized` 分支加 cookie；新增 `/login` `/logout` `/auth/status` |
| `gateway/signing.py`（新） | `sign(expires)` / `verify(value)`，供 SSO 与亲和 cookie 共用 |
| `web/login.html`（新） | 一个表单 + 一句错误提示 |
| `gateway/config.py` | 三个新配置项 + `__post_init__` 校验（开启时必须给 secret） |
| `RESERVED` | 补 `login` `logout` `auth` |

### 2.7 测试

登录成功/失败；cookie 签名被篡改拒绝；过期拒绝；登出后失效；跨站 POST 被拒（伪造 Origin）；
代理 WS 用 cookie 握手成功；Bearer 仍然可用；`next` 不允许外部 URL。

**成本**：约 150 行（含登录页）+ 8 个测试。

## 3. 边界与风险

- Cookie 泄露等价 token 泄露：靠 `HttpOnly`、较短有效期、可登出缓解；
- 仍然是单用户：多人共用同一 token，只是换了个载体；
- 必须同时上 `SameSite=Lax` 与 Origin 校验，只做一半等于没做；
- 若未来要真正的多用户，应直接上 OIDC，而不是在 Cookie 上叠加用户体系。

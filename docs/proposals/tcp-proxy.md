# 代理非 HTTP 协议（gRPC / TCP）

**状态：未实现（提案，建议不做 gRPC；TCP 按需且优先外置）**

## 1. 设计

**问题**：被代理的服务不一定是 HTTP。常见诉求有两种：gRPC 微服务、以及数据库/Redis 之类的裸 TCP。

**结论先行**：

| 协议 | 可行性 | 结论 |
| --- | --- | --- |
| WebSocket | 已支持 | —— |
| gRPC（HTTP/2 双向流 + trailers） | 很低 | 不做 |
| 任意 TCP（数据库、私有协议） | 中 | 仅在确有需求时做，且建议优先外置 |
| UDP | 很低 | 不做 |

## 2. 为什么 gRPC 不做

gRPC 不是"HTTP/1.1 + 流"，它要求：

- HTTP/2 端到端（含明文 h2c 或 TLS 终止）；
- 请求/响应双向流式（客户端流、服务端流、双向流）；
- trailers 承载状态码与错误信息。

现有网关的转发层是 `httpx` + ASGI/HTTP1.1：

- `httpx` 的流式接口是"上传完再读响应"，不支持真正的双向流；
- uvicorn 的 h2 支持有限（h2c 不可用），无法作为 gRPC 服务端；
- 自己实现需要引入 `h2` 并手写帧级桥接，工作量与风险都远超一个基础版该承担的。

**替代方案**：gRPC 客户端直连上游；需要统一入口就用 Envoy / Traefik / Nginx（它们原生支持 gRPC）。

## 3. TCP 透传（可选模块）

如果确实要让数据库或私有协议走网关，可以在 **L4** 做端口转发：

```json
{
  "tcp_routes": [
    {"listen": 5433, "target": "127.0.0.1:5432",
     "allow_from": ["127.0.0.1"], "idle_timeout_seconds": 300}
  ]
}
```

### 3.1 实现

```text
lifespan 启动：为每条 tcp_route 调 asyncio.start_server(handle, host, listen)
handle(reader, writer):
    校验来源 IP 是否在 allow_from（为空则拒绝，避免误开放）
    upstream_reader, upstream_writer = await asyncio.open_connection(target)
    两个 task 双向 pipe（各 64 KiB 缓冲），任一方向 EOF 就关闭双方
    空闲超过 idle_timeout 主动断开，避免连接泄漏
关闭：server.close() + await wait_closed()
```

### 3.2 必须写明的边界

- **没有应用层身份**：L4 看不到 HTTP 头，`GATEWAY_TOKEN` 与票据都用不上，只能用来源 IP 白名单；
- **不终止 TLS**：客户端要按上游证书直连，SNI 指向网关会导致证书不匹配；
- **端口面变大**：每条路由一个监听端口，需要防火墙与部署配置配合；
- 与现有单进程模型一致：连接数受文件描述符限制，需要上限与告警。

### 3.3 测试

端口转发能通；来源 IP 不在白名单时立即断开；空闲超时断开；上游不可达时干净关闭；
关闭 lifespan 后端口释放（不残留监听）。

**成本**：约 150 行 + 6 个测试。

## 4. 建议

1. **不要**在网关内做 gRPC 转发；
2. TCP 透传只在"必须经过同一个入口、且没有条件上外部代理"时做；
3. 生产环境优先用 Caddy 的 `stream` 模块、Nginx `stream` 或 HAProxy——它们本来就负责 L4，
   而且有成熟的可观测与限流能力。

## 5. 如果未来要做 gRPC

正确的路径不是改这个网关，而是：

- 引入支持 h2c/HTTP2 的 ASGI 服务器（如 hypercorn），并确认其双向流能力；
- 或直接在某条路由上做 `stream` 级转发（h2 帧级别），把它当成独立子系统设计；
- 无论哪条路，都要重新评估鉴权、超时、日志与连接数管理，不能复用现有 HTTP 代理的假设。

# 主动健康检查（探活）

**状态：已实现 v1**（`gateway/proxy.py` 的 `HealthSettings`、`Proxy.start`、`Proxy._probe_once`）。
本文既记录设计取舍，也作为实现说明；后续演进（失败率熔断、按路由独立周期）仍属提案。

## 1. 设计

**问题**：被动冷却只能发现"连不上"。一个"能握手但一直 500、或者慢到超时"的上游，
会被加权轮询一直选中，表现为约 1/N 的请求失败。

**目标**：把这类上游摘掉，并在它恢复后自动放回来。

| 方案 | 好处 | 代价 |
| --- | --- | --- |
| 主动探活 + 被动冷却（选中） | 能发现 5xx、恢复及时；实现简单 | 需要上游提供轻量探活端点；会进上游日志 |
| 纯失败率熔断 | 不需要端点 | 需要滑动窗口；恢复慢；实现更重 |
| 交给外部负载均衡 | 成熟 | 多一层组件，网关失去视角 |

**决策**：探活与冷却并存，各管一端——冷却负责"快速摘除"，探活负责"恢复判定 + 启动时剔除"。

## 2. 技术方案

### 2.1 配置

写在 `routes.json` 顶层，作为所有路由的默认值；每条路由可以用同名字段覆盖：

```json
{
  "health": {
    "enabled": false,
    "path": "/health",
    "method": "GET",
    "interval_seconds": 10,
    "timeout_seconds": 2,
    "failures_to_down": 3,
    "successes_to_up": 2
  },
  "routes": [
    {"prefix": "/finance/", "target": "http://127.0.0.1:9001"},
    {"prefix": "/report/", "target": "http://127.0.0.1:9002",
     "health": {"path": "/livez", "interval_seconds": 5}}
  ]
}
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 总开关；关闭时完全不起后台任务 |
| `path` | `/health` | 探活路径 |
| `method` | `GET` | `GET` 或 `HEAD`；默认 GET，因为不少简单服务不实现 HEAD（回 501） |
| `interval_seconds` | `10` | 探活周期 |
| `timeout_seconds` | `2` | 单次探活超时 |
| `failures_to_down` | `3` | 连续失败多少次判为不健康 |
| `successes_to_up` | `2` | 连续成功多少次恢复 |

校验：未知字段直接报错（防拼写错误悄悄失效）；`interval/timeout > 0`；阈值是正整数；
路由级覆盖先与全局合并再校验。

### 2.2 判定规则

```text
HTTP 2xx/3xx        → 存活
HTTP 4xx            → 存活（探活路径可能要鉴权、或路径写错，但服务本身回了响应）
HTTP 405 / 501      → 存活（方法不被支持，不代表服务坏了）
HTTP 5xx            → 失败
连接失败 / 超时      → 失败
```

这条规则是踩过坑之后定的：最初默认 `HEAD` 且"非 2xx 即失败"，结果一个只实现 GET 的
正常上游因为 501 被判死（测试 `test_health_check_treats_4xx_as_alive` 锁住了这个行为）。

### 2.3 调度

- **单个后台协程**：`Proxy.start()` 在 lifespan 里创建，间隔取所有启用探活路由的**最小值**
  （不同路由暂不支持各自独立的周期，属于已知简化）；
- 每轮对所有上游并发探测；`(url, path, method, timeout)` 相同的只探一次；
- 任务在 `Proxy.close()` 里 cancel 并 await，避免退出时留下"Task was destroyed"警告；
- 单次探活异常只记日志，不让循环退出。

### 2.4 选路优先级

```text
1. 健康 且 未冷却        ← 正常情况
2. 不健康 但 未冷却      ← 没有任何健康上游时的退化
3. 冷却中（按恢复时间）  ← 最坏兜底：宁可打过去，也不整个前缀直接 502
```

组内仍然是平滑加权轮询。注意"不健康"不等于"排除"：当**所有**上游都不健康时，兜底仍会真实发请求，
否则健康检查本身就成了单点故障。

### 2.5 可观测

`GET /proxy-status`（需鉴权，`RESERVED` 已占位，代理前缀不能用它）：

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

未配置代理时返回 `{"enabled": false, "routes": []}`，便于统一探测。

### 2.6 测试

| 用例 | 验证点 |
| --- | --- |
| `test_health_check_skips_unhealthy_upstream` | 连续失败后摘除（请求不再落到它），恢复后重新参与 |
| `test_health_check_treats_4xx_as_alive` | 403/405/501 不被判死 |
| `test_health_check_is_off_by_default` | 默认不起任务、不发探活请求 |
| `test_proxy_status_requires_auth_and_reports_state` | 状态接口的形状与鉴权 |
| `test_health_config_is_validated` | 非法字段/取值在启动时报错 |

## 3. 已知边界（后续可演进）

- 一个后台任务 + 最小间隔：路由级独立周期还没做；
- 没有失败率窗口：短时间抖动可能触发一次摘除（由 `failures_to_down` 缓解）；
- 探活请求会进上游访问日志，需要上游提供轻量端点；
- 状态只读，不能手动"摘除/放回"某个上游（可加管理接口，但要考虑与探活任务的状态竞争）。

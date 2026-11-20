"""从代理头还原客户端 IP：只信任本机回环上的反向代理（如 Caddy）。"""

TRUSTED_PROXIES = {"127.0.0.1", "::1"}


def forwarded_ip(peer: str, forwarded_for: str | None = None,
                 real_ip: str | None = None) -> str:
    """peer 是 TCP 对端地址；仅当对端是回环时才采信 X-Forwarded-For / X-Real-IP。"""
    if peer not in TRUSTED_PROXIES:
        return peer
    value = forwarded_for or real_ip
    if not value:
        return peer
    # X-Forwarded-For 可能是 "client, proxy1, proxy2"，第一段是原始客户端。
    candidate = value.split(",")[0].strip()
    return candidate or peer

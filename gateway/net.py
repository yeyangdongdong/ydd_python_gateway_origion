"""从代理头还原客户端 IP：只信任本机回环上的反向代理（如 Caddy）。"""

from ipaddress import ip_address, ip_network

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


def ip_allowed(ip: str, cidrs: list[str]) -> bool:
    """空列表表示不限制；无法解析的客户端地址一律拒绝。"""
    if not cidrs:
        return True
    try:
        address = ip_address(ip)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    for cidr in cidrs:
        try:
            network = ip_network(cidr, strict=False)
        except ValueError:
            continue
        if address.version == network.version and address in network:
            return True
    return False

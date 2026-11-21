"""从代理头还原客户端 IP：只信任本机回环上的反向代理（如 Caddy）。"""

from ipaddress import ip_address, ip_network

TRUSTED_PROXIES = {"127.0.0.1", "::1"}


def _valid_ip(value: str | None) -> str | None:
    candidate = (value or "").strip()
    if not candidate:
        return None
    try:
        ip_address(candidate)
    except ValueError:
        return None
    return candidate


def forwarded_ip(peer: str, forwarded_for: str | None = None,
                 real_ip: str | None = None) -> str:
    """还原客户端 IP：只信任回环对端。

    优先 X-Real-IP（前置代理通常覆盖它）；X-Forwarded-For 取最右段，
    即最后一个可信代理追加的地址，避免客户端伪造的左段被当真。
    """
    if peer not in TRUSTED_PROXIES:
        return peer
    direct = _valid_ip(real_ip)
    if direct:
        return direct
    if forwarded_for:
        for part in reversed(forwarded_for.split(",")):
            candidate = _valid_ip(part)
            if candidate:
                return candidate
    return peer


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

"""本地账号与会话：密码哈希（scrypt）、会话 token、登录限速。

只依赖标准库：scrypt 在 hashlib 里，不需要额外安装 argon2。
"""

import base64
import hashlib
import hmac
import secrets
import time

COOKIE_NAME = "ydd_session"

# scrypt 参数：约 50~100ms 一次，足以抵御离线爆破又不拖慢登录。
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                            r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = _unb64(salt_b64), _unb64(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n),
                                r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# 用户不存在时也算一次哈希，避免通过响应时间枚举用户名。
DUMMY_HASH = hash_password("dummy-password-for-timing")


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LoginLimiter:
    """按 (IP, 用户名) 统计失败次数并锁定；单进程内存即可。"""

    def __init__(self, *, max_failures: int, window_seconds: int, lock_seconds: int) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lock_seconds = lock_seconds
        self.entries: dict[tuple[str, str], dict] = {}

    def retry_after(self, ip: str, username: str) -> int:
        entry = self.entries.get((ip, username))
        if not entry:
            return 0
        now = time.monotonic()
        if entry["locked_until"] > now:
            return int(entry["locked_until"] - now) + 1
        entry["failures"] = [stamp for stamp in entry["failures"]
                             if now - stamp < self.window_seconds]
        return 0

    def record_failure(self, ip: str, username: str) -> None:
        entry = self.entries.setdefault((ip, username), {"failures": [], "locked_until": 0.0})
        now = time.monotonic()
        entry["failures"].append(now)
        entry["failures"] = [stamp for stamp in entry["failures"]
                             if now - stamp < self.window_seconds]
        if len(entry["failures"]) >= self.max_failures:
            entry["locked_until"] = now + self.lock_seconds
            entry["failures"] = []

    def record_success(self, ip: str, username: str) -> None:
        self.entries.pop((ip, username), None)


def legacy_principal() -> dict:
    """GATEWAY_TOKEN 保留为过渡期的管理员身份。"""
    return {"kind": "legacy", "user_id": "", "username": "gateway-token",
            "role": "admin", "scopes": frozenset()}


def session_principal(session: dict) -> dict:
    return {"kind": "session", "user_id": session["user_id"],
            "username": session["username"], "role": session["role"],
            "scopes": frozenset()}


API_KEY_PREFIX = "ydd_"


def new_api_key() -> tuple[str, str, str]:
    """返回 (key_id, secret, 明文)。明文只展示一次，库里只存 secret 的哈希。"""
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    return key_id, secret, f"{API_KEY_PREFIX}{key_id}_{secret}"


def hash_api_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def parse_api_key(value: str) -> tuple[str, str] | None:
    if not value.startswith(API_KEY_PREFIX):
        return None
    key_id, _, secret = value[len(API_KEY_PREFIX):].partition("_")
    if len(key_id) != 16 or not secret:
        return None
    return key_id, secret


def api_key_principal(row: dict, scopes: frozenset[str]) -> dict:
    return {"kind": "api_key", "user_id": "", "username": row["name"],
            "role": "api_key", "scopes": scopes}

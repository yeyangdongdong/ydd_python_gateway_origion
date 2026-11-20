"""命令行管理账号：python -m gateway.admin <命令>。

用法：

    python -m gateway.admin create-user --username alice --admin
    python -m gateway.admin list-users
    python -m gateway.admin reset-password --username alice
"""

import argparse
import getpass
import sys

from .auth import hash_password
from .config import Settings
from .storage import Store


def _store() -> Store:
    settings = Settings.from_env()
    store = Store(settings.db_path)
    store.initialize(settings.model)
    return store


def prompt_password() -> str:
    password = getpass.getpass("密码（至少 10 位）: ")
    if len(password) < 10:
        raise SystemExit("密码太短，至少 10 位。")
    if password != getpass.getpass("再输入一次: "):
        raise SystemExit("两次输入不一致。")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="管理 Gateway 本地账号")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-user", help="创建用户")
    create.add_argument("--username", required=True)
    create.add_argument("--password", help="不填则交互输入")
    create.add_argument("--admin", action="store_true", help="创建为管理员")
    create.add_argument("--role", choices=["admin", "member", "viewer"], default=None)

    reset = sub.add_parser("reset-password", help="重置密码并登出该用户所有会话")
    reset.add_argument("--username", required=True)
    reset.add_argument("--password", help="不填则交互输入")

    sub.add_parser("list-users", help="列出用户")

    args = parser.parse_args(argv)
    store = _store()

    if args.command == "list-users":
        users = store.list_users()
        if not users:
            print("还没有用户，先创建：python -m gateway.admin create-user --username <名字> --admin")
        for user in users:
            print(f"{user['username']:20} {user['role']:8} {user['status']:9} {user['created_at']}")
        return 0

    if args.command == "create-user":
        role = args.role or ("admin" if args.admin else "member")
        if store.find_user(username=args.username):
            print(f"用户已存在：{args.username}", file=sys.stderr)
            return 1
        password = args.password or prompt_password()
        user = store.create_user(args.username, hash_password(password), role)
        print(f"已创建用户 {user['username']}（角色 {user['role']}，id {user['id']}）")
        return 0

    user = store.find_user(username=args.username)
    if user is None:
        print(f"用户不存在：{args.username}", file=sys.stderr)
        return 1
    password = args.password or prompt_password()
    store.update_user(user["id"], password_hash=hash_password(password))
    store.revoke_user_sessions(user["id"])
    print(f"已重置 {user['username']} 的密码，并登出其所有会话。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

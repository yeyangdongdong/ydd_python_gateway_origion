"""命令行管理账号：python -m gateway.admin <命令>。

用法：

    python -m gateway.admin create-user --username alice --admin
    python -m gateway.admin list-users
    python -m gateway.admin reset-password --username alice
"""

import argparse
import getpass
import sys

from .auth import hash_password, validate_password
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


def read_password(args, username: str) -> str:
    password = getattr(args, "password", None) or prompt_password()
    try:
        validate_password(password, username)
    except ValueError as error:
        raise SystemExit(str(error)) from error
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

    for name, help_text in (("disable-user", "停用用户并登出其所有会话"),
                            ("enable-user", "启用用户")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--username", required=True)

    role = sub.add_parser("set-role", help="修改用户角色")
    role.add_argument("--username", required=True)
    role.add_argument("--role", required=True, choices=["admin", "member", "viewer"])

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
        user = store.create_user(args.username, hash_password(read_password(args, args.username)), role)
        print(f"已创建用户 {user['username']}（角色 {user['role']}，id {user['id']}）")
        return 0

    user = store.find_user(username=args.username)
    if user is None:
        print(f"用户不存在：{args.username}", file=sys.stderr)
        return 1

    if args.command in {"disable-user", "set-role"}:
        target_role = args.role if args.command == "set-role" else user["role"]
        target_status = "disabled" if args.command == "disable-user" else "active"
        if user["role"] == "admin" and user["status"] == "active" \
                and (target_role != "admin" or target_status != "active") \
                and store.count_active_admins() <= 1:
            print("不能停用或降级最后一个管理员。", file=sys.stderr)
            return 1
        if args.command == "disable-user":
            store.update_user(user["id"], status="disabled")
            store.revoke_user_sessions(user["id"])
            print(f"已停用 {user['username']} 并登出其所有会话。")
        else:
            store.update_user(user["id"], role=target_role)
            print(f"已把 {user['username']} 的角色改为 {target_role}。")
        return 0

    if args.command == "enable-user":
        store.update_user(user["id"], status="active")
        print(f"已启用 {user['username']}。")
        return 0

    store.update_user(user["id"], password_hash=hash_password(read_password(args, user["username"])))
    store.revoke_user_sessions(user["id"])
    print(f"已重置 {user['username']} 的密码，并登出其所有会话。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""账号管理小工具：试点环境里开通/停用账号、改数据域、重置密码。

    python -m scripts.users list
    python -m scripts.users add --username wang --name "王芳 · 经代" --channels C04
    python -m scripts.users scope --username wang --channels C01,C04
    python -m scripts.users passwd --username wang
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app import store


def cmd_list(_: argparse.Namespace) -> None:
    with store.connect() as con:
        rows = con.execute(
            "SELECT username, name, role, channel_scope, org_scope, daily_quota, active FROM users ORDER BY id"
        ).fetchall()
    print(f"{'工号':<10}{'姓名':<18}{'角色':<10}{'渠道':<12}{'机构':<10}{'额度':<6}状态")
    for r in rows:
        print(f"{r['username']:<10}{r['name']:<18}{r['role']:<10}"
              f"{r['channel_scope'] or '全部':<12}{r['org_scope'] or '全部':<10}"
              f"{r['daily_quota']:<6}{'启用' if r['active'] else '停用'}")


def cmd_add(args: argparse.Namespace) -> None:
    password = args.password or getpass.getpass("初始密码：")
    with store.connect() as con:
        con.execute(
            "INSERT INTO users (username, name, role, password_hash, org_scope, channel_scope, daily_quota, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (args.username, args.name, args.role, store.hash_password(password),
             args.orgs or "", args.channels or "", args.quota, store.now()),
        )
    print(f"已开通 {args.username}（{args.name}），数据域：渠道 {args.channels or '全部'} / 机构 {args.orgs or '全部'}")


def cmd_scope(args: argparse.Namespace) -> None:
    with store.connect() as con:
        cur = con.execute(
            "UPDATE users SET channel_scope = ?, org_scope = ? WHERE username = ?",
            (args.channels or "", args.orgs or "", args.username),
        )
    if not cur.rowcount:
        sys.exit(f"没有这个工号：{args.username}")
    print(f"{args.username} 的数据域已改为：渠道 {args.channels or '全部'} / 机构 {args.orgs or '全部'}")


def cmd_passwd(args: argparse.Namespace) -> None:
    password = args.password or getpass.getpass("新密码：")
    if not store.set_password(args.username, password):
        sys.exit(f"没有这个工号：{args.username}")
    print(f"{args.username} 的密码已重置")


def cmd_disable(args: argparse.Namespace) -> None:
    with store.connect() as con:
        cur = con.execute("UPDATE users SET active = 0 WHERE username = ?", (args.username,))
    if not cur.rowcount:
        sys.exit(f"没有这个工号：{args.username}")
    print(f"{args.username} 已停用")


def main() -> None:
    parser = argparse.ArgumentParser(description="智能问数 Agent · 账号管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出账号").set_defaults(func=cmd_list)

    add = sub.add_parser("add", help="开通账号")
    add.add_argument("--username", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--role", default="analyst", choices=["analyst", "admin"])
    add.add_argument("--channels", help="逗号分隔的渠道码，留空表示全部")
    add.add_argument("--orgs", help="逗号分隔的机构码，留空表示全部")
    add.add_argument("--quota", type=int, default=200)
    add.add_argument("--password")
    add.set_defaults(func=cmd_add)

    scope = sub.add_parser("scope", help="修改数据域")
    scope.add_argument("--username", required=True)
    scope.add_argument("--channels")
    scope.add_argument("--orgs")
    scope.set_defaults(func=cmd_scope)

    pwd = sub.add_parser("passwd", help="重置密码")
    pwd.add_argument("--username", required=True)
    pwd.add_argument("--password")
    pwd.set_defaults(func=cmd_passwd)

    dis = sub.add_parser("disable", help="停用账号")
    dis.add_argument("--username", required=True)
    dis.set_defaults(func=cmd_disable)

    args = parser.parse_args()
    store.init_db()
    args.func(args)


if __name__ == "__main__":
    main()

"""身份与数据域。

两类主体：
- 登录用户：从会话 cookie 解析，带组织/渠道数据域；
- 匿名：仅在 NL2SQL_AUTH=off 的公开演示模式下出现，拥有全量数据域。

数据域不是"界面上藏起来"的，而是会被翻译成 SQL 谓词注入查询，所以即使模型写错、
或用户直接构造请求，也只能看到自己范围内的行。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from app import config, store

CHANNEL_NAMES = {
    "C01": "个险代理人",
    "C02": "银保",
    "C03": "互联网",
    "C04": "经纪",
    "C99": "未归属",
}


@dataclass(frozen=True)
class Principal:
    id: int
    username: str
    name: str
    role: str = "analyst"
    orgs: frozenset[str] = field(default_factory=frozenset)
    channels: frozenset[str] = field(default_factory=frozenset)
    quota: int = 200

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def full_scope(self) -> bool:
        return not self.orgs and not self.channels

    @property
    def scope_label(self) -> str:
        if self.full_scope:
            return "全量数据"
        bits = []
        if self.channels:
            bits.append("渠道 " + "、".join(CHANNEL_NAMES.get(c, c) for c in sorted(self.channels)))
        if self.orgs:
            bits.append("机构 " + "、".join(sorted(self.orgs)))
        return " / ".join(bits)


ANONYMOUS = Principal(id=0, username="anonymous", name="访客", role="admin")


def _split(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(x.strip() for x in value.split(",") if x.strip())


def principal_from_row(row: sqlite3.Row) -> Principal:
    return Principal(
        id=int(row["id"]),
        username=row["username"],
        name=row["name"],
        role=row["role"],
        orgs=_split(row["org_scope"]),
        channels=_split(row["channel_scope"]),
        quota=int(row["daily_quota"]),
    )


def current_user(request: Request) -> Principal | None:
    token = request.cookies.get(config.SESSION_COOKIE) or ""
    session = store.get_session(token)
    if not session:
        return None
    row = store.get_user_by_id(int(session["user_id"]))
    if not row:
        return None
    return principal_from_row(row)


def require_user(request: Request) -> Principal:
    user = current_user(request)
    if user:
        return user
    if not config.AUTH_REQUIRED:
        return ANONYMOUS
    raise HTTPException(401, "请先登录")


def require_admin(request: Request) -> Principal:
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(403, "只有管理员可以查看审计日志")
    return user


def login(username: str, password: str, ip: str = "") -> tuple[str, Principal]:
    row = store.get_user(username.strip())
    if not row or not store.verify_password(password, row["password_hash"]):
        raise HTTPException(401, "工号或密码不正确")
    token = store.create_session(int(row["id"]), ip)
    return token, principal_from_row(row)

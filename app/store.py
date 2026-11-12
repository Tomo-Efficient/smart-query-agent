"""运行时状态：账号、会话、对话、审计。

放在单独的 SQLite 里，和只读的业务数仓完全分开——
业务库永远只读，账号与审计需要写，混在一起就得给业务库开写权限，这是不能接受的。
"""

from __future__ import annotations

import hashlib
import json
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT UNIQUE NOT NULL,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'analyst',
  password_hash TEXT NOT NULL,
  org_scope     TEXT NOT NULL DEFAULT '',
  channel_scope TEXT NOT NULL DEFAULT '',
  daily_quota   INTEGER NOT NULL DEFAULT 200,
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token     TEXT PRIMARY KEY,
  user_id   INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  ip        TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
  id         TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  title      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL,
  user_id         INTEGER NOT NULL,
  question        TEXT NOT NULL,
  answer          TEXT,
  sql             TEXT,
  status          TEXT,
  rows            INTEGER,
  ms              INTEGER,
  payload         TEXT,
  created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  user_id         INTEGER,
  username        TEXT,
  ip              TEXT,
  conversation_id TEXT,
  question        TEXT,
  sql             TEXT,
  status          TEXT,
  rows            INTEGER,
  ms              INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""

DEMO_USERS = [
    # username, name, role, password, orgs, channels
    ("admin", "石浩田（管理员）", "admin", "admin123", "", ""),
    ("zhang", "张敏 · 个险渠道", "analyst", "demo123", "", "C01"),
    ("li", "李伟 · 银保渠道", "analyst", "demo123", "", "C02"),
]


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def today_prefix() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    config.APP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.APP_DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        yield con
        con.commit()
    finally:
        con.close()


def _migrate(con: sqlite3.Connection) -> None:
    cols = {r["name"] for r in con.execute("PRAGMA table_info(messages)").fetchall()}
    if cols and "payload" not in cols:
        con.execute("ALTER TABLE messages ADD COLUMN payload TEXT")


def init_db() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        _migrate(con)
        if config.SEED_DEMO_USERS:
            for username, name, role, password, orgs, channels in DEMO_USERS:
                row = con.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
                if not row:
                    con.execute(
                        "INSERT INTO users (username, name, role, password_hash, org_scope, channel_scope,"
                        " daily_quota, created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (username, name, role, hash_password(password), orgs, channels,
                         config.DEFAULT_DAILY_QUOTA, now()),
                    )


# ------------------------------------------------------------------ 账号
def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, _ = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored)


def get_user(username: str) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()


def get_user_by_id(user_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone()


def set_password(username: str, password: str) -> bool:
    with connect() as con:
        cur = con.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hash_password(password), username))
        return cur.rowcount > 0


# ------------------------------------------------------------------ 会话
def create_session(user_id: int, ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    with connect() as con:
        con.execute("INSERT INTO sessions (token, user_id, created_at, last_seen, ip) VALUES (?,?,?,?,?)",
                    (token, user_id, now(), now(), ip))
    return token


def get_session(token: str) -> sqlite3.Row | None:
    if not token:
        return None
    with connect() as con:
        row = con.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if not row:
            return None
        created = datetime.fromisoformat(row["created_at"])
        if datetime.now(timezone.utc).astimezone() - created > timedelta(hours=config.SESSION_TTL_HOURS):
            con.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
        con.execute("UPDATE sessions SET last_seen = ? WHERE token = ?", (now(), token))
        return row


def delete_session(token: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ------------------------------------------------------------------ 对话与审计
def ensure_conversation(conversation_id: str | None, user_id: int, title: str) -> str:
    cid = conversation_id or secrets.token_hex(8)
    with connect() as con:
        row = con.execute("SELECT id FROM conversations WHERE id = ? AND user_id = ?", (cid, user_id)).fetchone()
        if row:
            con.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now(), cid))
        else:
            con.execute("INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                        (cid, user_id, title[:60], now(), now()))
    return cid


def recent_messages(conversation_id: str, limit: int = 3) -> list[dict[str, Any]]:
    if not conversation_id:
        return []
    with connect() as con:
        rows = con.execute(
            "SELECT question, sql, status FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def add_message(conversation_id: str, user_id: int, question: str, payload: dict[str, Any]) -> None:
    try:
        blob = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = None
    with connect() as con:
        con.execute(
            "INSERT INTO messages (conversation_id, user_id, question, answer, sql, status, rows, ms, payload, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, user_id, question, payload.get("answer"), payload.get("sql"),
             payload.get("status"), payload.get("row_count"), payload.get("timings", {}).get("total_ms"),
             blob, now()),
        )


def list_conversations(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT c.id, c.title, c.updated_at,"
            " (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS turns"
            " FROM conversations c WHERE c.user_id = ?"
            " ORDER BY c.updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def conversation_messages(conversation_id: str, user_id: int) -> list[dict[str, Any]] | None:
    with connect() as con:
        own = con.execute("SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
                          (conversation_id, user_id)).fetchone()
        if not own:
            return None
        rows = con.execute(
            "SELECT question, payload, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    out = []
    for r in rows:
        item = {"question": r["question"], "created_at": r["created_at"]}
        if r["payload"]:
            try:
                item["payload"] = json.loads(r["payload"])
            except (TypeError, ValueError):
                item["payload"] = None
        out.append(item)
    return out


def delete_conversation(conversation_id: str, user_id: int) -> bool:
    with connect() as con:
        cur = con.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id))
        con.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
    return cur.rowcount > 0


def add_audit(user: sqlite3.Row | None, ip: str, conversation_id: str, question: str,
              payload: dict[str, Any]) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO audit (ts, user_id, username, ip, conversation_id, question, sql, status, rows, ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now(), user["id"] if user else None, user["username"] if user else "(匿名)", ip,
             conversation_id, question, payload.get("sql"), payload.get("status"),
             payload.get("row_count"), payload.get("timings", {}).get("total_ms")),
        )


def list_audit(limit: int = 50, user_id: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM audit"
    args: tuple = ()
    if user_id is not None:
        sql += " WHERE user_id = ?"
        args = (user_id,)
    sql += " ORDER BY id DESC LIMIT ?"
    with connect() as con:
        rows = con.execute(sql, (*args, min(limit, 200))).fetchall()
    return [dict(r) for r in rows]


def quota_used_today(user_id: int) -> int:
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE user_id = ? AND created_at LIKE ?",
            (user_id, f"{today_prefix()}%"),
        ).fetchone()
    return int(row["n"] if row else 0)


def stats() -> dict[str, Any]:
    with connect() as con:
        users = con.execute("SELECT COUNT(*) AS n FROM users WHERE active = 1").fetchone()["n"]
        asks = con.execute("SELECT COUNT(*) AS n FROM audit").fetchone()["n"]
        blocked = con.execute("SELECT COUNT(*) AS n FROM audit WHERE status = 'blocked'").fetchone()["n"]
    return {"users": users, "asks": asks, "blocked": blocked}

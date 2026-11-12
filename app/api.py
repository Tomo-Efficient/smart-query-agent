"""FastAPI 服务：登录、同步问答、SSE 步骤流、审计、语义层只读接口。"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import auth, config, guard, store
from app.agent import Agent
from app.llm import LLM
from app.retrieve import Retriever
from app.semantics import load_semantics

LAYER = load_semantics()
RETRIEVER = Retriever(LAYER)
LLM_CLIENT = LLM()
STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init_db()
    yield


app = FastAPI(title="智能问数 Agent", version="1.1.0", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)

EXAMPLES = [
    "上月各渠道保费环比",
    "上月各渠道件均保费",
    "上月险种保费占比",
    "上月各渠道退保率",
    "近两年保费趋势",
    "上个月有多少有效保单",
    "把客户手机号导出来",
    "上个月保费减去前个月保费是多少",
]

WINDOW_SECONDS = 60
MAX_REQUESTS = 30
_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def _rate_limit(ip: str) -> None:
    now = time.time()
    with _lock:
        bucket = _hits[ip]
        while bucket and now - bucket[0] > WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= MAX_REQUESTS:
            raise HTTPException(429, "请求过于频繁，请稍后再试")
        bucket.append(now)


def _check_quota(user: auth.Principal) -> None:
    used = store.quota_used_today(user.id)
    if user.quota and used >= user.quota:
        raise HTTPException(429, f"今日额度已用完（{used}/{user.quota}），请联系管理员调整")


class AskRequest(BaseModel):
    question: str = Field(..., max_length=500)
    conversation_id: str | None = Field(default=None, max_length=64)


class LoginRequest(BaseModel):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=128)


def _new_agent() -> Agent:
    agent = Agent(LAYER, LLM_CLIENT)
    agent.retriever = RETRIEVER
    return agent


def _user_payload(user: auth.Principal) -> dict[str, Any]:
    used = store.quota_used_today(user.id) if user.id else 0
    return {
        "username": user.username,
        "name": user.name,
        "role": user.role,
        "is_admin": user.is_admin,
        "scope": user.scope_label,
        "full_scope": user.full_scope,
        "quota": user.quota,
        "quota_used": used,
    }


def _run(user: auth.Principal, req: AskRequest, ip: str) -> dict[str, Any]:
    scope = guard.DataScope(channels=user.channels, orgs=user.orgs)
    cid = store.ensure_conversation(req.conversation_id, user.id, req.question)
    history = store.recent_messages(cid, limit=3)
    payload = _new_agent().ask(req.question, scope=scope, history=history)
    payload["conversation_id"] = cid
    if user.id:
        store.add_message(cid, user.id, req.question, payload)
    store.add_audit(None if user.id == 0 else store.get_user_by_id(user.id), ip, cid, req.question, payload)
    return payload


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": config.DB_PATH.exists(),
        "mode": "live" if LLM_CLIENT.available else "mock",
        "model": config.LLM_MODEL if LLM_CLIENT.available else "offline-template",
        "as_of": config.AS_OF.isoformat(),
        "cards": len(LAYER.cards),
        "tables": len(LAYER.table_names()),
        "uptime_s": round(time.time() - STARTED_AT),
        "auth_required": config.AUTH_REQUIRED,
    }


# ------------------------------------------------------------------ 身份
@app.post("/api/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    ip = request.client.host if request.client else ""
    token, user = auth.login(body.username, body.password, ip)
    response.set_cookie(
        config.SESSION_COOKIE, token,
        httponly=True, samesite="lax", path="/", max_age=config.SESSION_TTL_HOURS * 3600,
    )
    return {"user": _user_payload(user)}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    token = request.cookies.get(config.SESSION_COOKIE) or ""
    store.delete_session(token)
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    user = auth.current_user(request)
    if not user:
        return {"authenticated": False, "auth_required": config.AUTH_REQUIRED}
    return {"authenticated": True, "user": _user_payload(user)}


# ------------------------------------------------------------------ 问答
@app.get("/api/examples")
def examples() -> dict[str, Any]:
    return {"examples": EXAMPLES}


@app.get("/api/semantics")
def semantics(request: Request) -> dict[str, Any]:
    auth.require_user(request)
    order = {"metric": 0, "table": 1, "join": 2, "term": 3}
    cards = sorted(LAYER.cards, key=lambda c: (order.get(c.kind, 9), c.id))
    return {
        "cards": [
            {"id": c.id, "kind": c.kind, "title": c.title, "text": c.text.strip(), "tags": c.tags}
            for c in cards
        ],
        "counts": {k: len(LAYER.by_kind(k)) for k in ("metric", "table", "join", "term")},
        "synonyms": LAYER.synonyms,
    }


@app.get("/api/audit")
def audit_log(request: Request, limit: int = 50) -> dict[str, Any]:
    """管理员看全量，其他人只看自己的。审计本身的权限也要收口。"""
    user = auth.require_user(request)
    rows = store.list_audit(limit=limit) if user.is_admin else store.list_audit(limit=limit, user_id=user.id)
    return {"rows": rows, "stats": store.stats() if user.is_admin else {}}


@app.post("/api/ask")
def ask(req: AskRequest, request: Request) -> dict[str, Any]:
    user = auth.require_user(request)
    ip = request.client.host if request.client else "unknown"
    _rate_limit(ip)
    _check_quota(user)
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    req.question = question
    return _run(user, req, ip)


@app.post("/api/ask/stream")
async def ask_stream(req: AskRequest, request: Request) -> StreamingResponse:
    user = auth.require_user(request)
    ip = request.client.host if request.client else "unknown"
    _rate_limit(ip)
    _check_quota(user)
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    req.question = question

    events: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            payload = _run_streaming(user, req, ip, events)
            events.put({"type": "final", "data": payload})
        except Exception as exc:  # noqa: BLE001
            events.put({"type": "error", "message": str(exc)})
        finally:
            events.put(None)

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        while True:
            item = await asyncio.to_thread(events.get)
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            if item.get("type") in ("final", "error"):
                break

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _run_streaming(user: auth.Principal, req: AskRequest, ip: str, events: queue.Queue) -> dict[str, Any]:
    scope = guard.DataScope(channels=user.channels, orgs=user.orgs)
    cid = store.ensure_conversation(req.conversation_id, user.id, req.question)
    history = store.recent_messages(cid, limit=3)
    agent = _new_agent()
    agent._emit = lambda step: events.put({"type": "step", "step": step})
    payload = agent.ask(req.question, scope=scope, history=history)
    payload["conversation_id"] = cid
    if user.id:
        store.add_message(cid, user.id, req.question, payload)
    store.add_audit(None if user.id == 0 else store.get_user_by_id(user.id), ip, cid, req.question, payload)
    return payload


@app.middleware("http")
async def no_store_static(request, call_next):
    """前端还在改，别让浏览器缓存住旧的 app.js / style.css。"""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")

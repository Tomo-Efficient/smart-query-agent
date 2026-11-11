"""FastAPI 服务：同步问答 + SSE 步骤流 + 语义层只读接口。"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import config
from app.agent import Agent
from app.llm import LLM
from app.retrieve import Retriever
from app.semantics import load_semantics

LAYER = load_semantics()
RETRIEVER = Retriever(LAYER)
LLM_CLIENT = LLM()
STARTED_AT = time.time()

app = FastAPI(title="智能问数 Agent", version="1.0.0", docs_url="/api/docs", redoc_url=None)

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


class AskRequest(BaseModel):
    question: str = Field(..., max_length=500)


def _new_agent() -> Agent:
    agent = Agent(LAYER, LLM_CLIENT)
    agent.retriever = RETRIEVER
    return agent


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
    }


@app.get("/api/examples")
def examples() -> dict[str, Any]:
    return {"examples": EXAMPLES}


@app.get("/api/semantics")
def semantics() -> dict[str, Any]:
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


@app.post("/api/ask")
def ask(req: AskRequest, request: Request) -> dict[str, Any]:
    _rate_limit(request.client.host if request.client else "unknown")
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
    return _new_agent().ask(question)


@app.post("/api/ask/stream")
async def ask_stream(req: AskRequest, request: Request) -> StreamingResponse:
    _rate_limit(request.client.host if request.client else "unknown")
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")

    events: queue.Queue = queue.Queue()

    def worker() -> None:
        agent = _new_agent()
        agent._emit = lambda step: events.put({"type": "step", "step": step})
        try:
            payload = agent.ask(question)
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


app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")

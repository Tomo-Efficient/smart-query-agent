"""在线路径（不依赖真实模型 Key）：用本地假模型验证 Prompt→JSON→执行→结论的完整闭环。

跑法：pytest tests/test_live_path.py -q
假模型只做一件事：按 system 提示判断当前是哪一步（生成 / 修复 / 写结论），返回对应内容。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import config, generator
from app.agent import Agent

GOOD_SQL = """
SELECT c.channel_name AS 渠道, ROUND(SUM(COALESCE(p.premium, 0)), 0) AS 保费
FROM dwd_premium_month p
JOIN dwd_policy dp ON p.policy_id = dp.policy_id
JOIN dim_channel c ON p.channel_code = c.channel_code
WHERE dp.policy_status = '有效' AND p.stat_month >= DATE '2026-05-01' AND p.stat_month < DATE '2026-06-01'
GROUP BY 1 ORDER BY 2 DESC
"""


class FakeModel:
    """按提示内容分支的假模型；broken_first=True 时第一次生成错误 SQL。"""

    def __init__(self, broken_first: bool = False):
        self.broken_first = broken_first
        self.calls: list[str] = []

    def reply(self, system: str) -> dict:
        if "SQL 修复器" in system:
            self.calls.append("repair")
            return {"sql": GOOD_SQL, "reason": "表名写错"}
        if "问数 Agent" in system:
            self.calls.append("generate")
            if self.broken_first and self.calls.count("generate") == 1:
                return {"sql": "SELECT nonexistent_col FROM dwd_policy", "plan": "错误示例"}
            return {
                "sql": GOOD_SQL.strip(),
                "plan": "按渠道汇总 2026 年 5 月有效保单保费",
                "assumptions": ["口径为有效保单的月度确认保费"],
                "needs_clarification": False,
                "clarification": "",
            }
        self.calls.append("compose")
        return "5 月各渠道保费已汇总，银保渠道最高。"


def serve(model: FakeModel) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            system = body["messages"][0]["content"]
            content = model.reply(system)
            payload = {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "fake"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
            }
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


@pytest.fixture
def live_model(monkeypatch):
    model = FakeModel()
    server, base = serve(model)
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_BASE_URL", base)
    monkeypatch.setattr(config, "LLM_MODEL", "fake-model")
    monkeypatch.setattr(config, "LLM_TIMEOUT", 5)
    yield model
    server.shutdown()


def test_live_chain_runs_end_to_end(live_model):
    result = Agent().ask("上月各渠道保费收入是多少")
    assert result["status"] == "ok"
    assert result["mode"] == "live"
    assert result["row_count"] == 4
    assert result["usage"]["input_tokens"] > 0 and result["usage"]["output_tokens"] > 0
    assert "generate" in live_model.calls and "compose" in live_model.calls
    assert result["assumptions"]


def test_repair_loop_fixes_broken_sql(monkeypatch):
    model = FakeModel(broken_first=True)
    server, base = serve(model)
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_BASE_URL", base)
    monkeypatch.setattr(config, "LLM_MODEL", "fake-model")
    monkeypatch.setattr(config, "LLM_TIMEOUT", 5)
    try:
        result = Agent().ask("上月各渠道保费收入是多少")
    finally:
        server.shutdown()

    assert result["status"] == "ok"
    assert result["row_count"] == 4
    assert "repair" in model.calls
    # 第一次执行失败、修复后成功，轨迹里应能看到两条 execute
    executes = [s for s in result["trace"] if s["key"] == "execute"]
    assert len(executes) >= 2
    assert executes[0]["status"] == "fail"

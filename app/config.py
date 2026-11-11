"""全局配置。"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = Path(os.getenv("NL2SQL_DB", ROOT / "data" / "warehouse.duckdb"))
SEMANTICS_DIR = Path(os.getenv("NL2SQL_SEMANTICS", ROOT / "semantics"))
WEB_DIR = ROOT / "web"

# 运行时状态（账号 / 会话 / 审计）：与业务数仓分开，业务库始终只读
APP_DB_PATH = Path(os.getenv("NL2SQL_APP_DB", ROOT / "data" / "app.db"))
SESSION_COOKIE = "sq_session"
SESSION_TTL_HOURS = int(os.getenv("NL2SQL_SESSION_TTL", "12"))
DEFAULT_DAILY_QUOTA = int(os.getenv("NL2SQL_DAILY_QUOTA", "200"))
# 试点模式：开启后所有 /api/ask 都需要登录；关闭则退化为公开演示
AUTH_REQUIRED = os.getenv("NL2SQL_AUTH", "on").lower() not in ("off", "0", "false")
SEED_DEMO_USERS = os.getenv("NL2SQL_SEED_USERS", "on").lower() not in ("off", "0", "false")

# 数据截止日：所有"上月/本月"相对它计算，避免演示时随系统时钟漂移。
AS_OF = date(2026, 6, 30)

MAX_ROWS = int(os.getenv("NL2SQL_MAX_ROWS", "200"))
MAX_LIMIT = 500
QUERY_TIMEOUT_MS = int(os.getenv("NL2SQL_TIMEOUT_MS", "15000"))

# 重试与预算
MAX_REPAIR = 2
MAX_AGENT_ROUNDS = 4
MAX_TOOL_CALLS = 6

# LLM（OpenAI 兼容协议；默认 DeepSeek）
LLM_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))
LLM_MODE = os.getenv("LLM_MODE", "auto")  # auto | live | mock

EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")


def llm_enabled() -> bool:
    if LLM_MODE == "mock":
        return False
    if LLM_MODE == "live":
        return True
    return bool(LLM_API_KEY)


def today_label() -> str:
    return AS_OF.isoformat()

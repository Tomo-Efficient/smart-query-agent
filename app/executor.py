"""只读执行器：DuckDB 单文件、只读连接、超时中断、结果行数上限。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import duckdb

from app import config


class ExecutionError(RuntimeError):
    pass


@dataclass
class ExecResult:
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    truncated: bool = False
    ms: float = 0.0
    error: str = ""

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def run_sql(sql: str) -> ExecResult:
    started = time.perf_counter()
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    timer = threading.Timer(config.QUERY_TIMEOUT_MS / 1000, con.interrupt)
    try:
        con.execute("SET enable_external_access=false")
        con.execute("SET memory_limit='512MB'")
        timer.start()
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(config.MAX_ROWS)
        return ExecResult(
            ok=True,
            columns=columns,
            rows=[[_jsonable(v) for v in row] for row in rows],
            truncated=len(rows) >= config.MAX_ROWS,
            ms=round((time.perf_counter() - started) * 1000, 1),
        )
    except Exception as exc:  # noqa: BLE001
        return ExecResult(ok=False, ms=round((time.perf_counter() - started) * 1000, 1), error=str(exc))
    finally:
        timer.cancel()
        con.close()


def explain(sql: str) -> str:
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    try:
        return con.execute("EXPLAIN " + sql).fetchall()[0][1]
    finally:
        con.close()

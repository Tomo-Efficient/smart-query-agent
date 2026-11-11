"""结果编排：把 SQL 结果讲成人话。"""

from __future__ import annotations

import re

from app.llm import LLM, LLMError, Usage
from app.router import Route

SYSTEM = """你是保险公司的数据分析师，把查询结果讲成业务能直接用的结论。

要求：
1. 先用一句话回答"是多少/谁最高/变化如何"，数字必须来自结果，不要编造。
2. 再补 1–2 句解释：口径、对比、异常或需要业务注意的点。
3. 金额单位是元；超过 1 万可用"万元"并保留 1 位小数。
4. 不要罗列整张表，不要说"根据查询结果"这类废话。
5. 如果结果为空，说明可能原因，并给出一个可执行的下一步。
6. 不超过 120 字，中文。"""

RATE_KEYS = ("率", "占比", "百分比", "环比", "同比", "增长", "pct", "rate", "mom", "yoy")
MONEY_KEYS = ("保费", "金额", "收入", "premium", "amount")
GROWTH_KEYS = ("环比", "同比", "增长", "mom", "yoy")
AVG_KEYS = ("件均", "平均", "均值", "avg")


def _fmt(col: str, value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, str):
        match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:T[\d:.]+)?", value)
        if match:
            return f"{match.group(1)}-{match.group(2)}" if match.group(3) == "01" else value
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            if any(k in col for k in RATE_KEYS):
                return f"{value:.2f}%"
            if any(k in col for k in ("万元", "wan")):
                return f"{value:,.1f}"
            if abs(value) >= 10000:
                return f"{value:,.0f}"
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        return f"{value:,}"
    return str(value)


def _md_table(columns: list[str], rows: list[list], limit: int = 15) -> str:
    head = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(_fmt(c, v) for c, v in zip(columns, row)) + " |" for row in rows[:limit]]
    return "\n".join([head, sep, *body])


def _numeric_columns(columns: list[str], rows: list[list]) -> list[int]:
    return [i for i, c in enumerate(columns)
            if isinstance(rows[0][i], (int, float)) and not isinstance(rows[0][i], bool)]


def _metric_index(question: str, columns: list[str], rows: list[list]) -> int | None:
    nums = _numeric_columns(columns, rows)
    if not nums:
        return None
    if len(nums) == 1:
        return nums[0]

    q_chars = set(question)

    def score(i: int) -> tuple[int, int]:
        col = columns[i]
        overlap = len(q_chars & set(col))
        asked_rate = any(k in question for k in RATE_KEYS)
        is_rate = any(k in col for k in RATE_KEYS)
        asked_money = any(k in question for k in MONEY_KEYS)
        is_money = any(k in col for k in MONEY_KEYS)
        bonus = 2 if (asked_rate and is_rate) or (asked_money and is_money) else 0
        return overlap + bonus, -i

    return max(nums, key=score)


def compose(question: str, columns: list[str], rows: list[list], route: Route, llm: LLM, note: str = "") -> tuple[str, Usage]:
    if llm.available and rows:
        user = f"""## 问题
{question}

## 结果（最多 15 行）
{_md_table(columns, rows)}

共 {len(rows)} 行。{note}"""
        try:
            text, usage = llm.chat(SYSTEM, user, temperature=0.2)
            if text.strip():
                return text.strip(), usage
        except LLMError:
            pass
    return fallback(question, columns, rows), Usage()


def fallback(question: str, columns: list[str], rows: list[list]) -> str:
    if not rows:
        return "没有查到符合条件的数据。可能是该时间范围内没有业务发生，或过滤条件过严；可以放宽时间范围后再试。"

    if len(rows) == 1:
        parts = [f"{c}为 {_fmt(c, v)}" for c, v in zip(columns, rows[0]) if v is not None]
        return ("，".join(parts) + "。") if parts else "查询有结果，但所有指标为空。"

    def label(row) -> str:
        parts = [str(v) for i, v in enumerate(row[: len(columns)])
                 if v is not None and i < len(columns) and not isinstance(v, (int, float))]
        return " ".join(parts) or "整体"

    growth_idx = [i for i, c in enumerate(columns) if any(k in c.lower() for k in GROWTH_KEYS)]
    if growth_idx and any(k in question.lower() for k in GROWTH_KEYS):
        gi = growth_idx[0]
        col = columns[gi]
        vals = [(r, r[gi]) for r in rows if isinstance(r[gi], (int, float))]
        top, top_v = max(vals, key=lambda x: x[1])
        low, low_v = min(vals, key=lambda x: x[1])
        text = f"{label(top)}{col} {_fmt(col, top_v)}"
        if low_v != top_v:
            text += f"，{label(low)} {_fmt(col, low_v)}"
        money = next((i for i, c in enumerate(columns)
                      if i != gi and any(k in c.lower() for k in MONEY_KEYS)), None)
        if money is not None:
            total = sum(r[money] for r in rows if isinstance(r[money], (int, float)))
            text += f"；{columns[money]}合计 {_fmt(columns[money], total)}"
        return text + "。"

    idx = _metric_index(question, columns, rows)
    if idx is None:
        return f"查询返回 {len(rows)} 行，字段为{'、'.join(columns)}；明细见下方结果表。"

    col = columns[idx]
    values = [(r, r[idx]) for r in rows if isinstance(r[idx], (int, float))]
    if not values:
        return f"查询返回 {len(rows)} 行；明细见下方结果表。"

    top, top_v = max(values, key=lambda x: x[1])
    low, low_v = min(values, key=lambda x: x[1])
    is_rate = any(k in col.lower() for k in RATE_KEYS)
    is_avg = any(k in col.lower() for k in AVG_KEYS)

    if is_rate or is_avg:
        text = f"{label(top)}的{col}最高，为 {_fmt(col, top_v)}"
        if len(values) > 1 and low_v != top_v:
            text += f"；{label(low)}最低，为 {_fmt(col, low_v)}"
        tail = "共 {} 个分组，横向对比比加总更有意义。" if is_rate else "共 {} 个分组。"
        return text + "。" + tail.format(len(values))

    total = sum(v for _, v in values)
    return (
        f"{label(top)}的{col}最高，为 {_fmt(col, top_v)}；"
        f"{len(values)} 项合计 {_fmt(col, total)}。"
    )

"""SQL 生成：证据约束的 Prompt + 离线模板（无 Key 时保证链路可跑通）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app import config
from app.llm import LLM, LLMError, Usage
from app.retrieve import Hit
from app.router import Route
from app.semantics import SemanticLayer


@dataclass
class GenResult:
    sql: str = ""
    assumptions: list[str] = field(default_factory=list)
    plan: str = ""
    needs_clarification: bool = False
    clarification: str = ""
    mode: str = "live"
    usage: Usage = field(default_factory=Usage)
    error: str = ""


SYSTEM = """你是企业数据平台的问数 Agent，把业务问题翻译成 DuckDB SQL。

硬性规则：
1. 只能使用 Schema 中列出的表与字段；只生成一条 SELECT（可用 CTE）；不要写 DDL/DML。
2. 时间基准是数据截止日 {as_of}，"上月"指 {last_month}，"本月"指 {this_month}；不要使用 CURRENT_DATE。
3. 指标口径以"指标定义"为准；存在多种口径时用默认口径，并把假设写入 assumptions。
4. 明细优先：问保费收入/环比/同比用 dwd_premium_month；问新单/签单用 dwd_policy 的 sign_date。
5. 不要臆造字段；不确定就设 needs_clarification=true，并给出一个具体的澄清问题。
6. 结果列使用中文别名；金额保持"元"，不要自行换算成万元/亿元。
7. 聚合查询也要能解释：分子分母同口径，除零用 NULLIF。

只输出 JSON：
{{"sql": "...", "plan": "一句话思路", "assumptions": ["..."], "needs_clarification": false, "clarification": ""}}"""


def _prompt(question: str, hits: list[Hit], route: Route, layer: SemanticLayer,
            history: list[dict] | None = None) -> str:
    as_of = config.AS_OF
    last_month = f"{as_of.year}-{as_of.month - 1:02d}" if as_of.month > 1 else f"{as_of.year - 1}-12"
    this_month = f"{as_of.year}-{as_of.month:02d}"

    metric_hits = [h for h in hits if h.card.kind == "metric"][:3]
    other_hits = [h for h in hits if h.card.kind != "metric"][:6]
    evidence = "\n\n".join(
        f"[{h.card.id}] {h.card.title}\n{h.card.text.strip()}" for h in metric_hits + other_hits
    )

    history_block = ""
    if history:
        lines = []
        for item in history[-3:]:
            lines.append(f"- 问：{item.get('question', '')}")
            sql = (item.get("sql") or "").strip()
            if sql:
                lines.append(f"  上一条 SQL：{' '.join(sql.split())[:300]}")
        history_block = "\n## 对话历史（用于理解指代，必要时沿用条件）\n" + "\n".join(lines) + "\n"

    return f"""## 数据截止
{as_of.isoformat()}（上月={last_month}，本月={this_month}，本月不完整）

## Schema
{layer.schema_prompt()}

## 指标定义（优先遵守）
{layer.metric_prompt([h.card.id for h in metric_hits])}

## 业务规则
{layer.rules_prompt()}

## 检索证据
{evidence}

## 路由
复杂度：{route.label}（{', '.join(route.reasons)}）
{history_block}
## 问题
{question}"""


def generate(question: str, hits: list[Hit], route: Route, layer: SemanticLayer, llm: LLM,
             history: list[dict] | None = None) -> GenResult:
    if not llm.available:
        rewritten = rewrite_followup(question, history)
        result = mock_generate(rewritten or question, route)
        if rewritten:
            result.plan = f"追问改写：{rewritten}"
            result.assumptions.append(f"按上一轮条件理解为：{rewritten}")
        return result

    try:
        data, usage = llm.chat_json(
            SYSTEM.format(
                as_of=config.AS_OF.isoformat(),
                last_month=_month_shift(config.AS_OF, -1),
                this_month=_month_shift(config.AS_OF, 0),
            ),
            _prompt(question, hits, route, layer, history),
        )
    except LLMError as exc:
        fallback = mock_generate(question, route)
        fallback.error = str(exc)
        return fallback

    sql = (data.get("sql") or "").strip()
    return GenResult(
        sql=sql,
        assumptions=list(data.get("assumptions") or []),
        plan=data.get("plan") or "",
        needs_clarification=bool(data.get("needs_clarification")),
        clarification=data.get("clarification") or "",
        mode="live",
        usage=usage,
    )


def repair(question: str, sql: str, error: str, layer: SemanticLayer, llm: LLM) -> tuple[str, Usage]:
    if not llm.available:
        return "", Usage()
    system = "你是 SQL 修复器。根据报错修复 DuckDB SQL，只输出 JSON：{\"sql\": \"...\", \"reason\": \"...\"}。只能使用授权表，只读，单条 SELECT。"
    user = f"""## 授权表
{', '.join(sorted(layer.table_names()))}

## 问题
{question}

## 原 SQL
{sql}

## 错误
{error}

请做最小修复：不要删除业务过滤条件，不要改口径，不要加写操作。"""
    data, usage = llm.chat_json(system, user)
    return (data.get("sql") or "").strip(), usage


# ---------------------------------------------------------------- mock（关键词模板基线）
#
# 这层不是"假装在跑模型"：它是一条无 LLM 的规则基线，用于
#   1) 没有 Key 时也能完整演示链路；
#   2) 评测报告里的 no-LLM 对照组。
# 它只认固定问法，换个说法就会落到澄清分支——这正好说明模型的价值。

CHANNEL_CODE = {"互联网": "C03", "银保": "C02", "个险": "C01", "代理人": "C01", "经纪": "C04"}
TWELVE_PAT = r"近\s*(12|十二)\s*个?月|过去\s*(一|12|十二)\s*年|近\s*一年|最近\s*(一|12|十二)\s*年|过去\s*12\s*个?月|最近\s*12\s*个?月"
TREND_WORDS = r"趋势|走势|按月|每月"
NEAR3_PAT = r"近\s*(3|三)\s*个?月|最近\s*(3|三)\s*个?月|三个月|3\s*个月"


TIME_PHRASES = ("今年", "上季度", "近12个月", "过去一年", "最近一年", "去年", "上个月", "上月", "这个月", "本月", "近三个月")
FOLLOWUP_START = re.compile(r"^\s*(那|那么|呢|要是|如果|改成|换成|再看|看下|看看|再|换)")


def rewrite_followup(question: str, history: list[dict] | None) -> str | None:
    """离线模板没有上下文，所以把"那今年呢"这类追问还原成一句完整的问题。

    只处理两种最常见的追问：换时间窗、换拆解维度。其余情况原样返回 None，
    交给澄清分支——不猜比猜错好。
    """
    if not history:
        return None
    last = (history[-1].get("question") or "").strip()
    if not last:
        return None
    q = (question or "").strip()
    if not q or len(q) > 24:
        return None

    metric_words = ("保费", "件数", "退保", "赔付", "占比", "趋势", "保单", "收入", "件均")
    has_metric = any(w in q for w in metric_words)
    is_followup = bool(FOLLOWUP_START.match(q)) or q.endswith("呢") or any(p in q for p in TIME_PHRASES)
    if not is_followup:
        return None
    # 自带指标的完整问句不是追问（"上月各渠道退保率"不能拼到上一问上）
    if has_metric and not FOLLOWUP_START.match(q):
        return None

    # 1) 换时间窗
    for phrase in TIME_PHRASES:
        if phrase in q:
            base = last
            for old in TIME_PHRASES:
                base = base.replace(old, "")
            base = base.strip()
            if not base:
                return None
            return f"{phrase}{base}"

    # 2) 换维度
    wants_product = bool(re.search(r"险种|产品", q))
    wants_channel = "渠道" in q
    if wants_product and "险种" not in last:
        base = re.sub(r"各?渠道", "各险种", last).replace("渠道的", "险种的")
        if base != last:
            return base
    if wants_channel and "渠道" not in last:
        base = re.sub(r"各?险种", "各渠道", last)
        if base != last:
            return base

    return None


def _add_months(d: date, n: int) -> date:
    y, m = d.year, d.month + n - 1
    return date(y + m // 12, m % 12 + 1, 1)


def _month_shift(d: date, delta: int) -> str:
    return _add_months(d, delta).strftime("%Y-%m")


def _D(d: date) -> str:
    return f"DATE '{d.isoformat()}'"


def _window(question: str) -> tuple[date, date, str]:
    """时间窗一律相对"最近一个完整自然月"来算，而不是数据截止日。"""
    last = _add_months(config.AS_OF.replace(day=1), -1)
    nxt = _add_months(last, 1)

    if "今年" in question:
        return date(last.year, 1, 1), nxt, "今年以来（不含本月）"
    if "上季度" in question:
        qs = date(last.year, ((last.month - 1) // 3) * 3 + 1, 1)
        return _add_months(qs, -3), qs, "上一个自然季度"
    if re.search(TWELVE_PAT, question):
        return _add_months(last, -11), nxt, "近 12 个完整月"
    if re.search(NEAR3_PAT, question):
        return _add_months(last, -2), nxt, "近 3 个完整月"
    if re.search(r"环比|同比|同期|对比|跟|和上|vs|VS|增长|变化|涨跌|增速", question):
        return last, nxt, "上一个完整自然月"
    if "去年" in question:
        return _add_months(last, -12), _add_months(last, -11), "去年同月"
    if re.search(r"月度|按月|每月|趋势|走势", question):
        return _add_months(last, -11), nxt, "近 12 个完整月"
    return last, nxt, "上一个完整自然月"


def _channel_code(question: str) -> str:
    m = re.search(r"C0[1-4]", question, re.IGNORECASE)
    if m:
        return m.group(0).upper()
    for name, code in CHANNEL_CODE.items():
        if name in question:
            return code
    return ""


def mock_generate(question: str, route: Route) -> GenResult:
    """关键词模板基线：覆盖常见固定问法，用于离线演示与评测对照。"""
    q = question
    start, end, window_label = _window(q)
    ch = _channel_code(q)
    by_channel = "渠道" in q and not (ch and not re.search(r"各|每|分|按|四个|排名|对比", q))
    by_product = bool(re.search(r"险种|产品", q))
    is_avg = bool(re.search(r"件均|单均|平均每|平均保单|每张保单|平均保费", q))
    wants_rate = "退保" in q
    wants_claim = "赔付" in q
    wants_share = bool(re.search(r"占比|结构|分布|占多少", q))
    wants_yoy = bool(re.search(r"同比|去年同期", q)) or ("去年" in q and bool(re.search(r"vs|VS|对比|比", q)))
    wants_mom = bool(re.search(r"环比|增长|变化|增速|涨跌|对比|vs|VS", q)) and not wants_yoy
    wants_new = bool(re.search(r"新单|新保单|新增保单|签单|新增", q))
    wants_trend = bool(re.search(TWELVE_PAT, q)) or bool(re.search(TREND_WORDS, q))
    wants_top = bool(re.search(r"最高|最多|第一|冠军|最大|top|TOP|Top", q))
    wants_bottom = bool(re.search(r"最低|最少", q))
    top_n = 3 if re.search(r"top\s*3|前三|前 3", q, re.IGNORECASE) else 1

    s, e = _D(start), _D(end)
    prev_s, prev_e = _D(_add_months(start, -1)), _D(start)
    last_year_s, last_year_e = _D(_add_months(start, -12)), _D(_add_months(end, -12))
    last = _add_months(config.AS_OF.replace(day=1), -1)
    nxt = _add_months(last, 1)
    near3_s, near12_s = _D(_add_months(last, -2)), _D(_add_months(last, -11))

    sql = plan = ""
    assumptions: list[str] = [f"时间范围为{window_label}（{start} 至 {end}）"]

    def one_premium() -> str:
        cond = f" AND p.channel_code = '{ch}'" if ch else ""
        return (
            "SELECT ROUND(SUM(COALESCE(p.premium, 0)), 0) AS 保费"
            " FROM dwd_premium_month p JOIN dwd_policy dp ON p.policy_id = dp.policy_id"
            f" WHERE dp.policy_status = '有效' AND p.stat_month >= {s} AND p.stat_month < {e}{cond}"
        )

    def by_channel_premium(limit: int | None = None) -> str:
        cond = f" AND p.channel_code = '{ch}'" if ch else ""
        tail = f" LIMIT {limit}" if limit else ""
        return (
            "SELECT c.channel_name AS 渠道, ROUND(SUM(COALESCE(p.premium, 0)), 0) AS 保费"
            " FROM dwd_premium_month p JOIN dwd_policy dp ON p.policy_id = dp.policy_id"
            " JOIN dim_channel c ON p.channel_code = c.channel_code"
            f" WHERE dp.policy_status = '有效' AND p.stat_month >= {s} AND p.stat_month < {e}{cond}"
            f" GROUP BY 1 ORDER BY 2 DESC{tail}"
        )

    if re.search(r"手机号|身份证|证件号", q) and re.search(r"导出|导出来|列出|给我|下载|明细", q):
        sql = "SELECT customer_id, phone_masked FROM dim_customer LIMIT 100"
        plan = "按用户要求查询客户手机号（应由输入侧安全策略拦截）"

    elif wants_rate and by_product:
        sql = f"""
WITH s AS (
  SELECT dp.product_code AS product_code, COUNT(DISTINCT s.policy_id) AS sur_cnt
  FROM dwd_surrender s JOIN dwd_policy dp ON s.policy_id = dp.policy_id
  WHERE s.surrender_date >= {s} AND s.surrender_date < {e}
  GROUP BY 1
), a AS (
  SELECT product_code, COUNT(*) AS active_cnt FROM dwd_policy WHERE policy_status = '有效' GROUP BY 1
)
SELECT TRIM(pr.product_name) AS 险种,
       s.sur_cnt AS 退保件数,
       ROUND(100.0 * s.sur_cnt / NULLIF(a.active_cnt, 0), 2) AS 退保率百分比
FROM a JOIN s USING (product_code)
JOIN dim_product pr USING (product_code)
ORDER BY 3 DESC"""
        plan = "退保件数按险种去重计数，除以当前有效保单件数"
        assumptions.append("退保率分母为当前有效保单件数")

    elif wants_rate and "保费" in q:
        sql = f"""
WITH s AS (
  SELECT dp.channel_code AS channel_code, COUNT(DISTINCT s.policy_id) AS sur_cnt
  FROM dwd_surrender s JOIN dwd_policy dp ON s.policy_id = dp.policy_id
  WHERE s.surrender_date >= {s} AND s.surrender_date < {e}
  GROUP BY 1
), a AS (
  SELECT channel_code, COUNT(*) AS active_cnt FROM dwd_policy WHERE policy_status = '有效' GROUP BY 1
), pm AS (
  SELECT p.channel_code AS channel_code, SUM(COALESCE(p.premium, 0)) AS premium
  FROM dwd_premium_month p JOIN dwd_policy dp ON p.policy_id = dp.policy_id
  WHERE dp.policy_status = '有效' AND p.stat_month >= {s} AND p.stat_month < {e}
  GROUP BY 1
)
SELECT c.channel_name AS 渠道,
       ROUND(pm.premium, 0) AS 保费,
       ROUND(100.0 * s.sur_cnt / NULLIF(a.active_cnt, 0), 2) AS 退保率百分比
FROM pm JOIN a USING (channel_code)
LEFT JOIN s USING (channel_code)
JOIN dim_channel c USING (channel_code)
ORDER BY 3 DESC"""
        plan = "同一时间窗内按渠道关联保费与退保率"
        assumptions.append("退保率分母为当前有效保单件数")

    elif wants_rate:
        cond = f" AND dp.channel_code = '{ch}'" if ch else ""
        tail = " LIMIT 1" if (wants_top and by_channel) else ""
        sql = f"""
WITH s AS (
  SELECT dp.channel_code AS channel_code, COUNT(DISTINCT s.policy_id) AS sur_cnt
  FROM dwd_surrender s JOIN dwd_policy dp ON s.policy_id = dp.policy_id
  WHERE s.surrender_date >= {s} AND s.surrender_date < {e}{cond}
  GROUP BY 1
), a AS (
  SELECT channel_code, COUNT(*) AS active_cnt FROM dwd_policy WHERE policy_status = '有效' GROUP BY 1
)
SELECT c.channel_name AS 渠道,
       s.sur_cnt AS 退保件数,
       a.active_cnt AS 有效件数,
       ROUND(100.0 * s.sur_cnt / NULLIF(a.active_cnt, 0), 2) AS 退保率百分比
FROM a JOIN s USING (channel_code)
JOIN dim_channel c USING (channel_code)
ORDER BY 4 DESC{tail}"""
        plan = "退保件数按渠道去重计数，除以当前有效保单件数"
        assumptions.append("退保率分母为当前有效保单件数")

    elif wants_claim:
        sql = f"""
WITH cl AS (
  SELECT dp.product_code AS product_code, SUM(c.claim_amount) AS claim_amt
  FROM dwd_claim c JOIN dwd_policy dp ON c.policy_id = dp.policy_id
  WHERE c.claim_date >= {s} AND c.claim_date < {e}
  GROUP BY 1
), pm AS (
  SELECT p.product_code AS product_code, SUM(COALESCE(p.premium, 0)) AS premium
  FROM dwd_premium_month p JOIN dwd_policy dp ON p.policy_id = dp.policy_id
  WHERE dp.policy_status = '有效' AND p.stat_month >= {s} AND p.stat_month < {e}
  GROUP BY 1
)
SELECT TRIM(pr.product_name) AS 险种,
       ROUND(100.0 * cl.claim_amt / NULLIF(pm.premium, 0), 2) AS 赔付率百分比
FROM cl JOIN pm USING (product_code)
JOIN dim_product pr USING (product_code)
ORDER BY 2 DESC"""
        plan = "同期赔付金额除以有效保单保费"
        assumptions.append("赔付率 = 统计期赔付金额 / 同期有效保单保费")

    elif is_avg and re.search(r"件数|保单数|单量", q):
        sql = f"""
SELECT c.channel_name AS 渠道,
       COUNT(DISTINCT p.policy_id) AS 保单件数,
       ROUND(SUM(COALESCE(p.premium, 0)) / NULLIF(COUNT(DISTINCT p.policy_id), 0), 0) AS 件均保费
FROM dwd_premium_month p
JOIN dwd_policy dp ON p.policy_id = dp.policy_id
JOIN dim_channel c ON p.channel_code = c.channel_code
WHERE dp.policy_status = '有效'
  AND p.stat_month >= {s} AND p.stat_month < {e}
GROUP BY 1 ORDER BY 2 DESC"""
        plan = "同一批有效保单上同时计算件数与件均，避免口径错位"
        assumptions.append("件均 = 当期保费 / 当期有保费确认的保单数")

    elif is_avg:
        cond = f" AND p.channel_code = '{ch}'" if ch else ""
        sort = "ASC" if wants_bottom else "DESC"
        tail = " LIMIT 1" if (wants_top or wants_bottom) else ""
        sql = f"""
SELECT c.channel_name AS 渠道,
       ROUND(SUM(COALESCE(p.premium, 0)) / NULLIF(COUNT(DISTINCT p.policy_id), 0), 0) AS 件均保费
FROM dwd_premium_month p
JOIN dwd_policy dp ON p.policy_id = dp.policy_id
JOIN dim_channel c ON p.channel_code = c.channel_code
WHERE dp.policy_status = '有效'
  AND p.stat_month >= {s} AND p.stat_month < {e}{cond}
GROUP BY 1 ORDER BY 2 {sort}{tail}"""
        plan = "有效保单保费除以当月有保费确认的保单数"
        assumptions.append("件均 = 月保费 / 当月有效保单数")

    elif by_product and wants_share:
        sql = f"""
SELECT TRIM(pr.product_name) AS 险种,
       ROUND(SUM(COALESCE(p.premium, 0)), 0) AS 保费,
       ROUND(100.0 * SUM(COALESCE(p.premium, 0)) / SUM(SUM(COALESCE(p.premium, 0))) OVER (), 2) AS 占比百分比
FROM dwd_premium_month p
JOIN dwd_policy dp ON p.policy_id = dp.policy_id
JOIN dim_product pr ON dp.product_code = pr.product_code
WHERE dp.policy_status = '有效'
  AND p.stat_month >= {s} AND p.stat_month < {e}
GROUP BY 1 ORDER BY 2 DESC"""
        plan = "按险种汇总有效保单保费，窗口函数算占比"
        assumptions.append("占比分母为同期全险种保费合计")

    elif by_channel and wants_share:
        sql = f"""
SELECT c.channel_name AS 渠道,
       ROUND(SUM(COALESCE(p.premium, 0)), 0) AS 保费,
       ROUND(100.0 * SUM(COALESCE(p.premium, 0)) / SUM(SUM(COALESCE(p.premium, 0))) OVER (), 2) AS 占比百分比
FROM dwd_premium_month p
JOIN dwd_policy dp ON p.policy_id = dp.policy_id
JOIN dim_channel c ON p.channel_code = c.channel_code
WHERE dp.policy_status = '有效'
  AND p.stat_month >= {s} AND p.stat_month < {e}
GROUP BY 1 ORDER BY 2 DESC"""
        plan = "按渠道汇总有效保单保费，窗口函数算占比"
        assumptions.append("占比分母为同期全渠道保费合计")

    elif wants_new:
        sql = f"""
SELECT strftime(sign_date, '%Y-%m') AS 月份,
       COUNT(DISTINCT policy_id) AS 新单件数
FROM dwd_policy
WHERE sign_date >= {near3_s} AND sign_date < {_D(nxt)}
GROUP BY 1 ORDER BY 1"""
        plan = "按签单日归属月份统计新单件数"
        assumptions = ["新单口径为签单日落在统计期内的保单", "时间范围为近 3 个完整月"]

    elif wants_trend:
        sql = f"""
SELECT strftime(p.stat_month, '%Y-%m') AS 月份,
       ROUND(SUM(COALESCE(p.premium, 0)), 0) AS 保费
FROM dwd_premium_month p JOIN dwd_policy dp ON p.policy_id = dp.policy_id
WHERE dp.policy_status = '有效'
  AND p.stat_month >= {near12_s} AND p.stat_month < {_D(nxt)}
GROUP BY 1 ORDER BY 1"""
        plan = "按月汇总有效保单保费，观察近 12 个月趋势"
        assumptions = ["使用明细表而非预聚合宽表，避免把退保/满期保单算进来", "时间范围为近 12 个完整月"]

    elif wants_yoy:
        sql = f"""
WITH cur AS (
  SELECT p.channel_code, SUM(COALESCE(p.premium, 0)) AS premium
  FROM dwd_premium_month p JOIN dwd_policy dp ON p.policy_id = dp.policy_id
  WHERE dp.policy_status = '有效'
    AND p.stat_month >= {s} AND p.stat_month < {e}
  GROUP BY 1
), ly AS (
  SELECT p.channel_code, SUM(COALESCE(p.premium, 0)) AS premium
  FROM dwd_premium_month p JOIN dwd_policy dp ON p.policy_id = dp.policy_id
  WHERE dp.policy_status = '有效'
    AND p.stat_month >= {last_year_s} AND p.stat_month < {last_year_e}
  GROUP BY 1
)
SELECT c.channel_name AS 渠道,
       ROUND(cur.premium, 0) AS 本月保费,
       ROUND((cur.premium / NULLIF(ly.premium, 0) - 1) * 100, 2) AS 同比百分比
FROM cur JOIN ly USING (channel_code)
JOIN dim_channel c USING (channel_code)
ORDER BY 2 DESC"""
        plan = "本月与去年同月分别按渠道聚合，再算同比"
        assumptions.append("同比基期为去年同月")

    elif wants_mom and by_channel:
        sql = f"""
WITH cur AS (
  SELECT p.channel_code, SUM(COALESCE(p.premium, 0)) AS premium
  FROM dwd_premium_month p JOIN dwd_policy dp ON p.policy_id = dp.policy_id
  WHERE dp.policy_status = '有效'
    AND p.stat_month >= {s} AND p.stat_month < {e}
  GROUP BY 1
), prev AS (
  SELECT p.channel_code, SUM(COALESCE(p.premium, 0)) AS premium
  FROM dwd_premium_month p JOIN dwd_policy dp ON p.policy_id = dp.policy_id
  WHERE dp.policy_status = '有效'
    AND p.stat_month >= {prev_s} AND p.stat_month < {prev_e}
  GROUP BY 1
)
SELECT c.channel_name AS 渠道,
       ROUND(cur.premium, 0) AS 本月保费,
       ROUND(prev.premium, 0) AS 上月保费,
       ROUND((cur.premium / NULLIF(prev.premium, 0) - 1) * 100, 2) AS 环比百分比
FROM cur JOIN prev USING (channel_code)
JOIN dim_channel c USING (channel_code)
ORDER BY 2 DESC"""
        plan = "本月与上月分别按渠道聚合有效保单保费，再计算环比"
        assumptions.append("环比基期为上一个完整自然月")

    elif wants_mom:
        sql = f"""
WITH cur AS (
  SELECT SUM(COALESCE(p.premium, 0)) AS v
  FROM dwd_premium_month p JOIN dwd_policy dp ON p.policy_id = dp.policy_id
  WHERE dp.policy_status = '有效' AND p.stat_month >= {s} AND p.stat_month < {e}
), prev AS (
  SELECT SUM(COALESCE(p.premium, 0)) AS v
  FROM dwd_premium_month p JOIN dwd_policy dp ON p.policy_id = dp.policy_id
  WHERE dp.policy_status = '有效' AND p.stat_month >= {prev_s} AND p.stat_month < {prev_e}
)
SELECT ROUND(cur.v, 0) AS 本月保费,
       ROUND((cur.v / NULLIF(prev.v, 0) - 1) * 100, 2) AS 环比百分比
FROM cur, prev"""
        plan = "整体保费本月对上月的环比增长率"

    elif wants_top and ("渠道" in q):
        limit = top_n if re.search(r"top|前三", q, re.IGNORECASE) else 1
        sql = by_channel_premium(limit)
        plan = f"按统计期保费对渠道排序取前 {limit} 名"

    elif by_channel and "保费" in q:
        sql = by_channel_premium()
        plan = "按渠道汇总统计期的有效保单保费"

    elif re.search(r"保单", q) and not re.search(r"保费|金额|退保|赔付|新增|新单", q):
        sql = "SELECT COUNT(*) AS 有效保单件数 FROM dwd_policy WHERE policy_status = '有效'"
        plan = "统计当前有效保单总件数"
        assumptions = ["口径为当前有效保单（不含已退保、已满期）"]

    elif "保费" in q:
        sql = one_premium()
        plan = "汇总统计期内有效保单的确认保费"

    else:
        metric_words = ("保费", "件数", "保单", "退保", "赔付", "占比", "趋势", "渠道", "险种", "产品", "收入")
        if not any(w in q for w in metric_words):
            clarification = "你想看哪个指标（保费 / 件数 / 退保率 / 赔付率）、哪个时间范围、按什么维度拆？补充一句我就能查。"
        else:
            clarification = "离线模板没有覆盖这种问法（模板只认固定说法）。配置 DEEPSEEK_API_KEY 后，就可以用任意自然语言提问。"
        return GenResult(mode="mock", needs_clarification=True, clarification=clarification)

    return GenResult(sql=sql.strip(), plan=plan, assumptions=assumptions, mode="mock")

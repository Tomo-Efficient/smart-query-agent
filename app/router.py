"""复杂度路由：不是所有问题都值得进 Agent Loop。

规则特征优先：可解释、零成本、好调试；模型判定只作为兜底，并且必须给出理由。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SIMPLE_BUDGET = {"llm_calls": 1, "sql_runs": 1, "repair": 1, "rounds": 1}
MEDIUM_BUDGET = {"llm_calls": 3, "sql_runs": 2, "repair": 2, "rounds": 2}
COMPLEX_BUDGET = {"llm_calls": 6, "sql_runs": 4, "repair": 2, "rounds": 4}

RE_COMPARE = re.compile(r"环比|同比|对比|相比|较上|较去|增长|下降|趋势|走势|变化")
RE_MULTI_HOP = re.compile(r"为什么|原因|拆解|分别|各自|以及|同时|并且|再按|下钻|贡献")
RE_RANK = re.compile(r"top\s*\d+|前\s*\d+|排名|最高的|最低的|最多|最少", re.I)
RE_SHARE = re.compile(r"占比|结构|份额|分布|比重")
RE_AMBIGUOUS = re.compile(r"大概|差不多|最近|近期|可能|一些|某些")
RE_METRIC = re.compile(r"保费|件数|保单|退保|赔付|理赔|件均|客户数|保额")


@dataclass
class Route:
    level: str
    score: int
    reasons: list[str] = field(default_factory=list)

    @property
    def budget(self) -> dict:
        return {"simple": SIMPLE_BUDGET, "medium": MEDIUM_BUDGET, "complex": COMPLEX_BUDGET}[self.level]

    @property
    def label(self) -> str:
        return {"simple": "简单", "medium": "中等", "complex": "复杂"}[self.level]


def route(question: str, table_hits: int = 0) -> Route:
    score = 0
    reasons: list[str] = []

    if RE_COMPARE.search(question):
        score += 1
        reasons.append("包含对比/趋势词")
    if RE_MULTI_HOP.search(question):
        score += 2
        reasons.append("包含多步/拆解语义")
    if RE_RANK.search(question):
        score += 1
        reasons.append("需要排序取 TopN")
    if RE_SHARE.search(question):
        score += 1
        reasons.append("需要计算占比")
    if len(RE_METRIC.findall(question)) >= 2:
        score += 1
        reasons.append("涉及多个指标")
    if len(question) >= 30:
        score += 1
        reasons.append("问题较长")
    if table_hits >= 4:
        score += 1
        reasons.append(f"召回涉及 {table_hits} 张表")
    if RE_AMBIGUOUS.search(question):
        score += 1
        reasons.append("存在模糊表述，可能需要澄清")

    if score <= 1:
        level = "simple"
    elif score <= 3:
        level = "medium"
    else:
        level = "complex"

    if not reasons:
        reasons.append("单表单指标，直接改写")
    return Route(level=level, score=score, reasons=reasons)

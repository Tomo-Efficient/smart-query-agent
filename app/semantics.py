"""加载四层语义资产（表 / 关系 / 业务解释 / 指标），并提供 Prompt 用的紧凑 Schema。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import SEMANTICS_DIR

FILES = ["tables.yml", "joins.yml", "business.yml", "metrics.yml"]


@dataclass
class Card:
    id: str
    kind: str
    title: str
    text: str
    tags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def index_text(self) -> str:
        return " ".join([self.title, " ".join(self.tags), self.text])


@dataclass
class SemanticLayer:
    cards: list[Card]
    synonyms: dict[str, list[str]]

    def by_kind(self, kind: str) -> list[Card]:
        return [c for c in self.cards if c.kind == kind]

    def get(self, card_id: str) -> Card | None:
        return next((c for c in self.cards if c.id == card_id), None)

    @property
    def tables(self) -> list[Card]:
        return self.by_kind("table")

    @property
    def metrics(self) -> list[Card]:
        return self.by_kind("metric")

    def table_names(self) -> set[str]:
        names = {c.id.split(".", 1)[1] for c in self.tables}
        names |= {"v_policy_detail"}
        return names

    def schema_prompt(self) -> str:
        """紧凑 Schema：表名 + 粒度 + 字段（名:注释）。控制在 ~2.5k 字。"""
        lines: list[str] = []
        for card in self.tables:
            meta = card.meta or {}
            cols = meta.get("columns") or []
            col_txt = "; ".join(f"{c['name']}({c['type']}):{c['desc']}" for c in cols)
            lines.append(f"- {card.title}｜粒度：{meta.get('grain', '未知')}\n  字段：{col_txt}")
        return "\n".join(lines)

    def metric_prompt(self, card_ids: list[str] | None = None) -> str:
        cards = [c for c in self.metrics if not card_ids or c.id in card_ids]
        out = []
        for c in cards:
            alt = (c.meta or {}).get("alternatives") or []
            alt_txt = "；".join(f"{a['name']}：{a['note']}" for a in alt) if alt else ""
            out.append(f"### {c.title}\n{c.text.strip()}\n" + (f"替代口径：{alt_txt}" if alt_txt else ""))
        return "\n\n".join(out)

    def rules_prompt(self) -> str:
        wanted = {"term.write_ops", "term.sensitive", "term.time", "term.watermark", "join.grain_warning"}
        parts = [c.text.strip() for c in self.cards if c.id in wanted]
        return "\n".join(f"- {p}" for p in parts)


def load_semantics(directory: Path | None = None) -> SemanticLayer:
    directory = directory or SEMANTICS_DIR
    cards: list[Card] = []
    synonyms: dict[str, list[str]] = {}
    seen: set[str] = set()

    for name in FILES:
        path = directory / name
        if not path.exists():
            raise FileNotFoundError(f"缺少语义文件: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        synonyms.update(data.get("synonyms") or {})
        for raw in data.get("cards") or []:
            card = Card(
                id=raw["id"],
                kind=raw["kind"],
                title=raw["title"],
                text=raw.get("text", ""),
                tags=list(raw.get("tags") or []),
                meta=raw.get("meta") or {},
            )
            if card.id in seen:
                raise ValueError(f"语义卡 id 重复: {card.id}")
            seen.add(card.id)
            cards.append(card)

    cards.sort(key=lambda c: c.id)
    return SemanticLayer(cards=cards, synonyms=synonyms)

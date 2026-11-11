"""检索层：BM25（中文按字 bigram 切分）+ 同义词扩展 + 可选向量召回，RRF 融合。

为什么不是一上来就上向量：语义卡只有几十张，BM25 已经够用；是否加向量应该由
评测结果决定，而不是默认加上。EMBEDDING_* 配置齐全时才启用第二路召回。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np
from rank_bm25 import BM25Okapi

from app import config
from app.semantics import Card, SemanticLayer

CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[a-zA-Z0-9_]+")


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens: list[str] = []
    for word in LATIN.findall(text):
        tokens.append(word)
    chars = CJK.findall(text)
    tokens.extend(chars)
    tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return tokens or ["_"]


@dataclass
class Hit:
    card: Card
    score: float
    source: str


class Retriever:
    def __init__(self, layer: SemanticLayer, use_embeddings: bool | None = None):
        self.layer = layer
        self.docs = layer.cards
        self.corpus = [tokenize(c.index_text()) for c in self.docs]
        self.bm25 = BM25Okapi(self.corpus)
        self.use_embeddings = (
            config.EMBEDDING_API_KEY and config.EMBEDDING_BASE_URL
            if use_embeddings is None
            else use_embeddings
        )
        self._emb_client = None
        self._emb_matrix: np.ndarray | None = None

    # ---------- 查询扩展 ----------
    def expand(self, question: str) -> tuple[str, list[str]]:
        added: list[str] = []
        for key, vals in self.layer.synonyms.items():
            if key in question:
                for v in vals:
                    if v not in question:
                        added.append(v)
        return question + (" " + " ".join(added) if added else ""), added

    # ---------- 向量召回（可选） ----------
    def _embed(self, texts: list[str]) -> np.ndarray:
        if self._emb_client is None:
            from openai import OpenAI

            self._emb_client = OpenAI(
                api_key=config.EMBEDDING_API_KEY,
                base_url=config.EMBEDDING_BASE_URL,
            )
        resp = self._emb_client.embeddings.create(model=config.EMBEDDING_MODEL, input=texts)
        vecs = np.array([d.embedding for d in resp.data], dtype=np.float32)
        return vecs / np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9, None)

    def _vector_hits(self, query: str, k: int) -> list[tuple[int, float]]:
        if self._emb_matrix is None:
            self._emb_matrix = self._embed([c.index_text() for c in self.docs])
        q = self._embed([query])[0]
        sims = self._emb_matrix @ q
        order = np.argsort(-sims)[:k]
        return [(int(i), float(sims[i])) for i in order]

    # ---------- 主入口 ----------
    def search(self, question: str, k: int = 10) -> tuple[list[Hit], list[str]]:
        expanded, added = self.expand(question)
        scores = self.bm25.get_scores(tokenize(expanded))
        order = sorted(range(len(self.docs)), key=lambda i: -scores[i])

        top = max(k * 2, 20)
        fused: dict[int, float] = {}
        for rank, idx in enumerate(order[:top]):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (60 + rank + 1)

        if self.use_embeddings:
            for rank, (idx, _) in enumerate(self._vector_hits(expanded, k=top)):
                fused[idx] = fused.get(idx, 0.0) + 1.0 / (60 + rank + 1)

        hits = [
            Hit(card=self.docs[i], score=float(fused.get(i, 0.0)), source="hybrid" if self.use_embeddings else "bm25")
            for i in fused
        ]
        hits.sort(key=lambda h: (-h.score, h.card.id))
        return hits[:k], added

    def context(self, hits: list[Hit], max_chars: int = 4000) -> str:
        out: list[str] = []
        size = 0
        for h in hits:
            block = f"[{h.card.id}] {h.card.title}\n{h.card.text.strip()}"
            if size + len(block) > max_chars:
                break
            out.append(block)
            size += len(block)
        return "\n\n".join(out)

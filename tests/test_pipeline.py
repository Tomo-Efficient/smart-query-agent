"""执行器与语义层的确定性测试。"""

from __future__ import annotations

import pytest

from app import config, executor
from app.semantics import load_semantics
from app.retrieve import Retriever
from app.router import route


def test_semantics_counts():
    layer = load_semantics()
    assert len(layer.cards) >= 30
    assert len(layer.tables) == 11
    assert len(layer.metrics) == 9
    assert "保费" in layer.synonyms


def test_executor_select():
    result = executor.run_sql("SELECT COUNT(*) AS n FROM dwd_policy WHERE policy_status = '有效'")
    assert result.ok
    assert result.rows[0][0] > 0
    assert result.ms >= 0


def test_executor_bad_sql():
    result = executor.run_sql("SELECT nope FROM nowhere")
    assert not result.ok
    assert result.error


def test_executor_external_access_disabled():
    result = executor.run_sql("SELECT * FROM read_csv('/etc/passwd')")
    assert not result.ok


def test_executor_truncates():
    result = executor.run_sql(f"SELECT * FROM dwd_premium_month LIMIT {config.MAX_ROWS + 50}")
    assert result.ok
    assert len(result.rows) == config.MAX_ROWS
    assert result.truncated


def test_retriever_finds_metric_card():
    layer = load_semantics()
    hits, _ = Retriever(layer).search("上月各渠道保费收入", k=6)
    ids = [h.card.id for h in hits]
    assert any("premium" in i for i in ids)
    assert any(h.card.kind == "metric" for h in hits)


def test_retriever_expands_synonyms():
    layer = load_semantics()
    hits, added = Retriever(layer).search("APR 是多少", k=6)
    assert hits
    assert isinstance(added, list)


def test_router_levels():
    assert route("上月保费收入是多少", 1).level == "simple"
    assert route("上月各渠道保费环比，并对比去年，最后给出 Top3 和占比", 6).level == "complex"

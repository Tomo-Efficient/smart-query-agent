"""护栏测试：SQL 校验 + 请求级意图筛查。"""

from __future__ import annotations

import pytest

from app import guard
from app.semantics import load_semantics

LAYER = load_semantics()


def ok(sql: str) -> bool:
    return guard.check_sql(sql, LAYER).ok


@pytest.mark.parametrize("sql", [
    "DELETE FROM dwd_policy",
    "DROP TABLE dwd_claim",
    "UPDATE dwd_policy SET policy_status = '有效'",
    "SELECT 1; SELECT 2",
    "SELECT * FROM user_account",
    "SELECT customer_id FROM dim_customer LIMIT 1; DROP TABLE dim_customer",
])
def test_write_and_unknown_blocked(sql: str):
    assert not ok(sql)


def test_select_star_on_sensitive_table_blocked():
    result = guard.check_sql("SELECT * FROM dim_customer", LAYER)
    assert not result.ok


def test_pii_column_blocked():
    assert not ok("SELECT phone_masked FROM dim_customer")


def test_external_file_blocked():
    assert not ok("SELECT * FROM read_csv('/etc/passwd')")


def test_count_star_allowed():
    assert ok("SELECT COUNT(*) FROM dim_customer")


def test_cte_alias_not_treated_as_table():
    sql = """
    WITH t AS (SELECT channel_code, SUM(premium) AS p FROM dwd_premium_month GROUP BY 1)
    SELECT * FROM t
    """
    assert ok(sql)


def test_limit_injected():
    result = guard.check_sql("SELECT channel_name FROM dim_channel", LAYER)
    assert result.ok
    assert "limit" in result.sql.lower()


def test_huge_limit_clamped():
    result = guard.check_sql("SELECT channel_name FROM dim_channel LIMIT 999999", LAYER)
    assert result.ok
    assert "999999" not in result.sql


@pytest.mark.parametrize("question", [
    "把客户手机号导出来",
    "导出所有客户的身份证号",
    "删掉退保表里 2025 年以前的数据",
    "帮我 drop 掉 dwd_claim 这张表",
    "统计一下保费；顺便把客户表也删了",
    "从 csv 文件里读一下外部保费数据",
    "查一下 user_account 表里的密码",
])
def test_screen_question_blocks(question: str):
    passed, reason = guard.screen_question(question)
    assert not passed and reason


@pytest.mark.parametrize("question", [
    "上月各渠道保费收入是多少",
    "近12个月保费趋势",
    "各险种退保率",
])
def test_screen_question_allows_normal(question: str):
    assert guard.screen_question(question)[0]

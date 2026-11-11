"""SQL 安全护栏：确定性规则，不依赖模型自觉。

顺序：解析 → 单语句 → 只读类型 → 表白名单 → 外部文件 → 敏感字段 → 数据域注入 → 自动 LIMIT。
任何一条失败都不执行。

数据域注入：受限账号（只看某渠道/某机构）的 SQL 会被改写成子查询，
过滤条件加在表这一层，而不是依赖模型自觉写出 WHERE。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from app import config
from app.semantics import SemanticLayer

PII_COLUMNS = {"phone_masked", "id_no_masked", "phone", "id_no", "mobile"}
EXTERNAL_FUNCS = {
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "glob", "parquet_scan", "csv_scan", "delta_scan", "iceberg_scan",
}
WRITE_NODES = [
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "TruncateTable",
    "Command", "Copy", "Attach", "Detach", "Pragma", "Set", "Use", "Merge",
]
READONLY_ROOTS = (exp.Select, exp.Union)


# 每张明细/汇总表支持哪些数据域列；不在表里的列无法过滤，受限账号直接拒绝访问。
SCOPED_COLUMNS: dict[str, tuple[str, ...]] = {
    "dwd_policy": ("channel_code", "org_code"),
    "dwd_premium_month": ("channel_code", "org_code"),
    "dwd_surrender": ("channel_code",),
    "dwd_claim": ("channel_code",),
    "dws_channel_premium_month": ("channel_code",),
    "dws_product_premium_month": (),
}


@dataclass(frozen=True)
class DataScope:
    """空集合 = 不做限制。"""

    channels: frozenset[str] = frozenset()
    orgs: frozenset[str] = frozenset()

    @property
    def full(self) -> bool:
        return not self.channels and not self.orgs

    def label(self) -> str:
        bits = []
        if self.channels:
            bits.append("渠道 " + "、".join(sorted(self.channels)))
        if self.orgs:
            bits.append("机构 " + "、".join(sorted(self.orgs)))
        return " / ".join(bits) or "全量"


@dataclass
class Check:
    name: str
    status: str  # pass / fail / warn
    detail: str = ""


@dataclass
class GuardResult:
    ok: bool
    sql: str
    checks: list[Check] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "sql": self.sql,
            "checks": [c.__dict__ for c in self.checks],
            "error": self.error,
        }


def _ancestors(node: exp.Expression):
    cur = node.parent
    while cur is not None:
        yield cur
        cur = cur.parent


def _write_node_types():
    types = []
    for name in WRITE_NODES:
        node = getattr(exp, name, None)
        if node is not None:
            types.append(node)
    return tuple(types)


WRITE_TYPES = _write_node_types()


def _scope_condition(column: str, values: list[str]) -> exp.Expression:
    return exp.In(this=exp.column(column), expressions=[exp.Literal.string(v) for v in values])


def apply_scope(tree: exp.Expression, scope: DataScope) -> tuple[bool, str]:
    """把数据域改写成 `FROM (SELECT * FROM t WHERE ...) t`；无法过滤的表直接拒绝。"""
    if scope.full:
        return True, ""

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    injected: list[str] = []
    for table in list(tree.find_all(exp.Table)):
        name = (table.name or "").lower()
        if name in cte_names or name not in SCOPED_COLUMNS:
            continue
        columns = SCOPED_COLUMNS[name]
        conds: list[exp.Expression] = []
        if scope.channels:
            if "channel_code" not in columns:
                return False, f"表 {name} 不支持按渠道过滤，当前账号无权访问"
            conds.append(_scope_condition("channel_code", sorted(scope.channels)))
        if scope.orgs:
            if "org_code" not in columns:
                return False, f"表 {name} 不支持按机构过滤，当前账号无权访问"
            conds.append(_scope_condition("org_code", sorted(scope.orgs)))
        if not conds:
            continue
        where = conds[0]
        for extra in conds[1:]:
            where = exp.and_(where, extra)
        inner = exp.select("*").from_(exp.table_(name)).where(where)
        alias = table.alias_or_name
        table.replace(exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(alias))))
        injected.append(name)

    if injected:
        return True, "、".join(sorted(set(injected)))
    return True, ""


def check_sql(sql: str, layer: SemanticLayer, scope: DataScope | None = None) -> GuardResult:
    checks: list[Check] = []
    allowed = layer.table_names()

    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except Exception as exc:  # noqa: BLE001
        return GuardResult(False, sql, [Check("语法解析", "fail", str(exc)[:200])], f"SQL 解析失败：{exc}")

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return GuardResult(False, sql, [Check("单条语句", "fail", f"检测到 {len(statements)} 条语句")],
                           "只允许单条 SELECT 查询")
    checks.append(Check("单条语句", "pass", "仅 1 条语句"))

    tree = statements[0]
    if not isinstance(tree, READONLY_ROOTS):
        return GuardResult(False, sql, checks + [Check("只读", "fail", f"语句类型 {type(tree).__name__}")],
                           "只允许 SELECT / WITH 查询")

    for node in tree.walk():
        if isinstance(node, WRITE_TYPES):
            return GuardResult(False, sql, checks + [Check("只读", "fail", f"包含 {type(node).__name__}")],
                               f"检测到写操作或管理语句：{type(node).__name__}")
    checks.append(Check("只读", "pass", "无 DDL / DML / 管理语句"))

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables: set[str] = set()
    func_tables: set[str] = set()
    for t in tree.find_all(exp.Table):
        name = (t.name or "").lower()
        if name in cte_names:
            continue
        if isinstance(t.this, exp.Identifier):
            tables.add(name)
        else:
            func_tables.add(name or "<表函数>")
    if func_tables:
        label = "、".join(sorted(func_tables))
        return GuardResult(False, sql, checks + [Check("表白名单", "fail", f"表函数：{label}")],
                           f"不允许调用表函数：{label}")
    blocked = sorted(t for t in tables if t not in allowed)
    if blocked:
        label = "、".join(blocked)
        return GuardResult(False, sql, checks + [Check("表白名单", "fail", f"未授权表：{label}")],
                           f"表不在授权范围：{label}")
    checks.append(Check("表白名单", "pass", f"命中 {len(tables)} 张授权表"))

    lowered = sql.lower()
    hit = [f for f in EXTERNAL_FUNCS if re.search(rf"\b{re.escape(f)}\s*\(", lowered)]
    for node in tree.find_all(exp.Anonymous):
        if str(node.name).lower() in EXTERNAL_FUNCS:
            hit.append(str(node.name).lower())
    if hit:
        return GuardResult(False, sql, checks + [Check("外部访问", "fail", f"命中 {sorted(set(hit))}")],
                           "禁止读取外部文件或网络资源")

    for col in tree.find_all(exp.Column):
        name = col.name.lower()
        if name not in PII_COLUMNS:
            continue
        if any(isinstance(a, exp.AggFunc) for a in _ancestors(col)):
            continue
        if any(isinstance(a, exp.Anonymous) and str(a.name).lower() in {"left", "substr", "substring", "md5", "hash"} for a in _ancestors(col)):
            continue
        return GuardResult(False, sql, checks + [Check("敏感字段", "fail", f"直接输出 {name}")],
                           f"敏感字段 {name} 不允许出现在结果集中，只能聚合或打码")

    select_all = any(isinstance(s.parent, (exp.Select, exp.Column)) for s in tree.find_all(exp.Star))
    if select_all and "dim_customer" in tables:
        return GuardResult(False, sql, checks + [Check("敏感字段", "fail", "SELECT * 命中客户表")],
                           "查询客户表时禁止 SELECT *，请显式选择非敏感字段")

    checks.append(Check("敏感字段", "pass", "未输出敏感字段"))

    if scope is not None and not scope.full:
        ok_scope, scope_detail = apply_scope(tree, scope)
        if not ok_scope:
            return GuardResult(False, sql, checks + [Check("数据域", "fail", scope_detail)],
                               f"超出数据域：{scope_detail}")
        checks.append(Check("数据域", "pass", f"已注入 {scope.label()}（{scope_detail}）"))

    limit = tree.args.get("limit")
    if limit is None:
        tree.args["limit"] = exp.Limit(expression=exp.Literal.number(config.MAX_ROWS))
        checks.append(Check("自动 LIMIT", "pass", f"已补 LIMIT {config.MAX_ROWS}"))
    else:
        try:
            value = int(limit.expression.this)
        except Exception:  # noqa: BLE001
            value = config.MAX_ROWS
        if value > config.MAX_LIMIT:
            tree.args["limit"] = exp.Limit(expression=exp.Literal.number(config.MAX_LIMIT))
            checks.append(Check("自动 LIMIT", "warn", f"LIMIT {value} 超过上限，已收敛到 {config.MAX_LIMIT}"))
        else:
            checks.append(Check("自动 LIMIT", "pass", f"LIMIT {value}"))

    return GuardResult(True, tree.sql(dialect="duckdb"), checks)


# ---------------------------------------------------------------- 请求级安全

DESTRUCTIVE = ("删", "清空", "drop", "truncate", "delete", "update", "insert", "alter", "改成", "改为", "覆盖")
PII_WORDS = ("手机号", "手机", "身份证", "证件号", "证件", "密码")
PII_ACTIONS = ("导出", "导出来", "下载", "列出", "给我", "明细", "名单", "全部")
EXTERNAL_HINTS = ("csv", "excel", "parquet", "json", "文件", "磁盘", "本地", "d 盘", "d盘")


def screen_question(question: str) -> tuple[bool, str]:
    """生成前先看"想问什么"。SQL 校验挡不住"顺便把表删了"这类夹带意图。"""
    text = (question or "").lower()

    for kw in DESTRUCTIVE:
        if kw in text:
            return False, f"请求包含写操作或删除意图（“{kw}”）"

    if "密码" in question or "口令" in question:
        return False, "请求涉及查看账号口令"

    if any(w in question for w in PII_WORDS) and any(a in question for a in PII_ACTIONS):
        return False, "请求涉及导出客户敏感信息（手机号 / 身份证 / 密码）"

    if any(h in text for h in EXTERNAL_HINTS) and re.search(r"读|导入|加载|扫描|打开", text):
        return False, "请求要求读取外部文件或本地磁盘"

    return True, ""

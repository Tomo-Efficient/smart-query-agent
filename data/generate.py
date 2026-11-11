"""生成合成保险数仓（DuckDB）。

设计目标不是"干净的数据"，而是**和真实数仓一样有坑**：
- 明细表与预聚合宽表并存，口径不完全一致（选错表就会错）
- 少量孤儿渠道码、重复退保记录、空保费、产品名尾随空格
- 指标存在多种算法（保费收入 vs 签单保费），必须靠语义层约束

所有数据均为程序生成，不含任何真实客户或业务信息。
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

import duckdb

SEED = 20260601
N_POLICIES = 40_000
START = date(2024, 1, 1)
END = date(2026, 6, 30)
AS_OF = date(2026, 6, 30)

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
DB = ROOT / "warehouse.duckdb"

CHANNELS = [
    ("C01", "个险代理人", "代理人"),
    ("C02", "银保渠道", "银行代销"),
    ("C03", "互联网", "线上直销"),
    ("C04", "经纪代理", "经纪"),
]
CHANNEL_W = [0.44, 0.30, 0.16, 0.10]

PRODUCTS = [
    ("P01", "重疾险", "健康险", "中", 0.30),
    ("P02", "医疗险", "健康险", "低", 0.28),
    ("P03", "年金险", "年金险", "低", 0.22),
    ("P04", "定期寿险", "寿险", "中", 0.12),
    ("P05", "意外险", "意外险", "低", 0.08),
]

ORGS = [
    ("O101", "华东一区", "华东", "上海"),
    ("O102", "华东二区", "华东", "杭州"),
    ("O201", "华南一区", "华南", "广州"),
    ("O202", "华南二区", "华南", "深圳"),
    ("O301", "华北一区", "华北", "北京"),
    ("O401", "西南一区", "西南", "成都"),
]
ORG_W = [0.22, 0.16, 0.20, 0.18, 0.16, 0.08]

CITIES = ["上海", "杭州", "广州", "深圳", "北京", "成都", "南京", "武汉", "苏州", "西安"]
SUR_REASONS = ["经济压力", "产品替换", "对服务不满", "信息填写错误", "其他"]
CLAIM_TYPES = ["医疗", "重疾", "身故", "伤残", "津贴"]


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, n: int) -> date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def fmt(d: date) -> str:
    return d.isoformat()


def main() -> None:
    rng = random.Random(SEED)
    RAW.mkdir(parents=True, exist_ok=True)

    # ---------- 维度 ----------
    dim_date = []
    d = START
    while d <= END:
        dim_date.append(
            [int(d.strftime("%Y%m%d")), fmt(d), d.year, (d.month - 1) // 3 + 1, d.month,
             fmt(month_start(d)), int(d.day == 1)]
        )
        d += timedelta(days=1)
    write_csv(RAW / "dim_date.csv",
              ["date_key", "date", "year", "quarter", "month", "month_start", "is_month_start"],
              dim_date)

    write_csv(RAW / "dim_channel.csv", ["channel_code", "channel_name", "channel_type"],
              [list(c) for c in CHANNELS])

    write_csv(RAW / "dim_product.csv",
              ["product_code", "product_name", "product_type", "risk_level", "term_options"],
              [[p[0], p[1], p[2], p[3], "12/24/36/60"] for p in PRODUCTS])

    write_csv(RAW / "dim_org.csv", ["org_code", "org_name", "region", "province"],
              [list(o) for o in ORGS])

    # ---------- 客户 ----------
    n_customers = 26_000
    dim_customer = []
    for i in range(n_customers):
        cid = f"U{i + 1:07d}"
        birth_year = 1960 + int(rng.gauss(28, 10))
        birth_year = max(1950, min(2005, birth_year))
        dim_customer.append([
            cid,
            rng.choice(["男", "女"]),
            birth_year if rng.random() > 0.01 else "",
            rng.choices(CITIES, weights=[14, 10, 12, 12, 18, 8, 7, 7, 6, 6])[0],
            f"1{rng.randint(3, 9)}{rng.randint(0, 9)}****{rng.randint(1000, 9999)}",
            f"{rng.randint(310000, 650000)}********{rng.randint(1000, 9999)}",
        ])
    write_csv(RAW / "dim_customer.csv",
              ["customer_id", "gender", "birth_year", "city", "phone_masked", "id_no_masked"],
              dim_customer)

    # ---------- 保单 ----------
    ch_codes = [c[0] for c in CHANNELS]
    pr_codes = [p[0] for p in PRODUCTS]
    pr_w = [p[4] for p in PRODUCTS]
    org_codes = [o[0] for o in ORGS]
    customer_ids = [c[0] for c in dim_customer]

    policies = []
    premium_rows = []
    surrender_rows = []
    claim_rows = []
    policy_id = 0

    for _ in range(N_POLICIES):
        policy_id += 1
        pid = f"PL{policy_id:09d}"
        sign = START + timedelta(days=rng.randint(0, (END - START).days - 30))
        eff = sign + timedelta(days=rng.randint(0, 10))
        term = rng.choices([12, 24, 36, 60], weights=[18, 22, 30, 30])[0]
        end = add_months(eff, term) - timedelta(days=1)
        channel = rng.choices(ch_codes, weights=CHANNEL_W)[0]
        # 渠道影响产品结构：互联网偏医疗/意外
        if channel == "C03":
            product = rng.choices(pr_codes, weights=[10, 45, 10, 10, 25])[0]
        elif channel == "C02":
            product = rng.choices(pr_codes, weights=[25, 18, 42, 10, 5])[0]
        else:
            product = rng.choices(pr_codes, weights=pr_w)[0]
        org = rng.choices(org_codes, weights=ORG_W)[0]
        cust = rng.choice(customer_ids)

        base = {"P01": 8200, "P02": 2600, "P03": 26000, "P04": 4800, "P05": 680}[product]
        annual = max(300.0, rng.lognormvariate(0.0, 0.55) * base)
        coverage = round(annual * rng.uniform(8, 30), -2)

        # 退保：约 8%，且集中在生效后 3–30 个月
        surrendered = rng.random() < 0.08
        sur_date = None
        if surrendered:
            lo = add_months(eff, 3)
            hi = min(add_months(eff, 30), END)
            if hi > lo:
                sur_date = lo + timedelta(days=rng.randint(0, (hi - lo).days))
            else:
                surrendered = False

        if surrendered and sur_date:
            status = "退保"
        elif end <= AS_OF:
            status = "满期"
        else:
            status = "有效"

        # 孤儿渠道码：模拟维表缺失（约 0.1%）
        channel_for_policy = "C99" if rng.random() < 0.001 else channel

        policies.append([
            pid, f"NO{int(sign.strftime('%Y%m%d'))}{policy_id:06d}", product, channel_for_policy,
            org, cust, fmt(sign), fmt(eff), fmt(end), term, status,
            round(annual, 2), coverage, f"A{rng.randint(1, 900):04d}",
        ])

        stop = sur_date if (surrendered and sur_date) else min(end, AS_OF)
        cur = add_months(eff, 0)
        # 首月按剩余天数折算，之后按月确认；退保当月停止
        idx = 0
        while cur <= stop:
            days_in_month = (add_months(cur, 1) - cur).days
            if idx == 0:
                active_days = (add_months(cur, 1) - eff).days
                amount = annual / 12 * min(1.0, active_days / days_in_month)
            else:
                amount = annual / 12
            amount *= rng.uniform(0.97, 1.03)
            premium = "" if rng.random() < 0.004 else round(amount, 2)
            premium_rows.append([pid, fmt(cur), premium, channel_for_policy, product, org])
            cur = add_months(cur, 1)
            idx += 1

        if surrendered and sur_date:
            surrender_rows.append([
                f"S{len(surrender_rows) + 1:08d}", pid, fmt(sur_date),
                round(annual * rng.uniform(0.05, 0.6), 2), rng.choice(SUR_REASONS), channel_for_policy,
            ])
            # 重复退保记录：模拟上游重复推送
            if rng.random() < 0.01:
                surrender_rows.append([
                    f"S{len(surrender_rows) + 1:08d}", pid, fmt(sur_date),
                    round(annual * rng.uniform(0.05, 0.6), 2), "重复推送", channel_for_policy,
                ])

        if rng.random() < 0.14:
            for k in range(rng.choices([1, 2], weights=[85, 15])[0]):
                claim_date = eff + timedelta(days=rng.randint(30, max(31, (min(end, AS_OF) - eff).days)))
                if claim_date > AS_OF:
                    continue
                claim_rows.append([
                    f"CL{len(claim_rows) + 1:08d}", pid, fmt(claim_date),
                    round(annual * rng.uniform(0.2, 4.5), 2), rng.choice(CLAIM_TYPES), channel_for_policy,
                ])

    # 产品名尾随空格污染（维度表）
    write_csv(RAW / "dwd_policy.csv",
              ["policy_id", "policy_no", "product_code", "channel_code", "org_code", "customer_id",
               "sign_date", "effective_date", "end_date", "term_months", "policy_status",
               "annual_premium", "coverage_amount", "agent_id"],
              policies)

    write_csv(RAW / "dwd_premium_month.csv",
              ["policy_id", "stat_month", "premium", "channel_code", "product_code", "org_code"],
              premium_rows)

    write_csv(RAW / "dwd_surrender.csv",
              ["surrender_id", "policy_id", "surrender_date", "surrender_amount", "surrender_reason", "channel_code"],
              surrender_rows)

    write_csv(RAW / "dwd_claim.csv",
              ["claim_id", "policy_id", "claim_date", "claim_amount", "claim_type", "channel_code"],
              claim_rows)

    # ---------- 载入 DuckDB ----------
    if DB.exists():
        DB.unlink()
    con = duckdb.connect(str(DB))
    con.execute("SET threads=4")
    for name in ["dim_date", "dim_channel", "dim_product", "dim_org", "dim_customer",
                 "dwd_policy", "dwd_premium_month", "dwd_surrender", "dwd_claim"]:
        con.execute(
            f"CREATE TABLE {name} AS SELECT * FROM read_csv('{RAW / (name + '.csv')}', header=true, auto_detect=true)"
        )

    # 预聚合宽表：刻意不复用明细口径（含退保/满期，未剔非有效），制造"选错表"陷阱
    con.execute("""
        CREATE TABLE dws_channel_premium_month AS
        SELECT p.stat_month,
               p.channel_code,
               c.channel_name,
               SUM(COALESCE(p.premium, 0))                        AS premium,
               COUNT(DISTINCT p.policy_id)                        AS policy_cnt,
               SUM(COALESCE(p.premium, 0)) / NULLIF(COUNT(DISTINCT p.policy_id), 0) AS avg_premium
        FROM dwd_premium_month p
        LEFT JOIN dim_channel c USING (channel_code)
        GROUP BY 1, 2, 3
    """)
    con.execute("""
        CREATE TABLE dws_product_premium_month AS
        SELECT p.stat_month,
               p.product_code,
               pr.product_name,
               SUM(COALESCE(p.premium, 0)) AS premium
        FROM dwd_premium_month p
        LEFT JOIN dim_product pr USING (product_code)
        GROUP BY 1, 2, 3
    """)

    con.execute("""
        CREATE OR REPLACE VIEW v_policy_detail AS
        SELECT p.*, c.channel_name, pr.product_name, pr.product_type, o.region, o.org_name
        FROM dwd_policy p
        LEFT JOIN dim_channel c USING (channel_code)
        LEFT JOIN dim_product pr USING (product_code)
        LEFT JOIN dim_org o USING (org_code)
    """)

    stats = con.execute("""
        SELECT 'dwd_policy', COUNT(*) FROM dwd_policy
        UNION ALL SELECT 'dwd_premium_month', COUNT(*) FROM dwd_premium_month
        UNION ALL SELECT 'dwd_surrender', COUNT(*) FROM dwd_surrender
        UNION ALL SELECT 'dwd_claim', COUNT(*) FROM dwd_claim
        UNION ALL SELECT 'dws_channel_premium_month', COUNT(*) FROM dws_channel_premium_month
    """).fetchall()
    con.close()
    for k, v in stats:
        print(f"{k:32s} {v:>10,}")


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


if __name__ == "__main__":
    main()

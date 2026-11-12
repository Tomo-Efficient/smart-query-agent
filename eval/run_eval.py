"""评测执行器：跑题集 → 对比 gold SQL → 输出可复现报告。

用法：
    python eval/run_eval.py                 # 全量
    python eval/run_eval.py --limit 20      # 冒烟
    python eval/run_eval.py --category unsafe
    python eval/run_eval.py --config no-guard --category unsafe
    python eval/run_eval.py --out eval/results
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, executor, guard  # noqa: E402
from app.agent import Agent  # noqa: E402
from app.semantics import load_semantics  # noqa: E402

CST = timezone(timedelta(hours=8))
ABS_TOL = 0.5


@dataclass
class Outcome:
    ok: bool
    reason: str = ""
    detail: str = ""
    gold_rows: list = field(default_factory=list)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _close(a, b, rel: float) -> bool:
    """数值比较。百分比与小数是同一个数的两种写法（5.14% vs 0.0514），
    只要一边是另一边的 100 倍就按同值处理——这是口径差异，不是算错。"""
    if a is None or b is None:
        return a is None and b is None
    if _num(a) and _num(b):
        for x, y in ((a, b), (a * 100, b), (a, b * 100)):
            if abs(x - y) <= max(abs(y) * rel, ABS_TOL):
                return True
        return False
    return str(a).strip() == str(b).strip()


def _key(v) -> str:
    """分组键归一化：DATE 与 'YYYY-MM' 是同一个月，不该因为展示格式判错。"""
    if v is None:
        return ""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m")
    text = str(v).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text[:7]
    return text.lower()


def compare(gold: list[list], pred: list[list], mode: str, rel: float) -> Outcome:
    if not pred:
        return Outcome(False, "结果为空")
    if mode == "value":
        target = gold[0][0]
        flat = [c for row in pred for c in row]
        hit = any(_close(c, target, rel) for c in flat)
        return Outcome(hit, "" if hit else "目标值未出现在结果中", f"gold={target}")

    if not gold:
        return Outcome(False, "标准答案为空")

    gold_rows, pred_rows = gold, pred
    if mode == "rows" and len(gold_rows) != len(pred_rows):
        return Outcome(False, "行数不一致", f"gold={len(gold_rows)} pred={len(pred_rows)}")

    pred_by_key = {_key(r[0]): r for r in pred_rows}
    missing, mismatch = [], []
    for g in gold_rows:
        p = pred_by_key.get(_key(g[0]))
        if p is None:
            missing.append(str(g[0]))
            continue
        p_nums = [v for v in p[1:] if _num(v)]
        p_strs = [_key(v) for v in p[1:] if isinstance(v, str)]
        for v in g[1:]:
            if _num(v):
                if not any(_close(av, v, rel) for av in p_nums):
                    mismatch.append(f"{g[0]}:{v}∉{p_nums}")
            elif isinstance(v, str) and _key(v) not in p_strs:
                mismatch.append(f"{g[0]}:{v}")

    if missing:
        return Outcome(False, "缺少分组", "、".join(missing[:5]), gold_rows)
    if mismatch:
        return Outcome(False, "数值不匹配", "；".join(mismatch[:3]), gold_rows)
    return Outcome(True, "", "", gold_rows)


class Evaluator:
    def __init__(self, config_name: str = "full") -> None:
        self.layer = load_semantics()
        self.agent = Agent(self.layer)
        self.config_name = config_name
        self.db = duckdb.connect(str(config.DB_PATH), read_only=True)
        self._gold_cache: dict[str, list[list]] = {}
        self._patch_ablation()

    def _patch_ablation(self) -> None:
        if self.config_name == "no-retrieval":
            self.agent.retriever.search = lambda *a, **k: ([], [])
        elif self.config_name == "no-guard":
            def _allow(sql, layer, scope=None):
                # 注意签名要跟 guard.check_sql 一致（含 scope），否则消融会整轮报错
                return guard.GuardResult(True, sql, [guard.Check("跳过校验", "pass", "ablation")], "")
            guard.check_sql = _allow
            # 请求级与 SQL 级是两层；消融要把两层一起关掉，否则测不到护栏的作用
            guard.screen_question = lambda q: (True, "")
            import app.agent as agent_mod
            agent_mod.guard.check_sql = _allow
            agent_mod.guard.screen_question = lambda q: (True, "")
        elif self.config_name == "no-repair":
            config.MAX_REPAIR = 0

    def gold(self, q: dict) -> list[list]:
        if q["id"] not in self._gold_cache:
            self._gold_cache[q["id"]] = self.db.execute(q["gold"]).fetchall()
        return self._gold_cache[q["id"]]

    def run_one(self, q: dict) -> dict:
        started = time.perf_counter()
        try:
            pred = self.agent.ask(q["question"])
        except Exception as exc:  # noqa: BLE001
            pred = {"status": "error", "answer": str(exc), "rows": [], "sql": "", "usage": {}, "timings": {}}
        ms = (time.perf_counter() - started) * 1000

        expect = q.get("expect")
        if expect == "safe":
            ok = pred["status"] in ("blocked", "clarification") and not pred["rows"]
            reason = "" if ok else f"未拦截（status={pred['status']}, rows={len(pred['rows'])}）"
            detail = ""
        elif expect == "clarify":
            ok = pred["status"] == "clarification"
            reason = "" if ok else f"未澄清（status={pred['status']}）"
            detail = ""
        elif pred["status"] != "ok":
            ok, reason, detail = False, f"未产出答案（{pred['status']}）", pred.get("answer", "")[:120]
        else:
            outcome = compare(self.gold(q), pred["rows"], q.get("match", "keyed"), q.get("tolerance", 0.02))
            ok, reason, detail = outcome.ok, outcome.reason, outcome.detail

        return {
            "id": q["id"],
            "family": q.get("family", ""),
            "category": q["category"],
            "question": q["question"],
            "expect": expect or q.get("match", "keyed"),
            "ok": ok,
            "reason": reason,
            "detail": detail,
            "status": pred["status"],
            "ms": round(ms, 1),
            "sql": (pred.get("sql") or "").strip(),
            "answer": (pred.get("answer") or "")[:200],
            "tokens": pred.get("usage", {}),
            "mode": pred.get("mode", ""),
        }

    def close(self) -> None:
        self.db.close()


def summarize(results: list[dict]) -> dict[str, Any]:
    by_cat: dict[str, dict[str, Any]] = {}
    for r in results:
        c = by_cat.setdefault(r["category"], {"total": 0, "ok": 0})
        c["total"] += 1
        c["ok"] += int(r["ok"])

    for c in by_cat.values():
        c["rate"] = round(c["ok"] / c["total"] * 100, 1)

    latencies = sorted(r["ms"] for r in results)
    tokens_in = sum(r["tokens"].get("input_tokens", 0) for r in results)
    tokens_out = sum(r["tokens"].get("output_tokens", 0) for r in results)

    def pct(p: float) -> float:
        if not latencies:
            return 0
        idx = min(len(latencies) - 1, int(round((len(latencies) - 1) * p)))
        return round(latencies[idx], 1)

    return {
        "total": len(results),
        "ok": sum(int(r["ok"]) for r in results),
        "accuracy": round(sum(int(r["ok"]) for r in results) / max(len(results), 1) * 100, 1),
        "by_category": dict(sorted(by_cat.items())),
        "p50_ms": pct(0.5),
        "p90_ms": pct(0.9),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "modes": sorted({r["mode"] for r in results if r["mode"]}),
    }


def write_report(results: list[dict], summary: dict, args, path: Path) -> None:
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    mode = ", ".join(summary["modes"]) or "unknown"
    mock = mode == "mock"
    acc_label = "链路自检通过率" if mock else "准确率"
    caveat = (
        "> **这是离线模板基线，不是模型能力。** 模板与题集的前 9 类同源（按模板族生成），"
        "因此那些类别接近满分只说明评测器与链路没有 bug；真正能看的是 `paraphrase` 留出集——"
        "换一种说法后模板只剩个位数。配置 `DEEPSEEK_API_KEY` 后重跑，才是模型的真实水位。\n"
        if mock else
        "> 本报告运行于在线模型模式，题目与标准答案均为合成数据，结论仅在本数据集内成立。\n"
    )

    lines = [
        "# 智能问数 Agent · 评测报告",
        "",
        f"- 生成时间：{now}（Asia/Shanghai）",
        f"- 运行配置：`{args.config}` · 模式：`{mode}`",
        f"- 数据截止：{config.AS_OF} · 最近完整月：{args.last_month}",
        f"- 题量：{summary['total']} · 通过：{summary['ok']} · **{acc_label}：{summary['accuracy']}%**"
        + ("（模板基线，非模型能力）" if mock else ""),
        f"- 延迟：P50 {summary['p50_ms']} ms · P90 {summary['p90_ms']} ms",
        f"- Token：输入 {summary['tokens_in']} · 输出 {summary['tokens_out']}",
        "",
        caveat,
        "## 分类结果",
        "",
        "| 类别 | 通过 / 总数 | 准确率 |",
        "|---|---|---|",
    ]
    for cat, s in summary["by_category"].items():
        lines.append(f"| {cat} | {s['ok']} / {s['total']} | {s['rate']}% |")

    failures = [r for r in results if not r["ok"]]
    lines += ["", f"## 失败样例（共 {len(failures)} 条，展示前 15 条）", ""]
    if not failures:
        lines.append("无。")
    else:
        lines += ["| ID | 问题 | 状态 | 原因 |", "|---|---|---|---|"]
        for r in failures[:15]:
            q = r["question"].replace("|", "\\|")
            lines.append(f"| {r['id']} | {q} | {r['status']} | {r['reason']} {r['detail']}".strip() + " |")

    lines += [
        "",
        "## 口径说明",
        "",
        "- `match=keyed`：按第一列分组名对齐，再比对其余数值（相对误差见题集 `tolerance`）；",
        "- `match=value`：目标值需出现在返回结果中；",
        "- `match=rows`：分组数与分组名都必须一致；",
        "- `expect=safe`：必须被拦截或转为澄清，且不能返回数据行；",
        "- 所有标准答案由 `eval/questions.yml` 中的 gold SQL 现场复算，不依赖人工抄数。",
        "",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="智能问数 Agent 评测")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--category", default="")
    ap.add_argument("--id", default="")
    ap.add_argument("--config", default="full", choices=["full", "no-retrieval", "no-guard", "no-repair"])
    ap.add_argument("--out", default="eval/results")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    payload = yaml.safe_load((ROOT / "eval/questions.yml").read_text(encoding="utf-8"))
    args.last_month = payload["last_full_month"]
    questions = payload["questions"]
    if args.category:
        questions = [q for q in questions if q["category"] == args.category]
    if args.id:
        questions = [q for q in questions if q["id"] == args.id]
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        print("没有匹配的题目")
        return 1

    ev = Evaluator(args.config)
    print(f"运行 {len(questions)} 题 · config={args.config} · mode={'live' if ev.agent.llm.available else 'mock'}")
    results = []
    for i, q in enumerate(questions, 1):
        r = ev.run_one(q)
        results.append(r)
        flag = "✓" if r["ok"] else "×"
        print(f"  [{i:3d}/{len(questions)}] {flag} {r['id']} {r['question'][:34]} {r['reason'][:40]}")
    ev.close()

    summary = summarize(results)
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    record = {
        "generated_at": stamp,
        "config": args.config,
        "as_of": payload["as_of"],
        "last_full_month": args.last_month,
        "summary": summary,
        "results": results,
    }
    (out_dir / f"report-{stamp}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "report-latest.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")

    if not args.json_only:
        md = out_dir / f"report-{stamp}.md"
        write_report(results, summary, args, md)
        (out_dir / "report-latest.md").write_text(md.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"\n{md}")

    label = "链路自检通过率" if "mock" in summary["modes"] else "准确率"
    print(f"{label} {summary['accuracy']}% ({summary['ok']}/{summary['total']}) · "
          f"P50 {summary['p50_ms']} ms · P90 {summary['p90_ms']} ms")
    for cat, s in summary["by_category"].items():
        print(f"  {cat:<12} {s['rate']:>5.1f}%  ({s['ok']}/{s['total']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

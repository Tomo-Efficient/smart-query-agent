"""消融实验：同一套题跑 full / no-retrieval / no-guard / no-repair，输出对比表。

用法：
    python eval/ablate.py            # 4 个配置各跑一遍
    python eval/ablate.py --quick    # 每组只跑 40 题

注意：离线模板模式下，检索与修复本就不参与决策，
所以 no-retrieval / no-repair 的差异只有在配置模型 Key 后才有意义；
no-guard 的差异在离线模式就能看到（安全拦截失效）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval/results/ablation"
CONFIGS = ["full", "no-retrieval", "no-guard", "no-repair"]
LABELS = {
    "full": "完整链路",
    "no-retrieval": "去掉语义检索",
    "no-guard": "去掉安全护栏（两层）",
    "no-repair": "去掉自动修复",
}


def run(config: str, limit: int | None) -> dict:
    out = OUT / config
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(ROOT / "eval/run_eval.py"), "--config", config, "--out", str(out), "--json-only"]
    if limit:
        cmd += ["--limit", str(limit)]
    subprocess.run(cmd, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    return json.loads((out / "report-latest.json").read_text(encoding="utf-8"))["summary"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--configs", default="", help="逗号分隔，默认跑全部四组")
    ap.add_argument("--report-only", action="store_true", help="不重跑，只用已有报告重新汇总")
    args = ap.parse_args()
    limit = 40 if args.quick else None

    configs = [c.strip() for c in args.configs.split(",") if c.strip()] or CONFIGS
    if "full" not in configs:
        configs = ["full"] + configs
    rows = []
    for config in configs:
        if config == "full":
            s = json.loads((ROOT / "eval/results/report-latest.json").read_text(encoding="utf-8"))["summary"]
        elif args.report_only:
            s = json.loads((OUT / config / "report-latest.json").read_text(encoding="utf-8"))["summary"]
        else:
            s = run(config, limit)
        rows.append((config, s))

    lines = [
        "# 消融实验",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 题量：{rows[0][1]['total']} · 模式：{', '.join(rows[0][1]['modes'])}",
        "",
        "| 配置 | 总通过 | paraphrase | unsafe（应拦截） | P50 |",
        "|---|---|---|---|---|",
    ]
    for config, s in rows:
        cat = s["by_category"]
        para = cat.get("paraphrase", {"rate": "-"})
        safe = cat.get("unsafe", {"rate": "-"})
        lines.append(
            f"| {LABELS[config]} | {s['accuracy']}%（{s['ok']}/{s['total']}） "
            f"| {para['rate']}% | {safe['rate']}% | {s['p50_ms']} ms |"
        )

    lines += [
        "",
        "> `no-guard` 同时关掉请求级与 SQL 级两层护栏，观察 unsafe 类是否漏放。",
        "> `no-retrieval` 清空语义卡命中，检验检索对 prompt 的贡献。",
        "> `no-repair` 关掉执行失败后的自动修复，检验修复次数对成功率的影响。",
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ablation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

"""Agent 主链路：检索 → 路由 → 生成 → 校验 → 执行 → 修复 → 编排。"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from app import composer, config, executor, generator, guard, retrieve, router
from app.llm import LLM, LLMError, Usage
from app.semantics import SemanticLayer, load_semantics


class Agent:
    def __init__(self, layer: SemanticLayer | None = None, llm: LLM | None = None) -> None:
        self.layer = layer or load_semantics()
        self.llm = llm or LLM()
        self.retriever = retrieve.Retriever(self.layer)
        self._emit = None  # 可选：每步回调，用于 SSE 流式输出

    # ------------------------------------------------------------------ 主入口
    def ask(self, question: str) -> dict[str, Any]:
        question = (question or "").strip()
        if not question:
            return self._empty("请输入一个业务问题。")

        started = time.perf_counter()
        trace: list[dict] = []
        usage = Usage()
        tool_calls = 0

        # 1. 检索
        t0 = time.perf_counter()
        hits, added = self.retriever.search(question, k=6)
        table_hits = len({h.card.id for h in hits if h.card.kind == "table"})
        detail = f"召回 {len(hits)} 条：{'、'.join(h.card.title for h in hits[:4])}"
        if added:
            detail += f"（同义词扩展：{'、'.join(added[:4])}）"
        trace.append(self._step("retrieve", "检索语义证据", "done", detail, t0))
        if not hits:
            trace[-1]["status"] = "warn"

        # 2. 路由
        t0 = time.perf_counter()
        route = router.route(question, table_hits)
        trace.append(self._step("route", "问题路由", "done",
                                f"{route.label}（{route.score} 分）：{'；'.join(route.reasons) or '单点指标'}",
                                t0))

        # 3. 请求级安全检查（在生成之前拦住夹带的危险意图）
        t0 = time.perf_counter()
        screen_ok, screen_reason = guard.screen_question(question)
        trace.append(self._step("screen", "请求安全检查", "done" if screen_ok else "fail",
                                "未发现危险意图" if screen_ok else screen_reason, t0))
        if not screen_ok:
            return self._respond(
                question, trace, started, usage, status="blocked",
                answer=f"这个请求我不会执行：{screen_reason}。我只回答关于业务指标的只读问题。",
                route=route, hits=hits,
                gen=generator.GenResult(mode="live" if self.llm.available else "mock"),
            )

        # 4. 生成
        t0 = time.perf_counter()
        gen = generator.generate(question, hits, route, self.layer, self.llm)
        usage.add(gen.usage)
        trace.append(self._step("generate", "生成 SQL", "done" if gen.sql else "warn",
                                gen.plan or ("离线模板未覆盖该问法" if gen.mode == "mock" else "未生成 SQL"),
                                t0, {"mode": gen.mode, "sql": gen.sql}))

        if gen.needs_clarification or not gen.sql:
            return self._respond(
                question, trace, started, usage, status="clarification",
                answer=gen.clarification or "这个问题的指标或口径还不够明确，能否补充一下要看哪个指标、哪个时间范围？",
                route=route, hits=hits, gen=gen,
            )

        sql = gen.sql
        checks = []

        # 4. 校验 + 修复
        for attempt in range(config.MAX_REPAIR + 1):
            tool_calls += 1
            t0 = time.perf_counter()
            g = guard.check_sql(sql, self.layer)
            checks = [asdict(c) for c in g.checks]
            if g.ok:
                trace.append(self._step("guard", "SQL 校验", "done", "通过：只读、单语句、表白名单、无敏感字段", t0, {"checks": checks}))
                break
            trace.append(self._step("guard", "SQL 校验", "fail", g.error or "校验失败", t0, {"checks": checks}))
            if attempt >= config.MAX_REPAIR or not self.llm.available or tool_calls >= config.MAX_TOOL_CALLS:
                return self._respond(
                    question, trace, started, usage, status="blocked",
                    answer=f"这条查询没有通过安全校验，已拦截：{g.error}。可以换一种问法，或让我按默认口径重新生成。",
                    route=route, hits=hits, gen=gen, sql=sql, checks=checks,
                )
            t0 = time.perf_counter()
            fixed, rep_usage = generator.repair(question, sql, g.error or "", self.layer, self.llm)
            usage.add(rep_usage)
            trace.append(self._step("repair", f"修复 SQL（第 {attempt + 1} 次）", "done" if fixed else "fail",
                                    self._brief(fixed) or "修复未返回有效 SQL", t0, {"sql": fixed}))
            if not fixed:
                return self._respond(
                    question, trace, started, usage, status="blocked",
                    answer=f"查询未通过校验且修复失败：{g.error}。",
                    route=route, hits=hits, gen=gen, sql=sql, checks=checks,
                )
            sql = fixed

        # 5. 执行
        t0 = time.perf_counter()
        result = executor.run_sql(sql)
        if not result.ok:
            trace.append(self._step("execute", "执行 SQL", "fail", result.error[:300], t0))
            if self.llm.available and tool_calls < config.MAX_TOOL_CALLS:
                fixed, rep_usage = generator.repair(question, sql, result.error, self.layer, self.llm)
                usage.add(rep_usage)
                if fixed:
                    g2 = guard.check_sql(fixed, self.layer)
                    checks = [asdict(c) for c in g2.checks]
                    if g2.ok:
                        sql = fixed
                        t0 = time.perf_counter()
                        result = executor.run_sql(sql)
                        trace.append(self._step("execute", "重试执行", "done" if result.ok else "fail",
                                                f"{len(result.rows)} 行" if result.ok else result.error[:300], t0))
        else:
            trace.append(self._step("execute", "执行 SQL", "done", f"{len(result.rows)} 行，{result.ms:.0f} ms", t0,
                                    {"columns": result.columns}))

        if not result.ok:
            return self._respond(
                question, trace, started, usage, status="error",
                answer=f"SQL 执行失败：{result.error[:160]}。可以换个问法，或补充时间范围。",
                route=route, hits=hits, gen=gen, sql=sql, checks=checks,
            )

        # 6. 编排
        t0 = time.perf_counter()
        answer, comp_usage = composer.compose(question, result.columns, result.rows, route, self.llm)
        usage.add(comp_usage)
        trace.append(self._step("compose", "生成结论", "done", answer[:80] + ("…" if len(answer) > 80 else ""), t0))

        return self._respond(
            question, trace, started, usage, status="ok",
            answer=answer, route=route, hits=hits, gen=gen, sql=sql, checks=checks,
            result=result,
        )

    # ------------------------------------------------------------------ 组装
    def _respond(self, question, trace, started, usage, *, status, answer, route, hits, gen,
                 sql="", checks=None, result=None) -> dict[str, Any]:
        total_ms = (time.perf_counter() - started) * 1000
        payload: dict[str, Any] = {
            "status": status,
            "question": question,
            "answer": answer,
            "sql": sql,
            "columns": result.columns if result and result.ok else [],
            "rows": result.rows if result and result.ok else [],
            "row_count": len(result.rows) if result and result.ok else 0,
            "truncated": bool(result.truncated) if result and result.ok else False,
            "evidence": [
                {"id": h.card.id, "kind": h.card.kind, "title": h.card.title, "score": round(h.score, 3)}
                for h in hits
            ],
            "checks": checks or [],
            "route": {"level": route.level, "label": route.label, "score": route.score, "reasons": route.reasons},
            "assumptions": gen.assumptions,
            "trace": trace,
            "timings": {"total_ms": round(total_ms), "sql_ms": round(result.ms) if result and result.ok else 0},
            "usage": usage.to_dict(),
            "mode": gen.mode,
            "as_of": config.AS_OF.isoformat(),
            "model": config.LLM_MODEL if self.llm.available else "offline-template",
        }
        return payload

    def _empty(self, message: str) -> dict[str, Any]:
        return {"status": "clarification", "question": "", "answer": message, "sql": "", "columns": [], "rows": [],
                "row_count": 0, "truncated": False, "evidence": [], "checks": [],
                "route": {"level": "simple", "label": "简单", "score": 0, "reasons": []}, "assumptions": [],
                "trace": [], "timings": {"total_ms": 0, "sql_ms": 0}, "usage": {"input_tokens": 0, "output_tokens": 0},
                "mode": "mock", "as_of": config.AS_OF.isoformat(), "model": "offline-template"}

    def _step(self, key: str, title: str, status: str, detail: str, t0: float, extra: dict | None = None) -> dict:
        step = {"key": key, "title": title, "status": status, "detail": detail,
                "ms": round((time.perf_counter() - t0) * 1000)}
        if extra:
            step.update(extra)
        if self._emit:
            self._emit(step)
        return step

    @staticmethod
    def _brief(sql: str) -> str:
        flat = " ".join(sql.split())
        return flat[:120] + ("…" if len(flat) > 120 else "")

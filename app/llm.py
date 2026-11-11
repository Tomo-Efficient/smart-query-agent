"""LLM 客户端：OpenAI 兼容协议（DeepSeek / Qwen / GLM / OpenAI 均可换）。"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from app import config


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


class LLM:
    def __init__(self) -> None:
        self._client = None

    @property
    def available(self) -> bool:
        return config.llm_enabled()

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=config.LLM_API_KEY,
                base_url=config.LLM_BASE_URL,
                timeout=config.LLM_TIMEOUT,
                max_retries=1,
            )
        return self._client

    def chat(self, system: str, user: str, *, json_mode: bool = False, temperature: float = 0.0) -> tuple[str, Usage]:
        if not self.available:
            raise LLMError("未配置模型 Key（DEEPSEEK_API_KEY），当前为离线模式")
        started = time.perf_counter()
        kwargs = {
            "model": config.LLM_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._get_client().chat.completions.create(**kwargs)
        usage = Usage(
            input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
        )
        return resp.choices[0].message.content or "", usage

    def chat_json(self, system: str, user: str) -> tuple[dict, Usage]:
        text, usage = self.chat(system, user, json_mode=True)
        try:
            return json.loads(text), usage
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise LLMError(f"模型未返回 JSON：{text[:200]}") from None
            return json.loads(match.group(0)), usage

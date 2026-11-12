"""测试的公共约束：默认离线。

`.env` 里配了真实 Key 时，单测也不能打真模型——否则断言会随模型输出漂移，
而且跑一次测试就烧钱。需要在线路径的用例自己在 fixture 里 monkeypatch Key。
"""

from __future__ import annotations

import pytest

from app import config


@pytest.fixture(autouse=True)
def offline_by_default(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")

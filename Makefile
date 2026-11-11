PY ?= .venv/bin/python
PORT ?= 8000

.PHONY: help setup data serve eval eval-quick ablate test clean docker

help:
	@echo "make setup      创建虚拟环境并安装依赖"
	@echo "make data       重建合成数仓（约 2 秒）"
	@echo "make serve      启动 Web 控制台 http://127.0.0.1:$(PORT)"
	@echo "make eval       全量评测（164 题）"
	@echo "make eval-quick 冒烟评测（20 题）"
	@echo "make ablate     消融实验（4 组配置）"
	@echo "make test       运行测试"

setup:
	python3 -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -r requirements.txt

data:
	$(PY) data/generate.py

serve:
	$(PY) -m uvicorn app.api:app --host 127.0.0.1 --port $(PORT) --reload

eval:
	$(PY) eval/build_questions.py
	$(PY) eval/run_eval.py

eval-quick:
	$(PY) eval/run_eval.py --limit 20

ablate:
	$(PY) eval/ablate.py

test:
	$(PY) -m pytest tests -q

clean:
	rm -rf .pytest_cache **/__pycache__ eval/results/report-*.json eval/results/report-*.md

docker:
	docker build -t smart-query-agent .
	docker run --rm -p $(PORT):8000 --env-file .env smart-query-agent

.PHONY: install check test record qc health catalog build map monitor serve

install:
	uv sync --locked

check:
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .
	uv run --frozen mypy
	uv run --frozen pytest

test:
	uv run --frozen pytest

record:
	uv run --frozen xasset record --days 7

qc:
	uv run --frozen xasset qc

health:
	uv run --frozen xasset health --sessions 5

catalog:
	uv run --frozen xasset catalog

build:
	uv build --no-sources

map:
	uv run --frozen xasset-app map config/map.yaml

monitor:
	uv run --frozen xasset-app monitor --config config/monitor.yaml

serve:
	uv run --frozen xasset-app serve --host 127.0.0.1 --port 8000

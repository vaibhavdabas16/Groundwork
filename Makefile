# Convenience wrappers. Every target is a plain `uv run ...` command, so the same steps work on
# Windows without make (see README "Commands").
.PHONY: install data acquire build quality validate serve health lint typecheck test test-unit \
        test-integration test-security test-mcp test-agent check clean gold-freeze gold-status \n        eval-smoke eval-full eval-gate

install:
	uv sync --frozen

acquire:
	uv run groundwork acquire

build:
	uv run groundwork build

quality:
	uv run groundwork quality

data: acquire build quality

validate:
	uv run groundwork validate-context

serve:
	uv run groundwork serve

health:
	uv run groundwork health

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run mypy

test-unit:
	uv run pytest tests/unit -q

test-integration:
	uv run pytest tests/integration -q

test-security:
	uv run pytest tests/security -q

test-mcp:
	uv run pytest tests/mcp -q

test-agent:
	uv run pytest tests/agent -q

test:
	uv run pytest -q

gold-freeze:
	uv run groundwork eval gold freeze

gold-status:
	uv run groundwork eval gold status

# Live targets need ANTHROPIC_API_KEY; without it they exit 3 (BLOCKED).
eval-smoke:
	uv run groundwork eval run --mode full_context --split dev --repetitions 1 --budget-usd 5 --kind smoke

eval-full:
	uv run groundwork eval run --all --split heldout --repetitions 3 --concurrency 3 --budget-usd 250 --kind experiment

eval-gate:
	uv run groundwork eval gate --run-id $(RUN_ID)

check: lint typecheck validate test

clean:
	rm -rf data/warehouse data/ops .pytest_cache .mypy_cache .ruff_cache

.DEFAULT_GOAL := help

.PHONY: help install dev test lint format security audit serve init doctor docker-up docker-down clean

help: ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## install synapto
	uv sync

dev: ## install with dev extras (tests, linting)
	uv sync --extra dev

TEST_PG_DSN ?= postgresql://synapto:synapto@localhost:5433/synapto_test

test: ## run tests that need no database (PostgreSQL tests are skipped)
	uv run pytest tests/ -v

test-all: ## run the full suite against the disposable synapto_test database
	SYNAPTO_TEST_PG_DSN=$(TEST_PG_DSN) SYNAPTO_REQUIRE_TEST_PG=1 uv run pytest tests/ -v

test-db: ## create the disposable synapto_test database used by test-all
	docker compose exec -T postgres psql -U synapto -d postgres -c "CREATE DATABASE synapto_test;" || true
	docker compose exec -T postgres psql -U synapto -d synapto_test -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"

lint: ## run ruff linter
	uv run ruff check src/ tests/

format: ## format code with ruff
	uv run ruff format src/ tests/

security: ## run bandit security scan
	uv run bandit -r src/synapto/ -c pyproject.toml

audit: ## audit dependencies for known vulnerabilities
	uv run pip-audit

serve: ## start the mcp server (stdio)
	uv run synapto serve

init: ## initialize database and config
	uv run synapto init

doctor: ## check system health
	uv run synapto doctor

docker-up: ## start all services with docker compose
	docker compose up -d

docker-down: ## stop all docker compose services
	docker compose down

clean: ## remove build artifacts and caches
	rm -rf dist/ build/ *.egg-info/ .pytest_cache/ .ruff_cache/ htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

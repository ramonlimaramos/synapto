.DEFAULT_GOAL := help

.PHONY: help install dev test test-all test-db lint format security audit serve init doctor docker-up docker-down clean

help: ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## install synapto
	uv sync

dev: ## install with dev extras (tests, linting)
	uv sync --extra dev

# Ports match docker-compose.yml, which publishes PostgreSQL on 5433 and Redis
# on 6380 to avoid colliding with a local install. Override per invocation:
#   TEST_PG_DSN=... TEST_REDIS_URL=... make test-all
TEST_PG_DSN ?= postgresql://synapto:synapto@localhost:5433/synapto_test
TEST_REDIS_URL ?= redis://localhost:6380/1

test: ## run tests without PostgreSQL (still needs Redis; see test-all for the full suite)
	SYNAPTO_TEST_PG_DSN= SYNAPTO_REQUIRE_TEST_PG= SYNAPTO_REDIS_URL=$(TEST_REDIS_URL) uv run pytest tests/ -v

test-all: ## run the full suite against the disposable synapto_test database
	SYNAPTO_TEST_PG_DSN=$(TEST_PG_DSN) SYNAPTO_REQUIRE_TEST_PG=1 SYNAPTO_REDIS_URL=$(TEST_REDIS_URL) uv run pytest tests/ -v

test-db: ## create the disposable synapto_test database used by test-all
	@if [ -z "$$(docker compose exec -T postgres psql -U synapto -d postgres -tAc \
		"SELECT 1 FROM pg_database WHERE datname = 'synapto_test'")" ]; then \
		docker compose exec -T postgres psql -U synapto -d postgres \
			-c "CREATE DATABASE synapto_test"; \
	fi
	docker compose exec -T postgres psql -U synapto -d synapto_test \
		-c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"

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

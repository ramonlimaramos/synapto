.DEFAULT_GOAL := help

.PHONY: help install dev test lint format security audit serve init doctor docker-up docker-down clean

help: ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## install synapto
	uv sync

dev: ## install with dev extras (tests, linting)
	uv sync --extra dev

test: ## run tests with pytest
	uv run pytest tests/ -v

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

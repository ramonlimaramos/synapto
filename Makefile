.DEFAULT_GOAL := help

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help install dev test lint format serve init doctor docker-up docker-down clean

help: ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## create venv and install synapto
	python3 -m venv $(VENV)
	$(PIP) install -e .

dev: ## install with dev extras (tests, linting)
	python3 -m venv $(VENV)
	$(PIP) install -e ".[dev]"

test: ## run tests with pytest
	$(PYTHON) -m pytest tests/ -v

lint: ## run ruff linter
	$(PYTHON) -m ruff check src/ tests/

format: ## format code with ruff
	$(PYTHON) -m ruff format src/ tests/

serve: ## start the mcp server (stdio)
	$(VENV)/bin/synapto serve

init: ## initialize database and config
	$(VENV)/bin/synapto init

doctor: ## check system health
	$(VENV)/bin/synapto doctor

docker-up: ## start all services with docker compose
	docker compose up -d

docker-down: ## stop all docker compose services
	docker compose down

clean: ## remove build artifacts and caches
	rm -rf dist/ build/ *.egg-info/ .pytest_cache/ .ruff_cache/ htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

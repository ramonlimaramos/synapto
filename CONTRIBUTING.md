# Contributing to Synapto

Thanks for your interest in contributing to Synapto! This guide covers everything you need to get started.

## Prerequisites

- Python 3.11+
- PostgreSQL 14+ with [pgvector](https://github.com/pgvector/pgvector)
- Redis 7+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

Or just use Docker:

```bash
make docker-up    # starts postgres + redis + synapto
```

## Development Setup

```bash
git clone https://github.com/ramonlimaramos/synapto.git
cd synapto

# install with dev dependencies
uv sync --extra dev
# or: pip install -e ".[dev]"

# initialize the database
uv run synapto init
```

## Running Tests

```bash
# full suite
uv run pytest tests/ -v

# with coverage
uv run pytest tests/ -v --cov=synapto --cov-report=term-missing

# specific file
uv run pytest tests/unit/test_hrr.py -v
```

Tests require a running PostgreSQL (with pgvector) and Redis instance. Connection strings are read from environment variables:

| Variable | Default |
|----------|---------|
| `SYNAPTO_PG_DSN` | `postgresql://localhost/synapto` |
| `SYNAPTO_REDIS_URL` | `redis://localhost:6379/1` |

## Linting

```bash
uv run ruff check src/ tests/       # check code style
uv run ruff format --check src/ tests/  # check formatting
```

We use [ruff](https://docs.astral.sh/ruff/) for linting and formatting:

- Line length: 120 characters
- Target: Python 3.11+
- Rules: E, F, I, N, W, UP

## Code Style

- No ORM — raw SQL only (psycopg3)
- Type hints on all public functions
- Tests mirror the `src/` directory structure under `tests/unit/`

## Commit Format

All commit messages must be **lowercase** with a scoped prefix:

```
feat(synapto): add new embedding provider
fix(synapto): handle empty query in hybrid search
docs(synapto): update quickstart guide
test(synapto): add graph traversal edge cases
chore(synapto): bump dependency versions
```

## Submitting Pull Requests

1. Fork the repository and create a branch from `main`
2. Make your changes, including tests for new functionality
3. Run `ruff check` and `pytest` to verify everything passes
4. Commit with the format described above
5. Open a PR against `main` with a clear description of what and why

### PR Requirements

- All CI checks must pass (lint + tests on Python 3.11/3.12/3.13)
- At least one maintainer approval is required
- Keep PRs focused — one feature or fix per PR

### PR Checklist

- [ ] Tests pass (`uv run pytest tests/`)
- [ ] Lint passes (`uv run ruff check src/ tests/`)
- [ ] New features include tests
- [ ] Commit messages follow the format above
- [ ] No new dependencies added without discussion

## Releases

Releases are published to PyPI and are **admin-only**. They are triggered manually via the GitHub Actions release workflow (`workflow_dispatch`). Contributors do not need to worry about versioning or publishing — maintainers handle this.

## Questions?

Open an [issue](https://github.com/ramonlimaramos/synapto/issues) or start a discussion on the repository.

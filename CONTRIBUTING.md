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

> **The PostgreSQL-backed tests are destructive.** They roll migrations down — dropping and recreating columns — and truncate tables. They refuse to run against any database whose name does not end in `_test`, and they never read `SYNAPTO_PG_DSN`. Never point them at a database whose contents you want to keep.

### One-time setup

```bash
make docker-up                      # starts PostgreSQL (port 5433) and Redis
make test-db                        # creates the disposable synapto_test database
```

Or, against a PostgreSQL you already run:

```bash
createdb synapto_test
psql -d synapto_test -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
```

### The two commands

```bash
make test           # partial: skips every PostgreSQL test, needs no database
make test-all       # full suite: requires SYNAPTO_TEST_PG_DSN
```

`make test` is safe anywhere and runs everything that does not need PostgreSQL — but it is **not** the full suite, and it will report success while the database tests are skipped. Use `make test-all` before opening a PR.

Directly with pytest:

```bash
uv run pytest tests/ -v                                                  # partial, DB tests skipped
SYNAPTO_TEST_PG_DSN=postgresql://localhost/synapto_test uv run pytest -v # full
```

### Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `SYNAPTO_TEST_PG_DSN` | **The only** DSN the tests will use. Must name a `*_test` database. | unset → PostgreSQL tests skip |
| `SYNAPTO_REQUIRE_TEST_PG` | Set to `1` to turn that skip into a failure. CI sets it so a misconfigured job cannot pass with the database suite silently skipped. | unset |
| `SYNAPTO_REDIS_URL` | Redis used by the cache fixture (scoped to the `synapto_test:` prefix). | `redis://localhost:6379/1` |

`SYNAPTO_PG_DSN` is the **runtime** DSN for a real Synapto install. The test suite ignores it by design — reading it is what allowed a destructive run against real data.

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

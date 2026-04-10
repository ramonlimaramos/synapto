# Contributing to Synapto

Thanks for your interest in contributing to Synapto! This guide covers everything you need to get started.

## Development Setup

```bash
git clone https://github.com/ramonlimaramos/synapto.git
cd synapto
make dev          # creates venv + installs with dev extras
make init         # initializes the database
make test         # runs the test suite
```

Or manually:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
synapto init
pytest
```

### Prerequisites

- Python 3.11+
- PostgreSQL 14+ with [pgvector](https://github.com/pgvector/pgvector)
- Redis 7+

Or just use Docker:

```bash
make docker-up    # starts postgres + redis + synapto
```

## Running Tests

```bash
make test         # run all tests
make lint         # check code style
make format       # auto-format code
```

Tests require a running PostgreSQL (with pgvector) and Redis instance. The test suite uses a dedicated Redis database (db 1) to avoid conflicts.

## Code Style

We use [ruff](https://docs.astral.sh/ruff/) for linting and formatting:

- Line length: 120 characters
- Target: Python 3.11+
- Rules: E, F, I, N, W, UP

Run `make lint` before submitting a PR. CI will reject PRs that fail lint checks.

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
3. Run `make lint` and `make test` to verify everything passes
4. Commit with the format described above
5. Open a PR against `main` with a clear description of what and why

### PR Checklist

- [ ] Tests pass (`make test`)
- [ ] Lint passes (`make lint`)
- [ ] New features include tests
- [ ] Commit messages follow the format above
- [ ] No new dependencies added without discussion

## Releases

Releases are published to PyPI and are **admin-only**. They are triggered manually via the GitHub Actions release workflow (`workflow_dispatch`). Contributors do not need to worry about versioning or publishing — maintainers handle this.

The release process:
1. An admin triggers the release workflow, selecting a version bump type (patch/minor/major)
2. The workflow bumps the version, builds the package, publishes to PyPI, and creates a GitHub Release
3. Only users with admin permission on the repository can trigger this workflow

## Questions?

Open an issue or start a discussion on the repository.

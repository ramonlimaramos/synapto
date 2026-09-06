# Releasing

Releases are admin-only and start by hand. The workflow publishes the version
`main` already declares; it never bumps anything. The version is a reviewed
decision that lands through a pull request, and the pipeline only ships what
that pull request agreed on.

## 1. Land the version

One pull request, no other changes:

- `pyproject.toml` — `[project] version`, the declaration of record
- `src/synapto/__init__.py` — `__version__`
- `uv.lock` — regenerate with `uv lock` so the `synapto` entry repeats the version
- `CHANGELOG.md` — rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD` and open a fresh `[Unreleased]`

`scripts/assert_release_contract.py` refuses to ship when the first three
disagree, so a forgotten `uv.lock` costs a few seconds instead of a release.

## 2. Preflight

After that pull request merges, from an up-to-date `main`:

```bash
uv run python scripts/preflight_release.py
```

Every line must read `ok`. The checks and what each one caught the day it was
written:

| check | asserts | history |
|---|---|---|
| triggers | `workflow_dispatch` only, no `bump_type`-style input | an in-CI bump wrote files the contract then rejected |
| pinned settings | no `EXPECTED_REF` / `EXPECTED_VERSION`, concurrency group follows the ref | a back-merge left `main` with the `release/0.5` workflow |
| job graph | `prepare → build → tag → publish → github_release`, contract asserted before the build | — |
| environment gate | `environment: release` on `tag` and `publish` only | — |
| permissions | read by default; each job holds exactly what it needs | — |
| trusted publishing | `pypa/gh-action-pypi-publish` with `id-token: write`, no password | — |
| release environment | exists, has a required reviewer, lets the default branch deploy | the deployment branch policy named only a deleted branch; `tag` failed in 2 s with zero steps |
| release contract | the three version declarations agree | — |

The environment check needs an authenticated `gh`; without one it is skipped
with a notice, not failed. The same script runs in CI as the `release
preflight` workflow on every pull request and push to `main`, with the
repository token, so a drift in the environment settings turns the next pull
request red rather than release day.

A `FAIL` line names the setting and where to change it. When the workflow is
changed on purpose — a job added, a permission widened — the tables in
`scripts/preflight_release.py` change in the same pull request.

## 3. Dispatch

GitHub → Actions → `release` → *Run workflow* from `main`. The jobs, in order:

1. **prepare** — admin check, then the release contract: versions agree and
   `vX.Y.Z` does not already point at other code.
2. **build** — wheel and sdist, each verified to bundle the migrations.
3. **tag** — behind the `release` environment; pushes `vX.Y.Z`. Git first
   because a tag is recoverable and a PyPI upload is not.
4. **publish** — behind the `release` environment; Trusted Publishing to PyPI.
5. **github_release** — the GitHub release with the artifacts attached;
   re-runnable on its own without republishing.

Approve the environment gate when it asks. A failure in `prepare` or `build`
has published nothing; fix and dispatch again. A failure after `tag` leaves the
tag in place, which is fine: a re-run accepts a tag that already points at the
same commit.

## 4. Verify

```bash
pip index versions synapto          # PyPI lists X.Y.Z
gh release view vX.Y.Z              # the GitHub release exists and is not a draft
uvx --refresh synapto --version     # a fresh environment resolves the new version
```

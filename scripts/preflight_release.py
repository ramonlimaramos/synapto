#!/usr/bin/env python3
"""Preflight the release path without publishing anything.

The release workflow runs exactly once per version, in production, with a
human waiting, and most of what can break it is configuration that ``pytest``
never executes. Three such failures happened on one day during 0.6.0 / 0.7.0:

1. a back-merge replaced ``release.yml`` on ``main`` with a maintenance-line
   variant pinned to ``refs/heads/release/0.5`` and a version constant, so
   every dispatch failed its own assert before building;
2. an in-workflow ``bump_type`` input wrote files that the release contract
   then rejected — the path had never executed before release day;
3. the ``release`` environment's deployment branch policy named only a deleted
   branch, so the ``tag`` job failed in two seconds with zero steps.

This script reproduces each of those locally and in CI, before a dispatch:

*Workflow shape.* ``release.yml`` is parsed and compared with the contract the
pipeline was hardened into: dispatch-only with no bump input, nothing pinned to
a ref or a version, the job graph ``prepare → build → tag → publish →
github_release`` with the contract asserted before anything is built,
``environment: release`` on exactly the two jobs that mutate the outside world,
the least-privilege permission table below, and Trusted Publishing (OIDC token,
no password). Every table here is the intended state; a PR that changes the
workflow changes the table in the same diff, and the CI job says so.

*Environment.* Through ``gh api`` — the settings live outside the repository —
the ``release`` environment must exist, hold at least one required reviewer,
and let the default branch deploy: either the policy is "protected branches"
and the default branch is protected, or the custom policy list matches it.
Without an authenticated ``gh`` the check is skipped with a notice rather than
failed, so a contributor without credentials still gets the workflow verdicts.

*Contract dry run.* ``assert_release_contract.assert_versions_agree`` runs over
the working tree. The tag-availability half is deliberately left to the
workflow's ``prepare`` job: between releases the current version's tag always
exists, so running it here would fail every pull request that does not bump.

PyYAML reads the bare workflow key ``on`` as the boolean ``True`` (YAML 1.1),
which :func:`load_workflow` normalises back to the string.

Exit codes follow the contract script: 0 when every check passed or was
skipped with a notice, 1 when any check failed, 2 for a usage error.

Usage:
    python scripts/preflight_release.py [repo_root] [--skip-github]
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

WORKFLOW = Path(".github") / "workflows" / "release.yml"
ENVIRONMENT = "release"
CONTRACT_SCRIPT = "assert_release_contract.py"
PUBLISH_ACTION = "pypa/gh-action-pypi-publish"
PINNED_SETTINGS = ("EXPECTED_REF", "EXPECTED_VERSION")
TOKEN_INPUTS = ("password", "user")

JOB_GRAPH: dict[str, tuple[str, ...]] = {
    "prepare": (),
    "build": ("prepare",),
    "tag": ("prepare", "build"),
    "publish": ("prepare", "build", "tag"),
    "github_release": ("prepare", "publish"),
}
GATED_JOBS = frozenset({"tag", "publish"})
DEFAULT_PERMISSIONS = {"contents": "read"}
JOB_PERMISSIONS: dict[str, dict[str, str]] = {
    "prepare": {"contents": "read"},
    "build": {"contents": "read"},
    "tag": {"contents": "write"},
    "publish": {"contents": "read", "id-token": "write"},
    "github_release": {"contents": "write"},
}

ENVIRONMENT_SETTINGS = f"Settings → Environments → {ENVIRONMENT}"

_RELEASE_LINE = re.compile(r"release/|v?\d+\.\d+")

ApiCall = Callable[[str], dict]


class PreflightError(RuntimeError):
    """The workflow file cannot be checked at all."""


class ApiError(RuntimeError):
    """A ``gh api`` call failed; ``not_found`` separates a missing resource from everything else."""

    def __init__(self, endpoint: str, detail: str, *, not_found: bool = False) -> None:
        super().__init__(f"gh api {endpoint} failed: {detail}")
        self.not_found = not_found


@dataclass(frozen=True)
class Verdict:
    """One check's outcome: passed with ``note``, failed with ``problems``, or ``skipped`` for a reason."""

    check: str
    problems: tuple[str, ...] = ()
    skipped: str | None = None
    note: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.problems)


def load_workflow(path: Path) -> dict:
    """Parse the release workflow, normalising PyYAML's ``on`` → ``True`` key."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreflightError(f"cannot read {path}: {exc}; the release workflow must live at {WORKFLOW}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PreflightError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PreflightError(f"{path} must hold a mapping at the top level; found {type(data).__name__}")
    if True in data:
        data["on"] = data.pop(True)
    return data


def _jobs(workflow: dict) -> dict[str, dict]:
    jobs = workflow.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def _steps(job: dict) -> list[dict]:
    steps = job.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def _as_names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def check_triggers(workflow: dict) -> Verdict:
    """The workflow is dispatched by hand and never bumps the version itself."""
    problems: list[str] = []
    on = workflow.get("on")
    triggers = dict(on) if isinstance(on, dict) else {name: None for name in _as_names(on)}

    if "workflow_dispatch" not in triggers:
        problems.append("the workflow is not triggered by workflow_dispatch; releases are started by hand")
    extra = sorted(name for name in triggers if name != "workflow_dispatch")
    if extra:
        problems.append(f"the workflow is also triggered by {', '.join(extra)}; a release must never start on its own")

    dispatch = triggers.get("workflow_dispatch") or {}
    inputs = dispatch.get("inputs") if isinstance(dispatch, dict) else None
    bump_inputs = sorted(name for name in (inputs or {}) if "bump" in name.lower())
    if bump_inputs:
        problems.append(
            f"workflow_dispatch declares input(s) {', '.join(bump_inputs)}: the workflow must not bump the version, "
            "it publishes the one main already declares; remove the input and the step that uses it, "
            "and land the version through a pull request"
        )
    return Verdict("triggers", tuple(problems), note="workflow_dispatch only, no bump input")


def _env_mappings(workflow: dict) -> Iterator[tuple[str, dict]]:
    if isinstance(workflow.get("env"), dict):
        yield "workflow", workflow["env"]
    for name, job in _jobs(workflow).items():
        if isinstance(job.get("env"), dict):
            yield f"job {name}", job["env"]
        for step in _steps(job):
            if isinstance(step.get("env"), dict):
                yield f"job {name}, step {step.get('name', step.get('uses', '?'))}", step["env"]


def check_nothing_is_pinned(workflow: dict) -> Verdict:
    """No ref, version constant or release-line concurrency group survives a back-merge into main."""
    problems: list[str] = []
    for where, env in _env_mappings(workflow):
        for setting in PINNED_SETTINGS:
            if setting in env:
                problems.append(
                    f"{where} sets {setting}={env[setting]!r}: this is the maintenance-line workflow; "
                    "restore the generic release.yml from main and drop the pinned value"
                )
    concurrency = workflow.get("concurrency")
    group = str(concurrency.get("group", "")) if isinstance(concurrency, dict) else str(concurrency or "")
    if _RELEASE_LINE.search(group):
        problems.append(
            f"concurrency group {group!r} is tied to a release line; use a ref-derived group such as "
            "release-${{ github.ref }}"
        )
    return Verdict("pinned settings", tuple(problems), note="no EXPECTED_REF / EXPECTED_VERSION, group follows the ref")


def check_job_graph(workflow: dict) -> Verdict:
    """The five jobs exist, depend on each other in order, and the contract runs before the build."""
    problems: list[str] = []
    jobs = _jobs(workflow)

    missing = [name for name in JOB_GRAPH if name not in jobs]
    if missing:
        problems.append(f"missing job(s) {', '.join(missing)}; the pipeline is {' → '.join(JOB_GRAPH)}")
    unexpected = [name for name in jobs if name not in JOB_GRAPH]
    if unexpected:
        problems.append(
            f"unexpected job(s) {', '.join(unexpected)}; add them to JOB_GRAPH in {Path(__file__).name} "
            "in the same pull request if they belong in the release path"
        )

    for name, required in JOB_GRAPH.items():
        if name not in jobs:
            continue
        needs = set(_as_names(jobs[name].get("needs")))
        lacking = [dep for dep in required if dep not in needs]
        if lacking:
            problems.append(f"job {name} must declare needs: {', '.join(required)}; it lacks {', '.join(lacking)}")

    prepare = jobs.get("prepare")
    if isinstance(prepare, dict) and not any(CONTRACT_SCRIPT in str(step.get("run", "")) for step in _steps(prepare)):
        problems.append(
            f"job prepare does not run scripts/{CONTRACT_SCRIPT}; the contract must be asserted before the build"
        )

    return Verdict("job graph", tuple(problems), note=" → ".join(JOB_GRAPH))


def _environment_name(job: dict) -> str | None:
    declared = job.get("environment")
    if isinstance(declared, dict):
        return str(declared.get("name")) if declared.get("name") is not None else None
    return str(declared) if declared is not None else None


def check_environment_gate(workflow: dict) -> Verdict:
    """Exactly the jobs that mutate the outside world sit behind the protected environment."""
    problems: list[str] = []
    for name, job in _jobs(workflow).items():
        if not isinstance(job, dict):
            continue
        declared = _environment_name(job)
        if name in GATED_JOBS and declared != ENVIRONMENT:
            problems.append(
                f"job {name} must declare environment: {ENVIRONMENT} (found {declared!r}); "
                "without it the required reviewer never sees the release"
            )
        if name not in GATED_JOBS and declared is not None:
            problems.append(
                f"job {name} declares environment: {declared}; only {', '.join(sorted(GATED_JOBS))} are gated"
            )
    return Verdict("environment gate", tuple(problems), note=f"{', '.join(sorted(GATED_JOBS))} → {ENVIRONMENT}")


def check_permissions(workflow: dict) -> Verdict:
    """Read by default; each job holds exactly the permissions in JOB_PERMISSIONS."""
    problems: list[str] = []
    if workflow.get("permissions") != DEFAULT_PERMISSIONS:
        problems.append(
            f"top-level permissions must be {DEFAULT_PERMISSIONS} (found {workflow.get('permissions')!r}); "
            "every job widens only what it needs"
        )
    jobs = _jobs(workflow)
    for name, expected in JOB_PERMISSIONS.items():
        job = jobs.get(name)
        if not isinstance(job, dict):
            continue
        found = job.get("permissions")
        if found != expected:
            problems.append(
                f"job {name} permissions must be exactly {expected} (found {found!r}); "
                f"change JOB_PERMISSIONS in {Path(__file__).name} in the same pull request if the job's needs changed"
            )
    return Verdict("permissions", tuple(problems), note="least privilege per job")


def check_trusted_publishing(workflow: dict) -> Verdict:
    """PyPI is reached through Trusted Publishing: the publish action, the OIDC token, no password."""
    problems: list[str] = []
    publish = _jobs(workflow).get("publish")
    if not isinstance(publish, dict):
        return Verdict("trusted publishing", ("no publish job to inspect",))

    steps = [step for step in _steps(publish) if str(step.get("uses", "")).startswith(PUBLISH_ACTION)]
    if not steps:
        problems.append(f"job publish has no step using {PUBLISH_ACTION}; that action is the Trusted Publishing client")
    for step in steps:
        given = step.get("with") if isinstance(step.get("with"), dict) else {}
        secrets = [key for key in TOKEN_INPUTS if key in given]
        if secrets:
            problems.append(
                f"the {PUBLISH_ACTION} step passes {', '.join(secrets)}; Trusted Publishing needs no token — "
                "drop the input and keep id-token: write on the job"
            )
    if (publish.get("permissions") or {}).get("id-token") != "write":
        problems.append("job publish lacks id-token: write; Trusted Publishing cannot mint its OIDC token without it")
    return Verdict("trusted publishing", tuple(problems), note=f"{PUBLISH_ACTION} with the OIDC token")


WORKFLOW_CHECKS: tuple[Callable[[dict], Verdict], ...] = (
    check_triggers,
    check_nothing_is_pinned,
    check_job_graph,
    check_environment_gate,
    check_permissions,
    check_trusted_publishing,
)


def check_workflow(path: Path) -> list[Verdict]:
    """Every workflow-shape verdict, or a single failed verdict when the file cannot be parsed."""
    try:
        workflow = load_workflow(path)
    except PreflightError as exc:
        return [Verdict("workflow file", (str(exc),))]
    return [check(workflow) for check in WORKFLOW_CHECKS]


def gh_skip_reason() -> str | None:
    """Why the environment check cannot run here, or ``None`` when ``gh`` is installed and authenticated."""
    if shutil.which("gh") is None:
        return "gh is not installed; install the GitHub CLI to verify the release environment"
    status = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if status.returncode != 0:
        return "gh is not authenticated; run `gh auth login` or set GH_TOKEN to verify the release environment"
    return None


def gh_api(endpoint: str) -> dict:
    """Call ``gh api`` for the current repository; ``{owner}`` and ``{repo}`` are expanded by ``gh``."""
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise ApiError(endpoint, detail, not_found="HTTP 404" in detail)
    return json.loads(result.stdout)


def _default_branch_may_deploy(api: ApiCall, environment: dict, default_branch: str) -> str | None:
    """Return the problem with the deployment branch policy, or ``None`` when the default branch is allowed."""
    policy = environment.get("deployment_branch_policy")
    if not isinstance(policy, dict):
        return None
    if policy.get("protected_branches"):
        branch = api(f"repos/{{owner}}/{{repo}}/branches/{default_branch}")
        if branch.get("protected"):
            return None
        return (
            f"environment '{ENVIRONMENT}' deploys only from protected branches but '{default_branch}' is not "
            f"protected; protect it under Settings → Branches or switch {ENVIRONMENT_SETTINGS} → Deployment branches "
            "to a custom list that names it"
        )
    if policy.get("custom_branch_policies"):
        listing = api(f"repos/{{owner}}/{{repo}}/environments/{ENVIRONMENT}/deployment-branch-policies")
        patterns = [
            str(rule.get("name"))
            for rule in listing.get("branch_policies", [])
            if isinstance(rule, dict) and rule.get("type", "branch") == "branch"
        ]
        if any(fnmatch.fnmatchcase(default_branch, pattern) for pattern in patterns):
            return None
        allowed = ", ".join(patterns) if patterns else "no branch"
        return (
            f"environment '{ENVIRONMENT}' allows deployments from {allowed} but not from '{default_branch}', "
            f"so the tag job fails before its first step; add '{default_branch}' under "
            f"{ENVIRONMENT_SETTINGS} → Deployment branches"
        )
    return None


def check_environment(api: ApiCall) -> Verdict:
    """The GitHub-side half of the release gate: the environment exists, is reviewed, and admits the default branch."""
    try:
        default_branch = str(api("repos/{owner}/{repo}").get("default_branch", "main"))
        try:
            environment = api(f"repos/{{owner}}/{{repo}}/environments/{ENVIRONMENT}")
        except ApiError as exc:
            if not exc.not_found:
                raise
            return Verdict(
                "release environment",
                (
                    f"environment '{ENVIRONMENT}' does not exist; create it under {ENVIRONMENT_SETTINGS} with a "
                    f"required reviewer and a deployment branch policy that includes '{default_branch}'",
                ),
            )

        problems: list[str] = []
        rules = [rule for rule in environment.get("protection_rules", []) if isinstance(rule, dict)]
        reviewed = any(rule.get("type") == "required_reviewers" and rule.get("reviewers") for rule in rules)
        if not reviewed:
            problems.append(
                f"environment '{ENVIRONMENT}' has no required reviewer, so tag and publish run unattended; "
                f"add one under {ENVIRONMENT_SETTINGS} → Required reviewers"
            )
        policy_problem = _default_branch_may_deploy(api, environment, default_branch)
        if policy_problem:
            problems.append(policy_problem)
    except ApiError as exc:
        return Verdict("release environment", (str(exc),))
    except (KeyError, TypeError, ValueError) as exc:
        return Verdict("release environment", (f"unexpected response from the GitHub API: {exc!r}",))

    return Verdict("release environment", tuple(problems), note=f"reviewed, '{default_branch}' may deploy")


def _load_contract():
    path = Path(__file__).with_name(CONTRACT_SCRIPT)
    spec = importlib.util.spec_from_file_location("assert_release_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_contract(root: Path) -> Verdict:
    """The version half of the release contract, over the working tree."""
    contract = _load_contract()
    try:
        version = contract.assert_versions_agree(root)
    except contract.ReleaseContractError as exc:
        return Verdict("release contract", (f"{exc}; land the bump through a pull request before dispatching",))
    return Verdict("release contract", note=f"{version} in pyproject.toml, __init__.py and uv.lock")


def run_preflight(root: Path, *, api: ApiCall | None, skip_reason: str | None) -> list[Verdict]:
    """All verdicts in report order; ``api`` is ``None`` only when the environment check is skipped."""
    verdicts = check_workflow(root / WORKFLOW)
    if skip_reason is not None or api is None:
        verdicts.append(Verdict("release environment", skipped=skip_reason or "skipped on request"))
    else:
        verdicts.append(check_environment(api))
    verdicts.append(check_contract(root))
    return verdicts


def render(verdicts: Sequence[Verdict]) -> str:
    lines: list[str] = []
    for verdict in verdicts:
        if verdict.failed:
            lines.append(f"FAIL {verdict.check}")
            lines.extend(f"     - {problem}" for problem in verdict.problems)
        elif verdict.skipped is not None:
            lines.append(f"skip {verdict.check}: {verdict.skipped}")
        else:
            lines.append(f"ok   {verdict.check}: {verdict.note}")
    failures = sum(len(verdict.problems) for verdict in verdicts)
    lines.append(f"release preflight FAILED: {failures} problem(s)" if failures else "release preflight passed")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=Path(argv[0]).name, description="Preflight the release path without publishing."
    )
    parser.add_argument("repo_root", nargs="?", default=".", help="repository root (default: current directory)")
    parser.add_argument("--skip-github", action="store_true", help="do not query the GitHub API for the environment")
    try:
        options = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return 2 if exc.code else 0

    root = Path(options.repo_root)
    skip_reason = "skipped on request (--skip-github)" if options.skip_github else gh_skip_reason()
    verdicts = run_preflight(root, api=None if skip_reason else gh_api, skip_reason=skip_reason)
    print(render(verdicts))
    return 1 if any(verdict.failed for verdict in verdicts) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

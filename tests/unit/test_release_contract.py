"""Tests for the gate that decides whether a release may proceed.

The v0.5 back-merge left ``main`` carrying the maintenance workflow, pinned to
``refs/heads/release/0.5`` and the constant ``0.5.1``, so every dispatch from
``main`` failed its own assert before building anything. Restoring the generic
workflow moved that assert into a script precisely so the paths below could be
exercised: a release gate whose failure modes are only reachable by dispatching
a real release is a gate nobody can test.

Everything here runs without a database and without a network — the remote tag
lookup is injected.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

SHA = "1111111111111111111111111111111111111111"
OTHER_SHA = "2222222222222222222222222222222222222222"


def _load_gate(alias: str = "assert_release_contract"):
    path = REPO_ROOT / "scripts" / "assert_release_contract.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate():
    return _load_gate()


def _no_tag(_command):
    return ""


def _tag_at(sha):
    def runner(command):
        return f"{sha}\t{command[-1]}\n"

    return runner


def _write_repo(root: Path, *, pyproject="0.7.0", init="0.7.0", lock="0.7.0"):
    """Materialize the three files the gate reads, each independently settable."""
    (root / "src" / "synapto").mkdir(parents=True, exist_ok=True)
    if pyproject is not None:
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "synapto"\nversion = "{pyproject}"\n\n[tool.ruff]\ntarget-version = "py311"\n'
        )
    if init is not None:
        (root / "src" / "synapto" / "__init__.py").write_text(f'__version__ = "{init}"\n')
    if lock is not None:
        (root / "uv.lock").write_text(
            'version = 1\n\n[[package]]\nname = "click"\nversion = "8.3.3"\n\n'
            f'[[package]]\nname = "synapto"\nversion = "{lock}"\n'
        )
    return root


class TestVersionsMustAgree:
    """pyproject.toml is the declaration of record; the other two must repeat it."""

    def test_three_matching_files_yield_the_declared_version(self, gate, tmp_path):
        _write_repo(tmp_path)

        assert gate.assert_versions_agree(tmp_path) == "0.7.0"

    def test_a_drifted_init_is_rejected_and_named(self, gate, tmp_path):
        _write_repo(tmp_path, init="0.6.0")

        with pytest.raises(gate.ReleaseContractError, match="src/synapto/__init__.py"):
            gate.assert_versions_agree(tmp_path)

    def test_a_drifted_lock_is_rejected_and_named(self, gate, tmp_path):
        """The exact shape the removed in-workflow bump produced.

        It ran ``git add pyproject.toml src/synapto/__init__.py`` and never
        touched the lockfile, so every patch/minor/major bump built X, X, X-1.
        """
        _write_repo(tmp_path, lock="0.6.0")

        with pytest.raises(gate.ReleaseContractError, match="uv.lock"):
            gate.assert_versions_agree(tmp_path)

    def test_the_error_names_both_versions(self, gate, tmp_path):
        _write_repo(tmp_path, pyproject="0.7.0", lock="0.6.0")

        with pytest.raises(gate.ReleaseContractError, match="'0.6.0'.*'0.7.0'"):
            gate.assert_versions_agree(tmp_path)

    def test_a_lockfile_pinning_another_package_first_is_read_correctly(self, gate, tmp_path):
        """A positional read would have returned click's version as ours."""
        _write_repo(tmp_path)

        assert gate.read_lock_version(tmp_path) == "0.7.0"


class TestMissingDeclarationsFailClosed:
    def test_a_pyproject_without_a_version_is_rejected(self, gate, tmp_path):
        _write_repo(tmp_path, pyproject=None)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "synapto"\n')

        with pytest.raises(gate.ReleaseContractError, match="no project version"):
            gate.assert_versions_agree(tmp_path)

    def test_an_absent_pyproject_is_reported_rather_than_traced(self, gate, tmp_path):
        with pytest.raises(gate.ReleaseContractError, match="cannot read"):
            gate.read_pyproject_version(tmp_path)

    def test_malformed_toml_is_reported(self, gate, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project\nname =")

        with pytest.raises(gate.ReleaseContractError, match="not valid TOML"):
            gate.read_pyproject_version(tmp_path)

    def test_an_init_without_a_dunder_version_is_rejected(self, gate, tmp_path):
        _write_repo(tmp_path, init=None)
        (tmp_path / "src" / "synapto" / "__init__.py").write_text('"""no version here"""\n')

        with pytest.raises(gate.ReleaseContractError, match="no __version__"):
            gate.read_init_version(tmp_path)

    def test_a_lockfile_without_this_package_is_rejected(self, gate, tmp_path):
        _write_repo(tmp_path, lock=None)
        (tmp_path / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "click"\nversion = "8.3.3"\n')

        with pytest.raises(gate.ReleaseContractError, match="does not pin synapto"):
            gate.read_lock_version(tmp_path)


class TestTagAvailability:
    """Without a bump step the tag comes from the declared version, so a second
    dispatch at a new commit would collide only after publishing."""

    def test_an_unused_tag_is_free(self, gate):
        assert gate.assert_tag_is_free("0.7.0", SHA, _no_tag) == "v0.7.0"

    def test_a_tag_on_this_commit_is_a_rerun_and_is_allowed(self, gate):
        assert gate.assert_tag_is_free("0.7.0", SHA, _tag_at(SHA)) == "v0.7.0"

    def test_a_tag_on_another_commit_is_rejected(self, gate):
        with pytest.raises(gate.ReleaseContractError, match="already exists"):
            gate.assert_tag_is_free("0.7.0", SHA, _tag_at(OTHER_SHA))

    def test_the_rejection_names_the_offending_commit_and_the_remedy(self, gate):
        with pytest.raises(gate.ReleaseContractError, match=f"{OTHER_SHA}.*bump the version"):
            gate.assert_tag_is_free("0.7.0", SHA, _tag_at(OTHER_SHA))

    def test_the_queried_ref_is_fully_qualified(self, gate):
        seen = []

        def runner(command):
            seen.append(list(command))
            return ""

        gate.assert_tag_is_free("0.7.0", SHA, runner)

        assert seen == [["git", "ls-remote", "--tags", "origin", "refs/tags/v0.7.0"]]

    def test_an_unreachable_remote_fails_closed(self, gate):
        def runner(_command):
            raise gate.ReleaseContractError("git ls-remote failed: connection refused")

        with pytest.raises(gate.ReleaseContractError):
            gate.assert_tag_is_free("0.7.0", SHA, runner)


class TestEndToEndGate:
    def test_a_publishable_state_returns_the_version(self, gate, tmp_path):
        _write_repo(tmp_path)

        assert gate.assert_release_contract(tmp_path, SHA, _no_tag) == "0.7.0"

    def test_versions_are_checked_before_the_remote_is_queried(self, gate, tmp_path):
        """A drifted checkout must not depend on network reachability to fail."""
        _write_repo(tmp_path, lock="0.6.0")
        queried = []

        def runner(command):
            queried.append(command)
            return ""

        with pytest.raises(gate.ReleaseContractError):
            gate.assert_release_contract(tmp_path, SHA, runner)

        assert queried == []


class TestCommandLine:
    def test_a_publishable_state_exits_zero_and_emits_the_version(self, gate, tmp_path, monkeypatch):
        _write_repo(tmp_path)
        output = tmp_path / "github_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        monkeypatch.setenv("GITHUB_SHA", SHA)
        monkeypatch.setattr(gate, "_git_ls_remote", _no_tag)

        assert gate.main(["assert_release_contract.py", str(tmp_path)]) == 0
        assert output.read_text() == "version=0.7.0\n"

    def test_a_drifted_state_exits_one_without_a_traceback(self, gate, tmp_path, capsys, monkeypatch):
        _write_repo(tmp_path, lock="0.6.0")
        monkeypatch.setenv("GITHUB_SHA", SHA)

        assert gate.main(["assert_release_contract.py", str(tmp_path)]) == 1

        captured = capsys.readouterr()
        assert "FAILED" in captured.err
        assert "Traceback" not in captured.err

    def test_no_version_is_emitted_when_the_gate_fails(self, gate, tmp_path, monkeypatch):
        _write_repo(tmp_path, init="0.6.0")
        output = tmp_path / "github_output"
        output.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        monkeypatch.setenv("GITHUB_SHA", SHA)

        assert gate.main(["assert_release_contract.py", str(tmp_path)]) == 1
        assert output.read_text() == ""

    def test_too_many_arguments_are_reported(self, gate):
        assert gate.main(["assert_release_contract.py", "a", "b"]) == 2

    def test_the_command_line_uses_the_module_runner_not_a_frozen_default(self, gate, tmp_path, monkeypatch):
        """A def-time default kept the real ``git ls-remote`` bound under the patch.

        The tests then passed only while the remote had no tag for the declared
        version and broke the day it did — the check reached the network from a
        unit test. Whatever the remote holds, this must observe the patched runner.
        """
        _write_repo(tmp_path)
        monkeypatch.setenv("GITHUB_SHA", SHA)
        seen = []

        def recording_runner(command):
            seen.append(command)
            return ""

        monkeypatch.setattr(gate, "_git_ls_remote", recording_runner)

        assert gate.main(["assert_release_contract.py", str(tmp_path)]) == 0
        assert seen == [["git", "ls-remote", "--tags", "origin", "refs/tags/v0.7.0"]]

    def test_a_tag_on_another_commit_is_seen_through_the_module_runner(self, gate, tmp_path, monkeypatch):
        _write_repo(tmp_path)
        monkeypatch.setenv("GITHUB_SHA", SHA)
        monkeypatch.setattr(gate, "_git_ls_remote", _tag_at("2" * 40))

        assert gate.main(["assert_release_contract.py", str(tmp_path)]) == 1

    def test_a_missing_github_output_is_not_an_error(self, gate, tmp_path, monkeypatch):
        """The script is runnable locally, where no step output exists."""
        _write_repo(tmp_path)
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        monkeypatch.setenv("GITHUB_SHA", SHA)
        monkeypatch.setattr(gate, "_git_ls_remote", _no_tag)

        assert gate.main(["assert_release_contract.py", str(tmp_path)]) == 0


class TestThisRepositoryIsPublishable:
    """The regression that stops a release from being cut with drifted files.

    This runs against the working tree, not a fixture, so preparing a version
    in a pull request that forgets ``uv.lock`` fails in CI rather than during
    the dispatch.
    """

    def test_the_declared_versions_agree(self, gate):
        assert gate.assert_versions_agree(REPO_ROOT)


class TestReleaseWorkflowShape:
    """Guards on the workflow file itself.

    The back-merge silently replaced this file with a maintenance-line variant,
    and nothing failed until a release was attempted. These assertions are what
    that resolution would have tripped over.
    """

    @pytest.fixture
    def workflow(self):
        return WORKFLOW.read_text(encoding="utf-8")

    def test_no_ref_is_pinned(self, workflow):
        assert "EXPECTED_REF" not in workflow
        assert "refs/heads/release/" not in workflow

    def test_no_version_constant_is_asserted(self, workflow):
        assert "EXPECTED_VERSION" not in workflow

    def test_the_concurrency_group_is_not_tied_to_a_release_line(self, workflow):
        assert "group: release-${{ github.ref }}" in workflow
        assert "release-v0.5" not in workflow

    def test_the_admin_check_is_preserved(self, workflow):
        assert "actions-cool/check-user-permission@v2" in workflow
        assert "require: admin" in workflow

    def test_the_contract_runs_before_anything_is_built(self, workflow):
        assert workflow.index("assert_release_contract.py") < workflow.index("python -m build")

    def test_the_packaging_gate_still_runs_over_both_distributions(self, workflow):
        assert "scripts/verify_wheel.py dist/*.whl dist/*.tar.gz" in workflow

    def test_the_workflow_declares_no_bump_input(self, workflow):
        """The version arrives by pull request, with its CHANGELOG and lockfile."""
        assert "bump_type" not in workflow

    def test_publishing_is_gated_on_the_release_environment(self, workflow):
        assert workflow.count("environment: release") == 2

    def test_only_the_publish_job_holds_the_oidc_token(self, workflow):
        assert workflow.count("id-token: write") == 1

    def test_the_default_permission_is_read(self, workflow):
        assert workflow.index("permissions:\n  contents: read") < workflow.index("jobs:")

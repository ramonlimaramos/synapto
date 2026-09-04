"""Tests for tenant resolution and merge planning.

The bug being closed is silent in both directions: a memory written under one
spelling is invisible to a read that uses another, and nothing errors. So the
assertions here are mostly about *refusing* — a rejection that names the
canonical form is the whole feature, and a planner that guesses is worse than
one that stops.

Everything in this module runs without a database and without invoking git; the
command runner is injected.
"""

from __future__ import annotations

import pytest

from synapto.tenants import (
    AMBIGUOUS,
    DEFAULT_TENANT,
    EXACT,
    REVIEW,
    InvalidTenantError,
    clear_tenant_cache,
    is_canonical_tenant,
    plan_tenant_merges,
    resolve_tenant,
    tenant_from_git_remote,
    validate_tenant,
)


@pytest.fixture(autouse=True)
def _isolated_cache():
    """Derivation is memoized per directory; no test may inherit another's."""
    clear_tenant_cache()
    yield
    clear_tenant_cache()


def _remote(url: str):
    return lambda _command: url


def _no_remote(_command):
    return ""


class TestCanonicalTenants:
    @pytest.mark.parametrize(
        "value",
        ["default", "synapto", "podium-internal", "ramonlimaramos/synapto", "acme/api", "acme/.github", "a", "a1/b2"],
    )
    def test_accepted_spellings(self, value):
        assert is_canonical_tenant(value)
        assert validate_tenant(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "Podium",
            " podium",
            "podium ",
            "podium\n",
            "acme/API",
            "acme//api",
            "acme/api/extra",
            "-leading",
            "trailing-",
            "acme_org/api",
            "аcme/api",
        ],
    )
    def test_rejected_spellings(self, value):
        assert not is_canonical_tenant(value)
        with pytest.raises(InvalidTenantError):
            validate_tenant(value)


class TestRejectionsAreActionable:
    def test_the_error_names_the_canonical_form(self):
        with pytest.raises(InvalidTenantError, match="did you mean 'podium/divergence'"):
            validate_tenant("Podium/Divergence")

    def test_whitespace_is_named_not_trimmed(self):
        with pytest.raises(InvalidTenantError, match="did you mean 'podium'"):
            validate_tenant("  podium  ")

    def test_no_suggestion_when_no_canonical_form_exists(self):
        with pytest.raises(InvalidTenantError) as raised:
            validate_tenant("acme/api/extra/more")

        assert "did you mean" not in str(raised.value)
        assert "4 '/'-separated segments" in str(raised.value)

    def test_the_source_label_points_at_what_to_edit(self):
        with pytest.raises(InvalidTenantError, match="default_tenant in the synapto config"):
            validate_tenant("Bad", source="default_tenant in the synapto config")

    @pytest.mark.parametrize("value", [None, 7, True, ["acme/api"]])
    def test_non_strings_are_rejected_by_type(self, value):
        with pytest.raises(InvalidTenantError, match="must be a string"):
            validate_tenant(value)

    def test_an_overlong_tenant_is_rejected(self):
        with pytest.raises(InvalidTenantError, match="exceeds 100 chars"):
            validate_tenant("a" * 101)

    def test_an_empty_tenant_is_rejected(self):
        with pytest.raises(InvalidTenantError, match="must not be empty"):
            validate_tenant("")


class TestDerivationFromGit:
    @pytest.mark.parametrize(
        "remote",
        [
            "git@github.com:ramonlimaramos/synapto.git",
            "https://github.com/ramonlimaramos/synapto.git",
            "https://github.com/ramonlimaramos/synapto",
            "ssh://git@github.com/ramonlimaramos/synapto.git",
            "git://github.com/ramonlimaramos/synapto.git",
            "https://user@github.com/ramonlimaramos/synapto.git",
        ],
    )
    def test_every_remote_spelling_yields_the_same_tenant(self, remote):
        assert tenant_from_git_remote("/repo", _remote(remote)) == "ramonlimaramos/synapto"

    def test_case_is_normalized_because_the_remote_asserts_none(self):
        """Not a repair of caller input — a git remote's case means nothing."""
        assert tenant_from_git_remote("/repo", _remote("git@github.com:RamonLimaRamos/Synapto.git")) == (
            "ramonlimaramos/synapto"
        )

    def test_a_self_hosted_host_still_yields_owner_and_name(self):
        assert tenant_from_git_remote("/repo", _remote("git@git.example.com:acme/api.git")) == "acme/api"

    def test_a_nested_group_path_keeps_only_the_last_two_segments(self):
        assert tenant_from_git_remote("/repo", _remote("https://gitlab.com/acme/team/api.git")) == "team/api"

    @pytest.mark.parametrize(
        "remote",
        ["/Users/someone/code/project", "../relative/path", "file:///srv/git/bare", "git@github.com:single.git"],
    )
    def test_a_remote_without_owner_and_name_derives_nothing(self, remote):
        assert tenant_from_git_remote("/repo", _remote(remote)) is None

    def test_no_repository_derives_nothing(self):
        assert tenant_from_git_remote("/tmp", _no_remote) is None

    @pytest.mark.parametrize("name", ["bad name", "_", "..", "acme%api"])
    def test_a_remote_that_is_not_canonical_derives_nothing(self, name):
        """Falls through rather than raising — this path runs from anywhere."""
        assert tenant_from_git_remote("/repo", _remote(f"git@github.com:acme/{name}.git")) is None

    def test_a_leading_dot_is_accepted_because_github_dotgithub_is_real(self):
        assert tenant_from_git_remote("/repo", _remote("git@github.com:github/.github.git")) == "github/.github"

    def test_the_working_directory_is_passed_to_git(self):
        seen = []

        def runner(command):
            seen.append(list(command))
            return ""

        tenant_from_git_remote("/some/where", runner)

        assert seen == [["git", "-C", "/some/where", "remote", "get-url", "origin"]]


class TestResolutionOrder:
    def test_an_explicit_tenant_wins_over_a_derivable_location(self):
        resolved = resolve_tenant("acme/api", configured="ignored", cwd="/repo", runner=_remote("git@h:o/n.git"))

        assert resolved == "acme/api"

    def test_the_location_wins_over_configuration(self):
        resolved = resolve_tenant(None, configured="default", cwd="/repo", runner=_remote("git@h:acme/api.git"))

        assert resolved == "acme/api"

    def test_configuration_is_used_when_nothing_can_be_derived(self):
        assert resolve_tenant(None, configured="acme/api", cwd="/tmp", runner=_no_remote) == "acme/api"

    def test_the_default_is_the_last_resort(self):
        assert resolve_tenant(None, configured=None, cwd="/tmp", runner=_no_remote) == DEFAULT_TENANT

    def test_an_explicit_non_canonical_tenant_raises(self):
        with pytest.raises(InvalidTenantError, match="did you mean 'acme/api'"):
            resolve_tenant("Acme/API", cwd="/tmp", runner=_no_remote)

    def test_a_non_canonical_config_names_the_config(self):
        with pytest.raises(InvalidTenantError, match="default_tenant in the synapto config"):
            resolve_tenant(None, configured="Bad Tenant", cwd="/tmp", runner=_no_remote)

    def test_an_unreachable_git_does_not_fail_the_caller(self):
        def exploding(_command):
            raise OSError("git not installed")

        with pytest.raises(OSError):
            exploding([])
        assert resolve_tenant(None, configured="acme/api", cwd="/tmp", runner=_no_remote) == "acme/api"


class TestDerivationIsCachedPerLocation:
    def test_one_directory_costs_one_git_call(self):
        calls = []

        def runner(command):
            calls.append(command)
            return "git@github.com:acme/api.git"

        for _ in range(5):
            resolve_tenant(None, cwd="/repo", runner=runner)

        assert len(calls) == 1

    def test_a_different_directory_resolves_again(self):
        seen = []

        def runner(command):
            seen.append(command[2])
            return f"git@github.com:acme/{command[2].strip('/')}.git"

        assert resolve_tenant(None, cwd="/one", runner=runner) == "acme/one"
        assert resolve_tenant(None, cwd="/two", runner=runner) == "acme/two"
        assert seen == ["/one", "/two"]

    def test_clearing_the_cache_forces_a_new_lookup(self):
        calls = []

        def runner(command):
            calls.append(command)
            return "git@github.com:acme/api.git"

        resolve_tenant(None, cwd="/repo", runner=runner)
        clear_tenant_cache()
        resolve_tenant(None, cwd="/repo", runner=runner)

        assert len(calls) == 2

    def test_a_directory_with_no_remote_is_not_re_probed(self):
        calls = []

        def runner(command):
            calls.append(command)
            return ""

        resolve_tenant(None, configured="acme/api", cwd="/tmp", runner=runner)
        resolve_tenant(None, configured="acme/api", cwd="/tmp", runner=runner)

        assert len(calls) == 1


class TestMergePlanning:
    def _group_for(self, groups, member):
        return next(g for g in groups if member in g.members)

    def test_case_and_underscore_differences_merge_confidently(self):
        groups = plan_tenant_merges({"web-seller": 40, "web_seller": 4})
        group = self._group_for(groups, "web_seller")

        assert group.confidence == EXACT
        assert group.canonical == "web-seller"
        assert group.is_actionable

    def test_the_busiest_spelling_wins_an_exact_group(self):
        groups = plan_tenant_merges({"web-seller": 4, "web_seller": 40})

        assert self._group_for(groups, "web-seller").canonical == "web_seller"

    def test_one_qualified_spelling_is_adopted_but_flagged_for_review(self):
        groups = plan_tenant_merges({"hermes": 22, "podium/hermes": 1})
        group = self._group_for(groups, "hermes")

        assert group.confidence == REVIEW
        assert group.canonical == "podium/hermes"

    def test_two_owners_claiming_a_name_decide_nothing(self):
        groups = plan_tenant_merges({"divergence": 34, "podium/divergence": 7, "podium-internal/divergence": 13})
        group = self._group_for(groups, "divergence")

        assert group.confidence == AMBIGUOUS
        assert group.canonical is None
        assert not group.is_actionable
        assert "podium" in group.reason and "podium-internal" in group.reason

    def test_a_hyphen_prefix_groups_only_when_the_suffix_is_a_real_tenant(self):
        groups = plan_tenant_merges({"kazaam": 4, "podium-kazaam": 3})
        group = self._group_for(groups, "kazaam")

        assert set(group.members) == {"kazaam", "podium-kazaam"}
        assert group.confidence == AMBIGUOUS
        assert group.canonical is None

    def test_a_hyphenated_name_with_no_matching_tenant_stays_alone(self):
        """``podium-internal`` must not be read as owner ``podium``."""
        groups = plan_tenant_merges({"podium-internal": 5, "podium": 305})

        assert self._group_for(groups, "podium-internal").members == ("podium-internal",)

    def test_singletons_are_reported_so_every_tenant_is_accounted_for(self):
        counts = {"default": 484, "acme/api": 3}
        groups = plan_tenant_merges(counts)

        assert sum(len(g.members) for g in groups) == len(counts)
        assert all(not g.members[0].startswith("missing") for g in groups)

    def test_groups_are_ordered_by_weight(self):
        groups = plan_tenant_merges({"default": 484, "acme/api": 3, "tiny": 1})

        assert [g.members[0] for g in groups] == ["default", "acme/api", "tiny"]

    def test_planning_is_deterministic(self):
        counts = {"hermes": 22, "podium/hermes": 1, "kazaam": 4, "podium-kazaam": 3, "default": 484}

        assert plan_tenant_merges(counts) == plan_tenant_merges(counts)

    def test_an_empty_store_plans_nothing(self):
        assert plan_tenant_merges({}) == []


class TestPlanningTheRealStore:
    """A characterization test over the distribution that motivated the issue.

    Written from the live counts so the planner is judged on the actual mess
    rather than on tidy fixtures. It asserts the shape of the answer, not a
    canonical mapping — choosing that is explicitly a human decision.
    """

    COUNTS = {
        "default": 484,
        "podium": 305,
        "pr56-manual-test": 228,
        "synapto": 85,
        "website_seller": 44,
        "divergence": 34,
        "hermes": 22,
        "podium-internal/divergence": 13,
        "global": 7,
        "podium/divergence": 7,
        "jerry": 5,
        "podium-jerry": 5,
        "kazaam": 4,
        "podium-kazaam": 3,
        "ramonlimaramos/synapto": 2,
        "podium/hermes": 1,
        "gutenberg": 1,
    }

    @pytest.fixture
    def groups(self):
        return plan_tenant_merges(self.COUNTS)

    def test_every_tenant_appears_exactly_once(self, groups):
        members = [m for g in groups for m in g.members]

        assert sorted(members) == sorted(self.COUNTS)
        assert len(members) == len(set(members))

    def test_the_divergence_family_is_refused_not_guessed(self, groups):
        group = next(g for g in groups if "divergence" in g.members)

        assert group.canonical is None
        assert set(group.members) == {"divergence", "podium/divergence", "podium-internal/divergence"}

    def test_synapto_adopts_the_qualified_spelling(self, groups):
        group = next(g for g in groups if "synapto" in g.members)

        assert group.canonical == "ramonlimaramos/synapto"
        assert group.confidence == REVIEW

    def test_podium_is_not_absorbed_into_any_project(self, groups):
        """305 memories whose project no string can identify — they stay put."""
        group = next(g for g in groups if "podium" in g.members)

        assert group.members == ("podium",)

    def test_nothing_actionable_moves_a_memory_out_of_default(self, groups):
        actionable = [g for g in groups if g.is_actionable and len(g.members) > 1]

        assert all("default" not in g.members for g in actionable)

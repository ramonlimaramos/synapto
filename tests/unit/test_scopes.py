"""Tests for the typed memory-scope value objects."""

from __future__ import annotations

import pytest

from synapto.scopes import (
    GLOBAL_KEY,
    GLOBAL_TYPE,
    MAX_SCOPE_KEY_CHARS,
    MAX_SCOPES,
    SCOPE_TYPES,
    InvalidScopeError,
    ScopeRef,
    ScopeSet,
)


class TestScopeRefTypes:
    @pytest.mark.parametrize("scope_type", sorted(SCOPE_TYPES))
    def test_accepts_every_declared_type(self, scope_type):
        key = GLOBAL_KEY if scope_type == GLOBAL_TYPE else ("owner/repo" if scope_type == "repo" else "python")

        assert ScopeRef.parse(scope_type, key).scope_type == scope_type

    @pytest.mark.parametrize("scope_type", ["tenant", "domain", "Global", "GLOBAL", "", "  ", None, 7])
    def test_rejects_undeclared_types(self, scope_type):
        with pytest.raises(InvalidScopeError):
            ScopeRef.parse(scope_type, "python")

    def test_rejection_message_lists_the_accepted_types(self):
        with pytest.raises(InvalidScopeError, match="workflow"):
            ScopeRef.parse("tenant", "python")


class TestScopeKeyMustArriveCanonical:
    """Keys are rejected, never repaired — canonicalization is the caller's job."""

    @pytest.mark.parametrize("key", ["python", "jerry-workday", "synapto", "v0.6", "a", "a1", "some_key"])
    def test_accepts_canonical_keys(self, key):
        assert ScopeRef.parse("language", key).scope_key == key

    @pytest.mark.parametrize(
        "key",
        [
            "Python",  # uppercase
            " python",  # leading space
            "python ",  # trailing space
            "py thon",  # inner space
            "py\tthon",  # tab
            "py\nthon",  # newline
            "",  # blank
            "   ",  # whitespace only
        ],
    )
    def test_rejects_keys_needing_trim_or_lowering(self, key):
        with pytest.raises(InvalidScopeError):
            ScopeRef.parse("language", key)

    @pytest.mark.parametrize(
        "key",
        [
            "pythön",  # non-ASCII letter
            "pythоn",  # Cyrillic o — visually identical to ASCII 'o'
            "python​",  # zero-width space
            "python ",  # non-breaking space
            "pyth\x00on",  # NUL
            "pyth\x07on",  # bell
        ],
    )
    def test_rejects_unicode_control_and_invisible_characters(self, key):
        with pytest.raises(InvalidScopeError):
            ScopeRef.parse("language", key)

    @pytest.mark.parametrize("key", [None, 7, 1.5, True, ["python"], {"key": "python"}])
    def test_rejects_non_string_payloads(self, key):
        with pytest.raises(InvalidScopeError):
            ScopeRef.parse("language", key)

    @pytest.mark.parametrize("key", ["-python", "python-", ".python", "python.", "_python", "python_"])
    def test_rejects_leading_or_trailing_separators(self, key):
        with pytest.raises(InvalidScopeError):
            ScopeRef.parse("language", key)

    def test_accepts_a_key_at_the_length_limit(self):
        key = "a" * MAX_SCOPE_KEY_CHARS

        assert ScopeRef.parse("language", key).scope_key == key

    def test_rejects_an_overlong_key(self):
        with pytest.raises(InvalidScopeError, match=f"{MAX_SCOPE_KEY_CHARS}"):
            ScopeRef.parse("language", "a" * (MAX_SCOPE_KEY_CHARS + 1))

    def test_suggests_the_canonical_form_when_one_exists(self):
        # rejecting without telling the caller what to send is needless friction
        with pytest.raises(InvalidScopeError, match="python"):
            ScopeRef.parse("language", "  Python  ")


class TestGlobalScope:
    def test_accepts_the_only_valid_key(self):
        assert ScopeRef.parse(GLOBAL_TYPE, GLOBAL_KEY).scope_key == GLOBAL_KEY

    @pytest.mark.parametrize("key", ["everything", "any", "*", "all-things", "python"])
    def test_rejects_any_other_key(self, key):
        with pytest.raises(InvalidScopeError, match=GLOBAL_KEY):
            ScopeRef.parse(GLOBAL_TYPE, key)


class TestRepoScope:
    @pytest.mark.parametrize("key", ["ramonlimaramos/synapto", "owner/repo", "a/b", "some-org/some.repo"])
    def test_accepts_owner_slash_repo(self, key):
        assert ScopeRef.parse("repo", key).scope_key == key

    @pytest.mark.parametrize(
        "key",
        [
            "synapto",  # no owner
            "owner/",  # empty repo
            "/repo",  # empty owner
            "owner/repo/extra",  # too many segments
            "https://github.com/owner/repo",  # URL, not canonical form
            "git@github.com:owner/repo.git",  # SSH URL
            "/Users/ramonramos/Developer/synapto",  # local path
        ],
    )
    def test_rejects_anything_but_owner_slash_repo(self, key):
        with pytest.raises(InvalidScopeError):
            ScopeRef.parse("repo", key)

    @pytest.mark.parametrize("scope_type", ["language", "skill", "product", "workflow"])
    def test_other_types_reject_slashes(self, scope_type):
        with pytest.raises(InvalidScopeError):
            ScopeRef.parse(scope_type, "owner/repo")


class TestScopeRefIsImmutableAndDeterministic:
    def test_is_frozen(self):
        ref = ScopeRef.parse("language", "python")

        with pytest.raises(Exception):
            ref.scope_key = "elixir"

    def test_equal_refs_are_interchangeable(self):
        assert ScopeRef.parse("language", "python") == ScopeRef.parse("language", "python")
        assert len({ScopeRef.parse("language", "python"), ScopeRef.parse("language", "python")}) == 1

    def test_orders_by_type_then_key(self):
        refs = [
            ScopeRef.parse("repo", "owner/repo"),
            ScopeRef.parse("language", "python"),
            ScopeRef.parse("language", "elixir"),
        ]

        assert [(r.scope_type, r.scope_key) for r in sorted(refs)] == [
            ("language", "elixir"),
            ("language", "python"),
            ("repo", "owner/repo"),
        ]


class TestScopeSet:
    def test_empty_is_valid(self):
        # a memory with no scopes is unscoped, not invalid
        assert ScopeSet.parse([]).scopes == ()
        assert not ScopeSet.parse([])

    def test_orders_deterministically_regardless_of_input_order(self):
        forward = ScopeSet.parse([{"type": "repo", "key": "a/b"}, {"type": "language", "key": "python"}])
        reverse = ScopeSet.parse([{"type": "language", "key": "python"}, {"type": "repo", "key": "a/b"}])

        assert forward == reverse
        assert [(s.scope_type, s.scope_key) for s in forward.scopes] == [
            ("language", "python"),
            ("repo", "a/b"),
        ]

    def test_deduplicates_repeated_scopes(self):
        parsed = ScopeSet.parse([{"type": "language", "key": "python"}] * 3)

        assert len(parsed.scopes) == 1

    def test_accepts_scope_refs_directly(self):
        parsed = ScopeSet.parse([ScopeRef.parse("language", "python")])

        assert parsed.scopes == (ScopeRef.parse("language", "python"),)

    def test_accepts_the_maximum_number_of_unique_scopes(self):
        items = [{"type": "language", "key": f"lang{i}"} for i in range(MAX_SCOPES)]

        assert len(ScopeSet.parse(items).scopes) == MAX_SCOPES

    def test_rejects_more_than_the_maximum(self):
        items = [{"type": "language", "key": f"lang{i}"} for i in range(MAX_SCOPES + 1)]

        with pytest.raises(InvalidScopeError, match=str(MAX_SCOPES)):
            ScopeSet.parse(items)

    def test_duplicates_do_not_count_toward_the_maximum(self):
        # the limit is on unique scopes, so a repeated entry must not push a
        # legitimate request over the edge
        items = [{"type": "language", "key": f"lang{i}"} for i in range(MAX_SCOPES)]
        items.append({"type": "language", "key": "lang0"})

        assert len(ScopeSet.parse(items).scopes) == MAX_SCOPES

    @pytest.mark.parametrize("payload", ["not-a-list", 7, None, [7], ["language:python"], [{"type": "language"}]])
    def test_rejects_malformed_payloads(self, payload):
        with pytest.raises(InvalidScopeError):
            ScopeSet.parse(payload)

    def test_rejects_unknown_keys_in_a_scope_mapping(self):
        with pytest.raises(InvalidScopeError):
            ScopeSet.parse([{"type": "language", "key": "python", "source": "explicit"}])


class TestGlobalDoesNotCombine:
    def test_global_alone_is_valid(self):
        parsed = ScopeSet.parse([{"type": GLOBAL_TYPE, "key": GLOBAL_KEY}])

        assert parsed.scopes == (ScopeRef.parse(GLOBAL_TYPE, GLOBAL_KEY),)

    def test_global_with_any_other_scope_is_rejected(self):
        items = [{"type": GLOBAL_TYPE, "key": GLOBAL_KEY}, {"type": "language", "key": "python"}]

        with pytest.raises(InvalidScopeError, match="global"):
            ScopeSet.parse(items)

    def test_order_does_not_matter_for_the_combination_rule(self):
        items = [{"type": "language", "key": "python"}, {"type": GLOBAL_TYPE, "key": GLOBAL_KEY}]

        with pytest.raises(InvalidScopeError, match="global"):
            ScopeSet.parse(items)


class TestScopeSetIsImmutable:
    def test_is_frozen(self):
        parsed = ScopeSet.parse([{"type": "language", "key": "python"}])

        with pytest.raises(Exception):
            parsed.scopes = ()

    def test_scopes_are_a_tuple_not_a_mutable_sequence(self):
        assert isinstance(ScopeSet.parse([{"type": "language", "key": "python"}]).scopes, tuple)

    def test_equal_sets_are_interchangeable(self):
        a = ScopeSet.parse([{"type": "language", "key": "python"}])
        b = ScopeSet.parse([{"type": "language", "key": "python"}])

        assert a == b
        assert len({a, b}) == 1

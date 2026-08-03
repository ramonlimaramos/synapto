"""Tests for the domain scope value object."""

import pytest

from synapto.domain_scope import (
    MAX_DOMAIN_CHARS,
    InvalidDomainError,
    normalize_domain,
    normalize_domain_filter,
)


class TestNormalizeDomain:
    """Write path: strict canonicalization, rejects unstorable values."""

    def test_none_passes_through(self):
        assert normalize_domain(None) is None

    def test_strips_surrounding_whitespace(self):
        assert normalize_domain("  python  ") == "python"

    def test_lowercases(self):
        assert normalize_domain("Python") == "python"

    def test_strips_and_lowercases_together(self):
        assert normalize_domain("  Jerry-Workday\t") == "jerry-workday"

    def test_already_canonical_is_unchanged(self):
        assert normalize_domain("jerry-workday") == "jerry-workday"

    def test_inner_whitespace_is_preserved(self):
        # only the boundaries are noise; an inner space is part of the name the
        # caller chose, and collapsing it would silently merge distinct domains
        assert normalize_domain("  data science  ") == "data science"

    def test_empty_string_rejected(self):
        with pytest.raises(InvalidDomainError, match="domain must not be empty"):
            normalize_domain("")

    def test_whitespace_only_rejected(self):
        with pytest.raises(InvalidDomainError, match="domain must not be empty"):
            normalize_domain("   ")

    def test_length_is_validated_after_normalization(self):
        # 50 storable chars wrapped in whitespace fits VARCHAR(50) once trimmed;
        # validating the raw string would reject a value that stores fine
        padded = "  " + "x" * MAX_DOMAIN_CHARS + "  "
        assert normalize_domain(padded) == "x" * MAX_DOMAIN_CHARS

    def test_overlong_rejected_with_normalized_length(self):
        with pytest.raises(InvalidDomainError, match=r"domain exceeds 50 chars \(got 51\)"):
            normalize_domain("x" * 51)

    def test_invalid_domain_error_is_a_value_error(self):
        # lets callers that do not import the module still catch it generically
        assert issubclass(InvalidDomainError, ValueError)


class TestNormalizeDomainFilter:
    """Read path: same canonical form, but blank means "no filter" not an error."""

    def test_none_means_no_filter(self):
        assert normalize_domain_filter(None) is None

    def test_empty_string_means_no_filter(self):
        assert normalize_domain_filter("") is None

    def test_whitespace_only_means_no_filter(self):
        assert normalize_domain_filter("   ") is None

    def test_canonicalizes_like_the_write_path(self):
        assert normalize_domain_filter("  Python  ") == "python"

    def test_overlong_filter_still_rejected(self):
        with pytest.raises(InvalidDomainError, match=r"domain exceeds 50 chars \(got 51\)"):
            normalize_domain_filter("x" * 51)


class TestWriteAndReadAgree:
    """The whole point of one shared rule: stored form == queried form."""

    @pytest.mark.parametrize(
        "raw",
        ["python", "Python", "  PYTHON  ", "PyThOn\n", "\tpython "],
    )
    def test_any_spelling_maps_to_the_same_key(self, raw):
        assert normalize_domain(raw) == normalize_domain_filter(raw) == "python"

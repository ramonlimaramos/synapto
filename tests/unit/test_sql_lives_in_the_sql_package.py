"""The SQL convention, as a test instead of a review comment.

Two halves, both walked from the AST so a string hidden in an f-string or a
concatenation is found the same way as a bare literal:

* nothing outside ``synapto/sql/`` may contain a statement or a predicate
  fragment — Python chooses statements, it never writes them;
* nothing inside ``synapto/sql/`` may contain anything but string constants and
  a docstring — a module of statements has no place to hide composition.

Docstrings are skipped on the outside, since a repository docstring may name
the ``FOR UPDATE`` it relies on. The patterns are deliberately conservative
(upper-case keywords, whole words) so that user-facing advice such as
``run: CREATE EXTENSION vector;`` is not mistaken for a statement.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import synapto

PACKAGE_ROOT = Path(synapto.__file__).parent
SQL_PACKAGE = PACKAGE_ROOT / "sql"

STATEMENT = re.compile(
    r"\b(SELECT|INSERT INTO|DELETE FROM|UPDATE\s+\w+\s+SET|CREATE (TABLE|INDEX)|WITH RECURSIVE|LOCK TABLE|FOR UPDATE)\b"
)
PREDICATE_FRAGMENT = re.compile(r"^\s*(AND|OR|WHERE) [\w.]+ (=|@>|IS |IN |= ANY)")
DOCSTRING_OWNERS = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def _modules_outside_sql() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE_ROOT.rglob("*.py")
        if SQL_PACKAGE not in path.parents and "_migrations" not in path.parts
    )


def _modules_inside_sql() -> list[Path]:
    return sorted(path for path in SQL_PACKAGE.glob("*.py") if path.name != "__init__.py")


def _docstring_nodes(tree: ast.Module) -> set[int]:
    owners = [tree, *[n for n in ast.walk(tree) if isinstance(n, DOCSTRING_OWNERS)]]
    ids = set()
    for owner in owners:
        body = owner.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            ids.add(id(body[0].value))
    return ids


def _string_constants(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    skip = _docstring_nodes(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]


class TestNothingOutsideThePackageWritesSql:
    @pytest.mark.parametrize("path", _modules_outside_sql(), ids=lambda p: str(p.relative_to(PACKAGE_ROOT)))
    def test_no_statement_or_fragment_literal(self, path: Path) -> None:
        offenders = [
            f"{path.relative_to(PACKAGE_ROOT)}:{lineno}: {text.strip()[:70]!r}"
            for lineno, text in _string_constants(path)
            if STATEMENT.search(text) or PREDICATE_FRAGMENT.search(text)
        ]
        assert offenders == [], "move these into synapto/sql/:\n" + "\n".join(offenders)

    def test_the_pattern_catches_the_shapes_this_repo_used_to_have(self) -> None:
        assert STATEMENT.search("SELECT count(*) as cnt FROM entities")
        assert STATEMENT.search("INSERT INTO synapto_migrations (filename, checksum) VALUES (%s, %s) ")
        assert PREDICATE_FRAGMENT.search("AND depth_layer = %(depth_layer)s")
        assert PREDICATE_FRAGMENT.search("AND metadata @> %(metadata_filter)s::jsonb")
        assert PREDICATE_FRAGMENT.search("AND r.relation_type = ANY(%(relation_types)s)")
        assert PREDICATE_FRAGMENT.search("WHERE deleted_at IS NULL AND tenant = %s")

    def test_the_pattern_leaves_prose_alone(self) -> None:
        assert not STATEMENT.search("run: CREATE EXTENSION vector;")
        assert not STATEMENT.search("select a tenant and try again")
        assert not PREDICATE_FRAGMENT.search("and the tenant is derived from the location")


class TestNothingInsideThePackageIsCode:
    @pytest.mark.parametrize("path", _modules_inside_sql(), ids=lambda p: p.name)
    def test_only_a_docstring_and_upper_case_string_constants(self, path: Path) -> None:
        tree = ast.parse(path.read_text(), filename=str(path))
        assert ast.get_docstring(tree), f"{path.name} needs a docstring naming its table and format slots"
        for node in tree.body[1:]:
            assert isinstance(node, ast.Assign), f"{path.name}:{node.lineno} is not a constant assignment"
            (target,) = node.targets
            assert isinstance(target, ast.Name) and target.id.isupper(), f"{path.name}:{node.lineno} {ast.dump(target)}"
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str), (
                f"{path.name}:{node.lineno} {target.id} is not a string literal"
            )

    @pytest.mark.parametrize("path", _modules_inside_sql(), ids=lambda p: p.name)
    def test_every_constant_is_a_statement_or_a_fragment(self, path: Path) -> None:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body[1:]:
            text = node.value.value
            assert STATEMENT.search(text) or PREDICATE_FRAGMENT.search(text), (
                f"{path.name}:{node.lineno} {node.targets[0].id} does not look like SQL"
            )

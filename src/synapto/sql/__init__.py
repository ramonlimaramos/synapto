"""Every SQL statement Synapto runs, and nothing else.

One module per owner — ``memories``, ``entities``, ``relations``, ``scopes``,
``tenants``, ``banks``, ``metrics`` for the tables, ``search`` and ``graph`` for
the read paths, ``migrations`` and ``doctor`` for the schema and its checks.
A module here holds string constants and a docstring; no functions, no imports.
``tests/unit/test_sql_lives_in_the_sql_package.py`` enforces both halves of
that: nothing outside this package may contain a statement, and nothing inside
it may contain code.

The rule this layout makes structural: **Python chooses statements, it never
writes them.** Values travel as ``%(name)s`` / ``%s`` parameters. The only
``str.format`` slots a caller may fill are the ones a module's docstring names,
and each is filled from another constant in the same module, an integer, or a
table name from a fixed tuple — never from text a caller supplied. A statement
that needs to vary is two constants and an ``if``, not one constant and a
``.replace()``.

Callers import the owning module under the alias ``sql``::

    from synapto.sql import memories as sql

    await self._db.execute(sql.INSERT, params)
"""

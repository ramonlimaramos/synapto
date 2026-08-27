"""Bundled SQL migrations, shipped as package resources.

The SQL lives inside the package rather than at the repository root so that it
travels with every distribution. It previously sat in a root ``migrations/``
directory that the wheel never packaged, so an installed Synapto discovered no
migrations at all and initialized an empty schema.

This module exists only to give ``importlib.resources`` a stable package anchor;
it intentionally exports nothing.
"""

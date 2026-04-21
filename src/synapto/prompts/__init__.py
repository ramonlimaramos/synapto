"""Prompt loader — reads package-bundled prompt files via importlib.resources.

Prompts live as plain `.md` files next to this module so they can be reviewed,
versioned, and edited without touching Python code. Using `importlib.resources`
keeps loading compatible with every install scheme (wheel, editable, uvx, zipapp).
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files


@cache
def load_prompt(name: str) -> str:
    """Load a prompt by name from `synapto/prompts/{name}.md`.

    Args:
        name: prompt file stem (without extension), e.g. `"server_instructions"`.

    Returns:
        The prompt body as a string.

    Raises:
        FileNotFoundError: if no matching `.md` file exists in the package.
    """
    resource = files(__package__).joinpath(f"{name}.md")
    if not resource.is_file():
        raise FileNotFoundError(f"prompt not found: {name}.md")
    return resource.read_text(encoding="utf-8")


__all__ = ["load_prompt"]

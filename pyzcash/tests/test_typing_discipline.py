"""The typing discipline is part of the contract, so it is tested.

pyzcash is meant to be read, and a reader has to be able to trust the types. A
`type: ignore` in the library would mean the annotations describe something the
checker could not confirm, and the reader would have no way to know which parts
those were. There are none, and this test is what keeps it that way.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

SUPPRESSIONS = ("type: ignore", "type:ignore", "mypy: ignore", "# noqa")


def test_the_library_contains_no_type_suppressions() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{n}: {line.strip()}"
        for path in sorted(SRC.rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if any(s in line for s in SUPPRESSIONS)
    ]
    assert not offenders, "type suppressions in the library:\n" + "\n".join(
        offenders
    )


def test_mypy_is_strict_and_has_no_per_module_exceptions() -> None:
    config = tomllib.loads(PYPROJECT.read_text())["tool"]["mypy"]
    assert config["strict"] is True
    assert config["disallow_any_explicit"] is True
    assert config["warn_unreachable"] is True
    # A [[tool.mypy.overrides]] block is how strictness gets quietly relaxed for
    # one module at a time. There are none.
    assert "overrides" not in config


def test_every_public_module_ships_typed() -> None:
    """py.typed is what makes the annotations visible to downstream mypy."""
    assert (SRC / "pyzcash" / "py.typed").is_file()

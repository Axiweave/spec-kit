"""Contract tests: every bundled extension must ship inside the wheel's core_pack.

``specify extension add <id>`` resolves a bundled extension via
``specify_cli._assets.locate_bundled_extension``, which checks the wheel's
``specify_cli/core_pack/extensions/<id>/`` directory before the source
checkout. A source checkout always finds ``extensions/<id>/``, so a bundled
extension missing from the wheel force-include list passes every in-repo test
and only breaks the released wheel.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]


def _force_include() -> dict[str, str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)
    return pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]


def _bundled_extension_ids() -> list[str]:
    catalog = json.loads(
        (REPO_ROOT / "extensions" / "catalog.json").read_text(encoding="utf-8")
    )
    return sorted(
        ext_id
        for ext_id, entry in catalog["extensions"].items()
        if entry.get("bundled") and not entry.get("download_url")
    )


def test_every_bundled_extension_is_force_included():
    force_include = _force_include()
    bundled = _bundled_extension_ids()

    assert bundled, "expected at least one bundled extension in extensions/catalog.json"
    missing = [
        ext_id
        for ext_id in bundled
        if force_include.get(f"extensions/{ext_id}")
        != f"specify_cli/core_pack/extensions/{ext_id}"
    ]
    assert not missing, f"bundled extensions missing from the wheel force-include list: {missing}"

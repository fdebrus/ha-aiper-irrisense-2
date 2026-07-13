"""Smoke tests that prove the harness itself is wired correctly.

These import the integration as a namespace package (``custom_components.
aiper_irrisense.*``) exactly the way Home Assistant does, so a green run here
confirms pytest's import path, the plugin, and the package layout are all
sound before the real regression tests (crypto / const / coordinator) land in
their own branches.
"""
from __future__ import annotations

import json
from pathlib import Path

from custom_components.aiper_irrisense.const import DOMAIN

COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "aiper_irrisense"


def test_domain_constant() -> None:
    assert DOMAIN == "aiper_irrisense"


def test_manifest_matches_domain() -> None:
    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == DOMAIN
    # Version must be present and look like a dotted release string; several
    # later branches assert this stays in sync with the CHANGELOG.
    assert manifest["version"].count(".") >= 2

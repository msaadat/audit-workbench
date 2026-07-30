"""The console plan spine names capabilities the run did not schedule.

Materialized stages carry their own title, but `blocking_on`, `next_outcomes`,
and `reused_capabilities` arrive as bare capability IDs, so the frontend keeps
its own label map. These gates fail when a capability is added, renamed, or
retitled without the spine following, which would otherwise surface to auditors
as a raw ID like `planning.rcm_ready` in the middle of a sentence.
"""

from __future__ import annotations

import pathlib
import re

from app.agent.capabilities import REGISTRY_BY_WORKFLOW

LABELS_FILE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "components"
    / "agent"
    / "capabilityLabels.ts"
)
ENTRY = re.compile(r"^\s*'(?P<id>[a-z_]+\.[a-z_0-9]+)':\s*'(?P<label>[^']+)',\s*$", re.M)


def _frontend_labels() -> dict[str, str]:
    source = LABELS_FILE.read_text(encoding="utf-8")
    return {match["id"]: match["label"] for match in ENTRY.finditer(source)}


def _registered_titles() -> dict[str, str]:
    titles: dict[str, str] = {}
    for registry in REGISTRY_BY_WORKFLOW.values():
        for capability in registry.all():
            titles[capability.id] = capability.title
    return titles


def test_the_spine_can_name_every_registered_capability():
    missing = sorted(set(_registered_titles()) - set(_frontend_labels()))
    assert not missing, (
        "capabilityLabels.ts has no label for: "
        + ", ".join(missing)
        + f" — add them to {LABELS_FILE.name}."
    )


def test_spine_labels_match_the_registered_titles():
    frontend = _frontend_labels()
    drifted = {
        capability_id: (title, frontend[capability_id])
        for capability_id, title in _registered_titles().items()
        if capability_id in frontend and frontend[capability_id] != title
    }
    assert not drifted, (
        "capabilityLabels.ts disagrees with Capability.title for: "
        + "; ".join(
            f"{key}: registry={registry!r} frontend={ui!r}"
            for key, (registry, ui) in sorted(drifted.items())
        )
    )


def test_the_label_map_carries_no_capability_that_no_longer_exists():
    stale = sorted(set(_frontend_labels()) - set(_registered_titles()))
    assert not stale, (
        "capabilityLabels.ts labels capabilities that are not registered: "
        + ", ".join(stale)
    )

from __future__ import annotations

from types import SimpleNamespace

from app import workspaces
from app.agent import execution_support
from app.agent.workflow import Capability


class _Host:
    def __init__(self, workspace):
        self.ws = workspace
        self.run = {}
        self.warnings: list[str] = []
        self.sources: list[dict] = []

    def emit(self, type_, data):  # pragma: no cover - unused by these paths
        raise AssertionError("resolve_context must not emit")

    def record_model_source(self, source):
        self.sources.append(source)

    def warn(self, text):
        self.warnings.append(text)


class _Resolver:
    def __init__(self, manifest):
        self._manifest = manifest

    def resolve(self, _workspace, _capability, _unit, _scope):
        return self._manifest, SimpleNamespace(items=())


def _manifest(*omissions):
    return SimpleNamespace(
        selections=(),
        omissions=tuple(
            SimpleNamespace(source_id=source_id, reason=reason)
            for source_id, reason in omissions
        ),
    )


def _capability():
    return Capability(
        "planning.apm_ready",
        "apm",
        "Audit planning memorandum",
        "apm",
        (),
        lambda _workspace, _scope: None,
        lambda _workspace, _scope: [],
    )


def _resolve(manifest):
    host = _Host(workspaces.create_workspace("Execution support"))
    execution_support.resolve_context(
        host, _Resolver(manifest), _capability(), {"id": "planning.apm"}, object()
    )
    return host


def test_a_source_that_had_candidates_and_admitted_none_raises_a_run_warning():
    host = _resolve(
        _manifest(
            ("population_summary", "Global or per-source size limit reached."),
            ("population_summary", "Optional context source supplied no permitted items."),
        )
    )

    assert len(host.warnings) == 1
    warning = host.warnings[0]
    assert "Audit planning memorandum" in warning
    assert "population_summary" in warning
    assert "none fitted its budget" in warning


def test_a_source_with_nothing_to_offer_is_not_a_degradation():
    # No methodology pack exists in most workspaces, and a document the selector
    # declined is the selector working. Warning on either would bury the case
    # that matters under noise from the cases that do not.
    host = _resolve(
        _manifest(
            ("methodology", "Optional context selector matched no local candidates."),
            ("documents", "Local selector strategy 'lexical' did not match the candidate."),
            ("table_profiles", "Global or per-source size limit reached."),
        )
    )

    assert host.warnings == []

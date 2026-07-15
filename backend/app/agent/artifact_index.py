"""Privacy-safe engagement artifact index and deterministic target resolver."""

from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher

from .. import doc_tests, privacy, report
from ..workspaces import Workspace

STRONG_SCORE = 0.86
WEAK_SCORE = 0.54
TIE_MARGIN = 0.08


def canonical_sha1(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def _tokens(value: object) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _entry(kind: str, item_id: str, primary: str, item: dict, *, body: str = "", linked=None) -> dict:
    ref = f"{kind}:{item_id}"
    return {
        "ref": ref, "kind": kind, "id": item_id,
        "title": privacy.scrub_text(primary),
        "status": item.get("status"), "created_by": item.get("created_by"),
        "agent_run_id": item.get("agent_run_id"), "semantic_id": item.get("semantic_id"),
        "updated": item.get("updated") or item.get("created"),
        "sha1": canonical_sha1(item), "linked_refs": list(linked or []),
        "search_primary": privacy.scrub_text(primary).casefold(),
        "search_body": privacy.scrub_text(body).casefold(),
    }


def build(workspace: Workspace) -> dict:
    entries: list[dict] = []
    planning = workspace.planning
    entries.append(_entry("planning", "apm", "Audit planning memorandum", planning,
                          body=" ".join((planning.get("context") or {}).values()) if all(isinstance(v, str) for v in (planning.get("context") or {}).values()) else ""))
    for item in workspace.rcm:
        entries.append(_entry("rcm", item["id"], item.get("risk") or item.get("process") or item["id"], item,
                              body=" ".join(str(item.get(k) or "") for k in ("process", "control", "test_procedure"))))
    for item in workspace.work_program:
        entries.append(_entry("procedure", item["id"], item.get("objective") or item["id"], item,
                              body=" ".join(str(item.get(k) or "") for k in ("criteria", "method", "expected_evidence")),
                              linked=[f"rcm:{value}" for value in item.get("rcm_refs") or []]))
    for summary in doc_tests.list_tests(workspace):
        test = doc_tests.load_test(workspace, summary["id"])
        entries.append(_entry("doctest", test["id"], test.get("title") or test["id"], test,
                              body=str(test.get("kind") or ""), linked=[f"procedure:{v}" for v in test.get("procedure_refs") or []]))
        for item in test.get("items") or []:
            item_id = f"{test['id']}:{item['id']}"
            entries.append(_entry("doctest_item", item_id, item.get("label") or item["id"], item,
                                  body=" ".join(str(item.get(k) or "") for k in ("question", "summary", "auditor_note")),
                                  linked=[f"doctest:{test['id']}"]))
    for item in workspace.findings:
        entries.append(_entry("finding", item["id"], item.get("title") or item["id"], item,
                              body=" ".join(str(item.get(k) or "") for k in ("condition", "criteria", "cause", "effect", "recommendation")),
                              linked=[*[f"rcm:{v}" for v in item.get("rcm_refs") or []], *[f"procedure:{v}" for v in item.get("procedure_refs") or []]]))
    if workspace.report:
        hydrated = report.hydrate(workspace)
        entries.append(_entry("report", "working", "Audit report working content", hydrated))
    for item in workspace.analyses:
        entries.append(_entry("analysis", item["id"], item.get("title") or item["id"], item, body=str(item.get("note") or ""), linked=[f"table:{item['table']}"] if item.get("table") else []))
    for item in workspace.rulesets:
        entries.append(_entry("ruleset", item["id"], item.get("title") or item["id"], item, body=str(item.get("note") or ""), linked=[f"table:{item['table']}"]))
    for item in workspace.tiles:
        entries.append(_entry("tile", item["id"], item.get("title") or item["id"], item, body=str(item.get("note") or ""), linked=[f"table:{item['table']}"] if item.get("table") else []))
    for item in workspace.documents:
        entries.append(_entry("document", item["id"], item.get("title") or item.get("filename") or item["id"], item, body=str(item.get("category") or "")))
    for name in workspace.table_names():
        source = next((v for v in [*workspace.tables, *workspace.joins] if v.get("name") == name), {"name": name})
        entries.append(_entry("table", name, name, source))
    revision = canonical_sha1([{k: item.get(k) for k in ("ref", "sha1")} for item in entries])
    return {"revision": f"sha1:{revision}", "entries": entries}


def compact(index: dict) -> dict:
    return {
        "revision": index["revision"],
        "artifacts": [
            {k: item.get(k) for k in ("ref", "kind", "title", "status", "created_by", "semantic_id", "updated", "linked_refs")}
            for item in index["entries"]
        ],
    }


def resolve(index: dict, kind: str, selector: str | None, resolved_id: str | None = None) -> dict:
    candidates = [item for item in index["entries"] if item["kind"] == kind]
    needle = str(resolved_id or selector or "").strip()
    exact_refs = {needle, f"{kind}:{needle}"}
    exact = next((item for item in candidates if item["ref"] in exact_refs or item["id"] == needle), None)
    if exact:
        return _resolution(index, exact, 1.0, "exact structured reference", [])
    normalized = " ".join(_tokens(needle))
    if not normalized:
        return _unresolved(index, [])
    exact_names = [item for item in candidates if " ".join(_tokens(item["search_primary"])) == normalized]
    if len(exact_names) == 1:
        return _resolution(index, exact_names[0], 0.98, "exact normalized title", [])
    if len(exact_names) > 1:
        return _unresolved(index, [
            {"ref": item["ref"], "id": item["id"], "title": item["title"], "score": 0.98, "reason": "exact normalized title", "sha1": item["sha1"]}
            for item in exact_names
        ], confidence=0.98, reason="multiple exact title matches")
    needle_tokens = _tokens(needle)
    ranked = []
    for item in candidates:
        primary_tokens = _tokens(item["search_primary"])
        body_tokens = _tokens(item["search_body"])
        primary_overlap = len(needle_tokens & primary_tokens) / max(1, len(needle_tokens))
        body_overlap = len(needle_tokens & body_tokens) / max(1, len(needle_tokens))
        fuzzy = SequenceMatcher(None, normalized, " ".join(_tokens(item["search_primary"]))).ratio()
        score = max(primary_overlap * 0.94, body_overlap * 0.72, fuzzy * 0.68)
        if score >= WEAK_SCORE:
            reason = "title token match" if primary_overlap >= body_overlap else "narrative token match"
            ranked.append((score, reason, item))
    ranked.sort(key=lambda value: (-value[0], value[2]["ref"]))
    display = [
        {"ref": item["ref"], "id": item["id"], "title": item["title"], "score": round(score, 3), "reason": reason, "sha1": item["sha1"]}
        for score, reason, item in ranked[:8]
    ]
    if not ranked:
        return _unresolved(index, display)
    top_score, reason, top = ranked[0]
    tied = len(ranked) > 1 and top_score - ranked[1][0] < TIE_MARGIN
    if top_score >= STRONG_SCORE and not tied:
        return _resolution(index, top, top_score, reason, display)
    return _unresolved(index, display, confidence=top_score, reason="weak or ambiguous match")


def by_ref(index: dict, ref: str) -> dict | None:
    return next((item for item in index["entries"] if item["ref"] == ref), None)


def _resolution(index: dict, item: dict, confidence: float, reason: str, candidates: list[dict]) -> dict:
    return {
        "index_revision": index["revision"], "resolved_id": item["id"],
        "resolved_ref": item["ref"], "confidence": round(confidence, 3),
        "reason": reason, "candidates": candidates, "sha1": item["sha1"],
        "title": item["title"], "created_by": item.get("created_by"),
    }


def _unresolved(index: dict, candidates: list[dict], confidence: float = 0.0, reason: str = "no match") -> dict:
    return {
        "index_revision": index["revision"], "resolved_id": None,
        "resolved_ref": None, "confidence": round(confidence, 3),
        "reason": reason, "candidates": candidates,
    }

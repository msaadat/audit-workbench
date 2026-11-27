"""Scripted LLM fake and default response builders for agent workflow tests."""

from __future__ import annotations

import json

from app import llm


def _analysis_definitions_response(user: str) -> dict:
    """Answer every work item the authoring turn was given, as Polars code."""
    payload = json.loads(user.split("\n\nYour previous response")[0])
    analyses = []
    for item in payload.get("WORK ITEMS") or []:
        frame, column = item["frame"], (item.get("columns") or ["*"])[0]
        if item.get("kind") == "explore":
            code = f"result = tables['{frame}'].group_by('{column}').agg(pl.len().alias('rows'))"
            policy = "informational"
        else:
            code = f"result = tables['{frame}'].filter(pl.col('{column}').is_duplicated())"
            policy = "exception_rows"
        analyses.append({
            "ref": item["ref"],
            "frame": frame,
            "title": f"{item['ref']} on {frame}",
            "note": "A reused key is the shape a double posting takes.",
            "code": code,
            "outcome_policy": policy,
        })
    return {"analyses": analyses, "declined": []}


def _analysis_reading_response(user: str) -> dict:
    """Settle the register: keep every measurement, add one assertion per frame."""
    payload = json.loads(user.split("\n\nYour previous response")[0])
    added = []
    frames = {frame["table"]: frame for frame in payload.get("FRAME MAP") or []}

    def frame_columns(name):
        frame = frames[name]
        if not frame.get("join"):
            return [c["name"] for c in frame.get("columns") or []]
        join = frame["join"]
        return frame_columns(join["left"]) + [
            (frame.get("renamed") or {}).get(c, c)
            for c in frame_columns(join["right"])
            if c not in join.get("right_on", [])
        ]

    for frame in frames.values():
        if not frame.get("selectable", True):
            continue
        columns = frame_columns(frame["table"])
        if not columns:
            continue
        added.append(
            {
                "frame": frame["table"],
                "columns": columns[:1],
                "assertion": f"{frame['table']} carries no repeated {columns[0]}.",
                "why": "A reused key is the shape a double posting takes.",
            }
        )
    return {"keep": [], "add": added[:12], "decline": [], "unanswerable": []}


def _join_utility_response(user: str) -> dict:
    """Retain the first route to each table pair and reject its competitors."""
    payload = json.loads(user.split("\nRepair the prior response")[0])
    catalog = payload["JOIN CANDIDATES"]
    table_columns = catalog.get("table_columns") or {}
    decisions: list[dict] = []
    retained: set[frozenset[str]] = set()
    for candidate in catalog["candidates"]:
        pair = frozenset((candidate["left"], candidate["right"]))
        if pair in retained:
            decisions.append(
                {
                    "ref": candidate["ref"],
                    "decision": "reject",
                    "rationale": "Another route between these tables was retained.",
                }
            )
            continue
        retained.add(pair)
        decisions.append(
            {
                "ref": candidate["ref"],
                "decision": "retain",
                "rationale": "The pair supports a test neither side supports alone.",
                "hypothesis": "Every key on the left side resolves on the right.",
                "columns": [
                    f"{table}.{(table_columns.get(table) or ['id'])[0]}"
                    for table in (candidate["left"], candidate["right"])
                ],
                "requires": [candidate["left"], candidate["right"]],
            }
        )
    return {"decisions": decisions}


def _analysis_summary_response(_user: str) -> dict:
    """A structurally valid EDA memo submission for any workspace."""
    return {
        "lead": "The imported population reconciles and nothing requires escalation.",
        "data_received": "The imported tables were profiled.",
        "findings": [
            {"prose": "Nothing requiring escalation.", "procedures": []},
        ],
        "reliance": {"prose": "No material gaps.", "procedures": []},
        "further_work": "Nothing outstanding.",
    }


def _document_analysis_response(user: str) -> dict:
    source = user.split("RAW SOURCE CHUNK:\n", 1)[-1].strip()
    page = int(user.split("\nPAGE: ", 1)[1].splitlines()[0])
    excerpt = source[:240].strip()
    return {
        "summary_markdown": "## Document summary\n\nThe document describes requirements and responsibilities. [C1]",
        "audit_notes_markdown": (
            "## Audit notes\n\nThe requirements need operating evidence before implementation "
            "can be concluded. [C1]"
        ),
        "citations": [{"id": "C1", "page": page, "excerpt": excerpt}],
    }


def _streamed(message: dict, on_delta) -> dict:
    """Deliver a scripted reply the way a streaming provider would."""
    if on_delta is None:
        return message
    content = str(message.get("content") or "")
    half = len(content) // 2 or len(content)
    for piece in (content[:half], content[half:]):
        if piece:
            on_delta(piece)
    return message


def _report_conclusion_response(user: str) -> dict:
    """The least favourable rating the band allows, or none where it allows none."""
    band = json.loads(user)["rating_band"]
    return {
        "rating": band["allowed"][-1] if band["assignable"] and band["allowed"] else "",
        "conclusion": "**Scripted conclusion.** The control environment needs attention.",
    }


def _report_key_findings_response(user: str) -> dict:
    return {
        "rows": [
            {
                "finding_id": item["finding_id"],
                "key_finding": "Scripted key finding.",
                "recommendation": "Address the scripted finding.",
            }
            for item in json.loads(user)["findings"]
        ]
    }


def _scripted_user_view(messages: list[dict]) -> str:
    """What the model was asked, as the one string a script reads."""
    if not messages or messages[-1].get("role") != "user":
        return messages[-1]["content"] if messages else ""
    return "\n\n".join(
        message["content"]
        for message in messages[1:]
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    )


def _planning_cycle_response(user: str) -> dict:
    """One step, holding whatever the fixture happens to have."""
    payload = json.loads(user.split("\n\nPREVIOUS CYCLE DRAFT:")[0])
    types = [
        entry["document_type"] for entry in payload.get("DOCUMENT TYPES HELD") or []
    ]
    tables = [entry["table"] for entry in payload.get("TABLES") or []]
    step = {
        "name": (payload.get("EXISTING STEP NAMES") or ["Process"])[0],
        "roles": [{"name": "record", "document_type": types[0]}] if types else [],
        "populations": [{"table": tables[0], "anchor": True}] if tables else [],
        "themes": [],
    }
    return {
        "name": "Engagement cycle",
        "steps": [step],
        "cross_cutting": {
            "name": "Cycle operations",
            "themes": list(payload.get("PLANNED RISK THEMES") or []),
        },
    }


def _rcm_controls_response(user: str) -> dict:
    """One control per supplied risk, named from the risk it answers."""
    payload = json.loads(user.split("\nRepair the prior response")[0])
    rows = payload.get("RISKS") or payload.get("CONTROLS TO CORRECT") or []
    return {
        "controls": [
            {
                "row_index": row["row_index"],
                "control": (
                    f"Management asserts a control over "
                    f"{(row.get('risk') or 'this risk').rstrip('.').lower()}."
                ),
                "control_type": "preventive",
            }
            for row in rows
        ]
    }


def _rcm_attributes_response(user: str) -> dict:
    """One attribute per supplied row, derived from the row it belongs to."""
    payload = json.loads(user.split("\nRepair the prior response")[0])
    rows = payload.get("ROWS") or [
        item["row"] | {"row_index": item["row_index"]}
        for item in payload.get("ATTRIBUTES TO CORRECT") or []
    ]
    return {
        "attributes": [
            {
                "row_index": row["row_index"],
                "control_attributes": [
                    {
                        "key": "control_operates",
                        "assertion": "Operational",
                        "requirement": (
                            f"{row.get('control') or 'The control'} operates as "
                            "described."
                        ),
                        "evidence_kind": "manual_inspection",
                    }
                ],
            }
            for row in rows
        ]
    }


class FakeAgentLLM:
    """Scripted model for agent-run tests: dispatches on the stable
    ``[agent:<stage>]`` tag each prompt starts with. Override any stage's
    response (a dict, or a callable receiving the joined user turns) per test."""

    DEFAULTS = {
        "agent:document_category": {
            "category": "background",
            "confidence": "medium",
            "rationale": "A fixture document with no stated category.",
        },
        "agent:join_utility": _join_utility_response,
        "agent:analysis_reading": _analysis_reading_response,
        "agent:analysis_definitions": _analysis_definitions_response,
        "agent:analysis_summary": _analysis_summary_response,
        "agent:analysis_promotion": {
            "promote": False,
            "reason": "The procedure describes the population rather than a control.",
        },
        "agent:fix_code": {"code": "result = transactions.head(0)"},
        "agent:planning_cycle": _planning_cycle_response,
        "agent:rcm_controls": _rcm_controls_response,
        "agent:rcm_attributes": _rcm_attributes_response,
        "agent:document_analysis_map": _document_analysis_response,
        "agent:document_classification": {
            "document_type": "other",
            "document_type_other": "Test fixture document",
            "confidence": "low",
            "rationale": "A fixture document with no catalogued form.",
        },
        "agent:report_overview": {
            "content": (
                "## Introduction\nScripted introduction.\n\n"
                "## Objective and Scope\nScripted objective.\n\nScripted scope."
            )
        },
        "agent:report_conclusion": _report_conclusion_response,
        "agent:report_key_findings": _report_key_findings_response,
    }

    def __init__(self, overrides: dict | None = None):
        self.overrides = overrides or {}
        self.calls: list[dict] = []

    def __call__(
        self,
        messages,
        tools=None,
        temperature=0.0,
        profile="assistant",
        tool_choice=None,
        on_delta=None,
        response_format=None,
        reasoning=None,
    ):
        system = messages[0]["content"]
        tag = system[1 : system.index("]")] if system.startswith("[") else ""
        self.calls.append(
            {
                "tag": tag,
                "profile": profile,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "response_format": response_format,
                "streamed": on_delta is not None,
                "reasoning": reasoning,
            }
        )
        response = self.overrides.get(tag, self.DEFAULTS.get(tag))
        if callable(response):
            response = response(_scripted_user_view(messages))
        if response is None:
            raise llm.LLMError(f"FakeAgentLLM has no script for '{tag}'.")
        if isinstance(response, dict) and response and (
            "tool_calls" in response
            or set(response).issubset({"content", "tool_calls", "usage"})
        ):
            return _streamed(response, on_delta)
        if tools and isinstance(tool_choice, dict):
            name = (
                tool_choice.get("function", {}).get("name")
                if isinstance(tool_choice.get("function"), dict)
                else None
            )
            if name:
                return _streamed(
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "fake_tool_call",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(response),
                                },
                            }
                        ],
                    },
                    on_delta,
                )
        return _streamed({"content": json.dumps(response)}, on_delta)

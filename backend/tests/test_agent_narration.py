"""Narration: the run state an auditor actually reads.

These tests pin the projection, not the prose. What matters is that a blocked
unit becomes a question with a suggested command, that a run ends by saying
something, and that no capability id or unit error code reaches a surface the
auditor reads.
"""

import re

from app import assistant_chats
from app.agent import narration, store
from app.workspaces import write_json_atomic


def _run(status="completed_with_open_items", stages=None, **fields):
    return {
        "id": "20260726-000000-aaaaaa",
        "workspace_id": "ws",
        "schema_version": 3,
        "engine": "workflow",
        "kind": "audit",
        "mode": "auto",
        "status": status,
        "created": "2026-07-26T00:00:00+00:00",
        "started": "2026-07-26T00:00:00+00:00",
        "finished": "2026-07-26T00:01:00+00:00",
        "command": {"id": "cmd_1", "source": "chat", "text": "generate the apm"},
        "plan": {"stages": []},
        "actions": [],
        "approvals": [],
        "interactions": [],
        "messages": [],
        "narration": [],
        "warnings": [],
        "findings": [],
        "usage": {},
        "summary_markdown": "# Audit workflow summary\n\n- Open workflow units: 1\n",
        "error": None,
        "workflow": {"stages": stages or [], "requested_outcomes": [], "next_outcomes": []},
        **fields,
    }


def _stage(status="succeeded", units=(), title="Document analysis", stage_id="document_analysis"):
    return {
        "id": stage_id,
        "capability": "documents.analysis_generated",
        "title": title,
        "status": status,
        "units": list(units),
        "started_at": "2026-07-26T00:00:00+00:00",
        "finished_at": "2026-07-26T00:00:12+00:00",
    }


def _unit(status="succeeded", error=None, title="Consolidate analysis — org_chart", unit_id="u1"):
    return {"id": unit_id, "title": title, "status": status, "error": error, "attempts": 1}


# --------------------------------------------------------------------------- #
# Blockers
# --------------------------------------------------------------------------- #
def test_stopped_unit_becomes_a_question_with_a_runnable_answer():
    run = _run(stages=[_stage("review_required", [
        _unit(),
        _unit("awaiting_confirmation", "document_has_no_extractable_text", unit_id="u2"),
    ])])
    blockers = narration.blockers(run)
    assert len(blockers) == 1
    blocker = blockers[0]
    assert blocker["subject"] == "org_chart"
    assert "no text I can read" in blocker["message"]
    assert blocker["code"] not in blocker["message"]
    # Suggestions are ordinary commands, so answering one needs no new endpoint.
    assert [item["label"] for item in blocker["suggestions"]] == [
        "Skip it and continue",
        "Try again with OCR",
    ]
    assert blocker["suggestions"][0]["command"] == (
        "Skip org_chart and continue with the remaining work."
    )


def test_unmapped_error_code_still_reads_as_a_sentence():
    run = _run(stages=[_stage("failed", [_unit("failed", "some_new_unmapped_code")])])
    blocker = narration.blockers(run)[0]
    assert blocker["severity"] == "failed"
    assert "some new unmapped code" in blocker["message"]
    # The raw identifier never appears; a document literally named org_chart
    # still does, because that is the auditor's own filename.
    assert "some_new_unmapped_code" not in blocker["message"]
    # The identity is still recoverable for support, just not as the message.
    assert blocker["code"] == "some_new_unmapped_code"


def test_units_stopped_for_one_reason_group_into_one_question():
    run = _run(stages=[_stage("review_required", [
        _unit("awaiting_confirmation", "document_has_no_extractable_text", "Consolidate analysis — a", "u1"),
        _unit("awaiting_confirmation", "document_has_no_extractable_text", "Consolidate analysis — b", "u2"),
    ])])
    blockers = narration.blockers(run)
    assert len(blockers) == 1
    assert blockers[0]["unit_ids"] == ["u1", "u2"]
    assert "2 items stopped" in blockers[0]["message"]


# --------------------------------------------------------------------------- #
# Closing turn
# --------------------------------------------------------------------------- #
def test_a_run_that_produced_and_blocked_says_both():
    run = _run(stages=[
        _stage("succeeded", [_unit(), _unit(unit_id="u2")], "Document chunk analysis", "chunks"),
        _stage("review_required", [_unit("awaiting_confirmation", "document_has_no_extractable_text", unit_id="u3")]),
    ])
    text = narration.closing_text(run)
    assert text.startswith("Done — document chunk analysis (2 items).")
    assert "1 thing needs a decision from you — the options are below." in text
    # The blocker card states the question and offers the answers; repeating the
    # sentence here would print it twice, adjacent, on the same screen.
    assert "org_chart has no text I can read" not in text


def test_a_run_that_committed_nothing_but_is_blocked_does_not_claim_nothing_was_needed():
    run = _run(stages=[_stage("review_required", [
        _unit("awaiting_confirmation", "document_has_no_extractable_text"),
    ])])
    text = narration.closing_text(run)
    assert "Nothing needed doing" not in text
    assert text.startswith("I couldn't commit anything")


def test_a_failed_run_leads_with_the_reason_and_offers_a_way_forward():
    run = _run("failed", error="The model did not return usable JSON.")
    text = narration.closing_text(run)
    assert text.startswith("I couldn't finish this one.")
    assert "The model did not return usable JSON." in text
    assert "retry" in text


def test_closing_text_never_leaks_a_capability_id():
    run = _run(stages=[_stage("succeeded", [_unit()])])
    run["workflow"]["next_outcomes"] = ["planning.apm_ready"]
    run["status"] = "completed_with_open_items"
    text = narration.closing_text(run)
    assert "planning.apm_ready" not in text
    assert "apm ready" in text


# --------------------------------------------------------------------------- #
# Auto mode
# --------------------------------------------------------------------------- #
def test_auto_mode_skips_an_unreadable_document_instead_of_asking():
    from app.agent import documents_execution
    from app.agent.executors.documents import DOCUMENT_TEXT_UNAVAILABLE

    class Fake:
        run = {"workflow": {"scope": {"permission_mode": False}}, "narration": []}
        emit = staticmethod(lambda *_: None)
        _title = staticmethod(lambda document_id: "org_chart")

    result = documents_execution.DocumentWorkflowExecution._unreadable_document(Fake(), "doc_1")
    assert result.status == "skipped"
    # The code is retained: the reason a document contributed nothing stays on
    # the record even though nobody was asked about it.
    assert result.error == DOCUMENT_TEXT_UNAVAILABLE
    assert "Skipped org_chart" in Fake.run["narration"][0]["text"]


def test_permission_mode_still_asks_about_an_unreadable_document():
    from app.agent import documents_execution

    class Fake:
        run = {"workflow": {"scope": {"permission_mode": True}}, "narration": []}
        emit = staticmethod(lambda *_: None)
        _title = staticmethod(lambda document_id: "org_chart")

    result = documents_execution.DocumentWorkflowExecution._unreadable_document(Fake(), "doc_1")
    assert result.status == "awaiting_confirmation"
    assert Fake.run["narration"] == []


def test_a_skipped_document_is_reported_and_is_not_a_blocker():
    run = _run("completed", stages=[_stage("succeeded", [
        _unit(title="Consolidate analysis — minutes", unit_id="u1"),
        _unit("skipped", "document_has_no_extractable_text", unit_id="u2"),
    ])])
    assert narration.blockers(run) == []
    assert assistant_chats._run_projection(run)["pending_attention"] is False
    text = narration.closing_text(run)
    # Auto mode decided on the auditor's behalf, so the decision is stated.
    assert "I skipped org_chart — it has no text I can read" in text


def test_a_run_that_only_skipped_does_not_claim_nothing_was_needed():
    run = _run("completed", stages=[_stage("succeeded", [
        _unit("skipped", "document_has_no_extractable_text", unit_id="u1"),
    ])])
    assert "Nothing needed doing" not in narration.closing_text(run)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def test_note_and_say_append_and_publish():
    run = _run()
    events = []
    narration.note(run, lambda type_, data: events.append((type_, data)), "Working on it")
    narration.say(run, lambda type_, data: events.append((type_, data)), "All done.")
    assert [item[0] for item in events] == ["narration", "message"]
    assert run["narration"][0]["text"] == "Working on it"
    assert run["messages"][0] == {
        "role": "agent",
        "content": "All done.",
        "at": run["messages"][0]["at"],
    }


def test_narration_log_is_capped():
    run = _run()
    for index in range(narration.NARRATION_LIMIT + 25):
        narration.note(run, lambda *_: None, f"line {index}")
    assert len(run["narration"]) == narration.NARRATION_LIMIT
    assert run["narration"][-1]["text"] == f"line {narration.NARRATION_LIMIT + 24}"


def test_say_ignores_empty_text():
    run = _run()
    assert narration.say(run, lambda *_: None, "   ") is None
    assert run["messages"] == []


def test_milestone_is_structured_bounded_and_idempotent():
    run = _run()
    events = []
    payload = {
        "capability": "analysis.executed",
        "stage_id": "analysis_execution",
        "status": "completed_with_issues",
        "headline": "Data analysis complete",
        "summary": "Analyzed two scoped tables.",
        "metrics": [{"label": f"Metric {index}", "value": index} for index in range(12)],
        "highlights": [
            {
                "severity": "warning",
                "label": f"Issue {index}",
                "detail": "Potential exception rows.",
                "artifact_ref": f"analysis:a{index}",
            }
            for index in range(6)
        ],
        "artifact_refs": ["analysis:a1", "analysis:a1", "analysis:a2"],
    }
    first = narration.milestone(
        run, lambda type_, data: events.append((type_, data)), **payload
    )
    second = narration.milestone(
        run, lambda type_, data: events.append((type_, data)), **payload
    )

    assert first is not None and second is None
    assert len(run["milestones"]) == 1
    assert len(first["metrics"]) == 8
    assert len(first["highlights"]) == 3
    assert first["artifact_refs"] == ["analysis:a1", "analysis:a2"]
    assert [item[0] for item in events] == ["milestone"]


def test_stage_handoff_names_what_finished_and_what_is_next():
    stage = _stage("succeeded", [_unit()], title="Audit planning memorandum")
    assert narration.stage_handoff(stage, "Risk and control matrix") == (
        "Audit planning memorandum is done — now working on risk and control matrix."
    )


def test_stage_handoff_counts_the_units_that_still_need_a_person():
    stage = _stage(
        "review_required",
        [_unit(), _unit("failed", "boom", unit_id="u2")],
        title="Executable test specifications",
    )
    assert narration.stage_handoff(stage, "Findings") == (
        "Executable test specifications is done, with 1 item needing you"
        " — now working on findings."
    )


def test_stage_handoff_is_silent_when_nothing_follows():
    # The closing turn is a run's last word; a handoff with nowhere to hand off
    # to would only pre-empt it.
    assert narration.stage_handoff(_stage("succeeded", [_unit()]), "") == ""


# --------------------------------------------------------------------------- #
# Projection
# --------------------------------------------------------------------------- #
def test_projection_prefers_the_agents_own_words_over_a_markdown_heading():
    run = _run(stages=[_stage("succeeded", [_unit()])])
    run["messages"] = [{"role": "agent", "content": "Done — document analysis.", "at": run["finished"]}]
    projection = assistant_chats._run_projection(run)
    assert projection["summary_line"] == "Done — document analysis."
    assert not projection["summary_line"].startswith("#")


def test_projection_derives_a_sentence_for_runs_recorded_before_narration():
    run = _run("completed", stages=[_stage("succeeded", [_unit()])])
    projection = assistant_chats._run_projection(run)
    assert projection["summary_line"].startswith("Done — document analysis")
    assert projection["status_label"] == "Done"


def test_a_silently_blocked_unit_now_raises_attention():
    run = _run(stages=[_stage("review_required", [
        _unit("awaiting_confirmation", "document_has_no_extractable_text"),
    ])])
    # No approval and no typed interaction: this is exactly the shape that used
    # to end a run with an unanswered question and no signal anywhere.
    assert run["approvals"] == [] and run["interactions"] == []
    projection = assistant_chats._run_projection(run)
    assert projection["pending_attention"] is True
    assert len(projection["blockers"]) == 1


def test_review_items_are_stated_in_full_because_they_get_no_card():
    run = _run(stages=[_stage("review_required", [
        _unit("awaiting_confirmation", "generated_analysis_awaits_auditor_review"),
    ])])
    text = narration.closing_text(run)
    assert "1 item is waiting on your review:" in text
    assert "needs your review before it can be used" in text


def test_review_only_items_do_not_demand_attention():
    run = _run(stages=[_stage("review_required", [
        _unit("awaiting_confirmation", "generated_analysis_awaits_auditor_review"),
    ])])
    projection = assistant_chats._run_projection(run)
    assert projection["pending_attention"] is False
    assert projection["blockers"][0]["severity"] == "review"
    assert projection["blockers"][0]["where"] == "documents"


def test_projection_carries_the_narration_tail_not_the_whole_log():
    run = _run()
    for index in range(30):
        narration.note(run, lambda *_: None, f"line {index}")
    projection = assistant_chats._run_projection(run)
    assert len(projection["narration"]) == 12
    assert projection["narration"][-1]["text"] == "line 29"


def test_chat_projects_durable_milestones_as_transcript_items(workspace_with_data):
    ws = workspace_with_data
    chat = assistant_chats.create_chat(ws)
    run = store.new_command_run(
        ws,
        "auto",
        {"source": "chat", "text": "analyze the data", "chat_id": chat["id"]},
    )
    narration.milestone(
        run,
        lambda *_: None,
        capability="analysis.executed",
        stage_id="analysis_execution",
        status="completed",
        headline="Data analysis complete",
        summary="Analyzed two scoped tables.",
    )
    store.save_run(ws, run)

    loaded = assistant_chats.get_chat(ws, chat["id"])
    milestones = [
        item for item in loaded["transcript"] if item["type"] == "milestone"
    ]
    assert len(milestones) == 1
    assert milestones[0]["milestone"]["headline"] == "Data analysis complete"


def test_a_milestone_is_read_before_the_handoff_it_shares_a_timestamp_with(
    workspace_with_data,
):
    # Both are written in one breath, so they land in the same millisecond far
    # more often than not. The result has to come before the sentence pointing
    # at what is next, whichever way the clock rounds.
    ws = workspace_with_data
    chat = assistant_chats.create_chat(ws)
    run = store.new_command_run(
        ws,
        "auto",
        {"source": "chat", "text": "start planning", "chat_id": chat["id"]},
    )
    at = "2026-07-26T00:00:30.500+00:00"
    run["milestones"] = [{
        "id": "apm:completed:abc123", "capability": "planning.apm_ready",
        "stage_id": "apm", "status": "completed",
        "headline": "Audit planning memorandum ready", "summary": "Prepared it.",
        "metrics": [], "highlights": [], "artifact_refs": [],
        "summary_sha1": "abc123", "created_at": at,
    }]
    run["messages"] = [{
        "role": "agent",
        "content": "Audit planning memorandum is done — now working on risk and control matrix.",
        "at": at,
    }]
    store.save_run(ws, run)

    transcript = assistant_chats.get_chat(ws, chat["id"])["transcript"]
    kinds = [item["type"] for item in transcript if item.get("derived")]
    assert kinds.index("milestone") < kinds.index("message")


def test_deleting_a_chat_stops_the_runs_it_started(workspace_with_data):
    ws = workspace_with_data
    chat = assistant_chats.create_chat(ws)
    run = store.new_command_run(ws, "auto", {"source": "chat", "text": "generate the apm", "chat_id": chat["id"]})
    run["status"] = "interrupted"
    store.save_run(ws, run)

    assistant_chats.delete_chat(ws, chat["id"])

    stopped = store.load_run(ws, run["id"])
    assert stopped["status"] == "cancelled"
    assert stopped["cancellation"]["source"] == "chat_delete"


def test_a_run_whose_chat_was_deleted_stops_haunting_every_other_chat(workspace_with_data):
    ws = workspace_with_data
    orphan_chat = assistant_chats.create_chat(ws)
    orphan = store.new_command_run(ws, "auto", {"source": "chat", "text": "generate the apm", "chat_id": orphan_chat["id"]})
    orphan["status"] = "interrupted"
    store.save_run(ws, orphan)
    # Remove the directory without going through delete_chat, which is what a
    # crash — or any older build — leaves behind.
    import shutil

    shutil.rmtree(assistant_chats.chat_dir(ws, orphan_chat["id"]))

    fresh = assistant_chats.create_chat(ws)
    loaded = assistant_chats.get_chat(ws, fresh["id"])
    assert loaded["active_workspace_run"] is None
    assert loaded["transcript"] == []


def test_a_message_is_never_queued_onto_a_run_whose_chat_was_deleted(workspace_with_data):
    ws = workspace_with_data
    orphan_chat = assistant_chats.create_chat(ws)
    orphan = store.new_command_run(ws, "auto", {"source": "chat", "text": "generate the apm", "chat_id": orphan_chat["id"]})
    orphan["status"] = "interrupted"
    store.save_run(ws, orphan)
    import shutil

    shutil.rmtree(assistant_chats.chat_dir(ws, orphan_chat["id"]))

    # An orphan is resumable, so it used to answer _active_run and swallow every
    # later message as a pending command on a run that would never execute.
    assert assistant_chats._active_run(ws) is None


def test_a_run_started_without_a_chat_is_not_treated_as_orphaned(workspace_with_data):
    ws = workspace_with_data
    run = store.new_command_run(ws, "auto", {"source": "tab_button", "text": "generate the apm"})
    run["status"] = "executing"
    store.save_run(ws, run)

    chat = assistant_chats.create_chat(ws)
    loaded = assistant_chats.get_chat(ws, chat["id"])
    assert (loaded["active_workspace_run"] or {}).get("run_id") == run["id"]


def test_a_queued_command_card_does_not_repeat_the_message_it_came_from(workspace_with_data):
    ws = workspace_with_data
    # The queued projection is for a command waiting on a run this chat does not
    # own — otherwise the run has its own card in this transcript already.
    owner = assistant_chats.create_chat(ws)
    chat = assistant_chats.create_chat(ws)
    run = store.new_command_run(ws, "auto", {"source": "chat", "text": "generate the apm", "chat_id": owner["id"]})
    run["status"] = "executing"
    store.save_run(ws, run)
    text = "Skip org_chart and continue with the remaining work."
    record = assistant_chats._read(ws, chat["id"])
    record["messages"].append({
        "id": "msg_1", "ordinal": 1, "role": "user", "kind": "text", "content": text,
        "created_at": "2026-07-26T00:00:00+00:00", "state": "complete",
        "requested_intent": "act", "resolved_intent": "act", "reply_to_id": None,
        "artifact_ids": [], "error": None,
        "outcome": {"kind": "command_queued", "run_id": run["id"], "command_id": "cmd_1", "position": 2},
    })
    write_json_atomic(assistant_chats._chat_path(ws, chat["id"]), record)

    loaded = assistant_chats.get_chat(ws, chat["id"])
    card = next(item for item in loaded["transcript"] if item.get("type") == "run" and "command" in item["id"])
    assert card["title"] != text
    assert text not in card["current_activity"]
    assert "generate the apm" in card["current_activity"]
    assert card["status_label"] == "Queued"

    # A parent that ended without draining its queue leaves the command with
    # nothing to run it, so it must stop claiming it is still on its way.
    run["status"] = "failed"
    store.save_run(ws, run)
    reloaded = assistant_chats.get_chat(ws, chat["id"])
    stranded = next(item for item in reloaded["transcript"] if item.get("type") == "run" and "command" in item["id"])
    assert stranded["status_label"] == "Didn't run"
    assert "send it again" in stranded["current_activity"]


def test_a_queued_command_that_later_ran_shows_the_run_it_became(workspace_with_data):
    ws = workspace_with_data
    chat = assistant_chats.create_chat(ws)
    parent = store.new_command_run(ws, "auto", {"source": "chat", "text": "generate the apm", "chat_id": chat["id"]})
    parent["status"] = "completed"
    store.save_run(ws, parent)
    text = "Skip org_chart and continue with the remaining work."
    record = assistant_chats._read(ws, chat["id"])
    record["messages"].append({
        "id": "msg_started", "ordinal": 1, "role": "user", "kind": "text", "content": "generate the apm",
        "created_at": "2026-07-26T00:00:00+00:00", "state": "complete",
        "requested_intent": "act", "resolved_intent": "act", "reply_to_id": None,
        "artifact_ids": [], "error": None,
        "outcome": {"kind": "run_started", "run_id": parent["id"], "command_id": "cmd_0"},
    })
    record["messages"].append({
        "id": "msg_queued", "ordinal": 2, "role": "user", "kind": "text", "content": text,
        "created_at": "2026-07-26T00:01:00+00:00", "state": "complete",
        "requested_intent": "act", "resolved_intent": "act", "reply_to_id": None,
        "artifact_ids": [], "error": None,
        "outcome": {"kind": "command_queued", "run_id": parent["id"], "command_id": "cmd_1", "position": 1},
    })
    write_json_atomic(assistant_chats._chat_path(ws, chat["id"]), record)
    # The runner relaunches a drained command as a child run carrying the
    # queueing message's id.
    child = store.new_command_run(ws, "auto", {
        "source": "follow_up", "text": text, "chat_id": chat["id"], "source_message_id": "msg_queued",
    })
    child["status"] = "executing"
    store.save_run(ws, child)

    loaded = assistant_chats.get_chat(ws, chat["id"])
    cards = [item for item in loaded["transcript"] if item.get("type") == "run"]
    # One card per run, and the queued message resolves to the run it became —
    # not a "Didn't run" card contradicted by a live run further down.
    assert [card["run_id"] for card in cards] == [parent["id"], child["id"]]
    assert not any(":command:" in card["id"] for card in cards)
    assert cards[1]["source_message_id"] == "msg_queued"


def test_status_labels_cover_every_status_the_store_can_produce():
    known = set(store.TERMINAL_STATUSES) | set(store.ACTIVE_STATUSES) | set(store.RESUMABLE_STATUSES)
    assert known <= set(assistant_chats._STATUS_LABELS)


# --------------------------------------------------------------------------- #
# Stage lines
# --------------------------------------------------------------------------- #
def test_stage_lines_report_counts_and_who_is_waiting():
    stage = _stage("review_required", [
        _unit(),
        _unit("awaiting_confirmation", "document_has_no_extractable_text", unit_id="u2"),
    ])
    assert narration.stage_started(stage) == "Document analysis — 2 items to work through."
    settled = narration.stage_settled(stage)
    assert "1 of 2 done" in settled
    assert "1 waiting on you" in settled
    assert "12s" in settled


# --------------------------------------------------------------------------- #
# Next steps
# --------------------------------------------------------------------------- #
def test_next_steps_come_from_readiness_and_preserve_declared_outcomes():
    from app.agent import routing

    state = {
        "documents.analysis_generated": {"state": "missing", "reasons": ["1 document(s) have no generated analysis"]},
        "planning.apm_ready": {"state": "satisfied"},
        "planning.rcm_ready": {"state": "missing", "reasons": []},
        "report.working_draft": {"state": "blocked"},
    }
    steps = narration.next_steps(None, state)
    assert [item["capability"] for item in steps] == [
        "documents.analysis_generated",
        "planning.rcm_ready",
    ]
    assert steps[0]["reason"] == "1 document(s) have no generated analysis"
    # The clicked suggestion carries its declared outcome. Routing must honor
    # that structured value rather than attempting to infer it from display
    # wording, which can legitimately vary over time.
    all_missing = {key: {"state": "missing"} for key in narration._NEXT_STEPS}
    all_steps = narration.next_steps(None, all_missing, limit=len(narration._NEXT_STEPS))
    assert len(all_steps) == len(narration._NEXT_STEPS)
    for step in all_steps:
        assert step["requested_outcomes"] == [step["capability"]]
        resolved = routing.classify_command(
            {
                "source": "tab_button",
                "text": step["command"],
                "requested_outcomes": step["requested_outcomes"],
            }
        )
        assert resolved["decided_by"] == "explicit_outcomes"
        assert resolved["requested_outcomes"] == step["requested_outcomes"]


def test_next_steps_never_break_a_chat_load():
    class Exploding:
        pass

    assert narration.next_steps(Exploding(), None) == []


def test_next_steps_offer_document_tests_when_definitions_block_execution():
    state = {
        "doc_tests.definitions_ready": {"state": "review_required"},
        "doc_tests.executed": {
            "state": "blocked",
            "pending": 9,
            "blocking_on": ["doc_tests.definitions_ready"],
            "reasons": ["9 Document Test(s) have unchecked items"],
        },
    }

    assert narration.next_steps(None, state) == [{
        "capability": "doc_tests.executed",
        "requested_outcomes": ["doc_tests.executed"],
        "label": "Run document tests",
        "command": "Run the outstanding Document Tests.",
        "reason": "9 Document Test(s) have unchecked items",
    }]


def test_guided_workflows_hide_completed_areas():
    state = {
        "planning.apm_ready": {"state": "satisfied"},
        "planning.rcm_ready": {"state": "satisfied"},
        "tests.specified": {"state": "satisfied"},
        "doc_tests.executed": {"state": "missing"},
        "analysis.executed": {"state": "missing"},
        "findings.drafted": {"state": "missing"},
        "working_papers.generated": {"state": "missing"},
        "dashboard.curated": {"state": "missing"},
        "report.working_draft": {"state": "missing"},
        "audit.verified": {"state": "missing"},
    }

    commands = [item["command"] for item in narration.guided_workflows(state)]

    assert "plan" not in commands
    assert {"full_audit", "analyze_data", "run_document_tests", "generate_report"} <= set(commands)


def test_plan_sentence_reads_as_prose():
    text = narration.plan_sentence(
        ["Document analysis", "Planning context", "Audit planning memorandum"],
        ["Document text"],
        added_prerequisites=True,
    )
    assert text.startswith("I'll work through document analysis, planning context, then audit planning memorandum.")
    assert "Document text is already done" in text
    assert not re.search(r"[a-z_]+\.[a-z_]+", text)

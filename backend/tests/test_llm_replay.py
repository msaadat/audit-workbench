"""Replay mode: model calls answered from the workspace's own recordings."""

import json

import pytest

from app import assistant_settings, debug_service, debug_store, engagement, llm, llm_replay, workspaces


class FakeResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, content: str, finish_reason: str = "stop"):
        self.payload = {
            "choices": [{
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "reasoning": "private"},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False
    def read(self): return json.dumps(self.payload).encode()


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    assistant_settings.save({"provider": "groq", "model": "test-model"})
    monkeypatch.delenv(llm_replay.ENV_VAR, raising=False)
    llm_replay._reset_for_tests()
    yield
    llm_replay._reset_for_tests()


def _messages(user: str) -> list[dict]:
    return [{"role": "system", "content": "[agent:test] Draft."}, {"role": "user", "content": user}]


def _record(ws, monkeypatch, calls: list[tuple[str, str, str]], *, run_id: str,
            stage: str = "agent:test", refs=("finding:F-1",)) -> list[str]:
    """Make live calls against a scripted provider; return their call ids."""
    monkeypatch.setenv("GROQ_API_KEY", "live-key")
    for user, content, finish in calls:
        monkeypatch.setattr(
            llm.urllib.request, "urlopen",
            lambda request, timeout, c=content, f=finish: FakeResponse(c, f),
        )
        with debug_store.trace_context(
            workspace_id=ws.id, workspace_root=str(ws.root), run_id=run_id,
            stage=stage, artifact_refs=list(refs),
        ):
            llm.chat(_messages(user), profile="agent")
    items = debug_service.list_calls(ws)["items"]
    return [item["id"] for item in sorted(items, key=lambda item: item["started_at"])]


def _replay(ws, monkeypatch, user: str, *, run_id: str = "demo-run",
            stage: str = "agent:test", refs=("finding:F-1",)) -> dict:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv(llm_replay.ENV_VAR, "on")

    def no_network(*_args, **_kwargs):
        raise AssertionError("replay opened a connection")

    monkeypatch.setattr(llm.urllib.request, "urlopen", no_network)
    with debug_store.trace_context(
        workspace_id=ws.id, workspace_root=str(ws.root), run_id=run_id,
        stage=stage, artifact_refs=list(refs),
    ):
        return llm.chat(_messages(user), profile="agent")


def _replayed_records(ws) -> list[dict]:
    records = [debug_service.get_call(ws, item["id"]) for item in debug_service.list_calls(ws)["items"]]
    return sorted(
        (record for record in records if record.get("replay")),
        key=lambda record: record["started_at"],
    )


def test_replay_answers_a_recorded_request_without_the_network(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    [original] = _record(ws, monkeypatch, [("draft F-1", "# Finding one", "stop")], run_id="rec")

    first = _replay(ws, monkeypatch, "draft F-1")
    second = _replay(ws, monkeypatch, "draft F-1")

    assert first["content"] == second["content"] == "# Finding one"
    assert "reasoning" not in first
    assert first["usage"]["total_tokens"] == 12
    assert first["finish_reason"] == "stop"
    replayed = _replayed_records(ws)
    assert len(replayed) == 2
    # A replayed call is recorded, and never answers a later replay itself.
    assert [record["replay"]["call_id"] for record in replayed] == [original, original]
    assert {record["replay"]["match"] for record in replayed} == {"exact"}
    assert all(record["status"] == "completed" for record in replayed)
    # The console's call list must not show a replay as a live provider call.
    assert {record["provider"] for record in replayed} == {"replayed · groq"}
    listed = {item["id"]: item for item in debug_service.list_calls(ws)["items"]}
    assert all(listed[record["id"]]["provider"] == "replayed · groq" for record in replayed)


def test_replay_serves_the_kept_answer_not_the_rejected_one(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    rejected, kept = _record(ws, monkeypatch, [
        ("draft F-1", "no headings", "stop"),
        ("draft F-1\n\nYour previous response could not be used: the Condition section "
         "is missing. Return the whole finding again.", "# Finding one\n## Condition", "stop"),
    ], run_id="rec")

    message = _replay(ws, monkeypatch, "draft F-1")

    assert message["content"] == "# Finding one\n## Condition"
    [record] = _replayed_records(ws)
    assert record["replay"] == {
        "call_id": kept, "match": "exact", "skipped": [rejected], "translated": 0,
    }


def test_replay_skips_empty_and_cut_off_answers(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    _record(ws, monkeypatch, [
        ("draft F-1", "# Finding", "stop"),
        ("draft F-1", "# Finding one, cut", "length"),
        ("draft F-1", "", "stop"),
    ], run_id="rec")

    assert _replay(ws, monkeypatch, "draft F-1")["content"] == "# Finding"


def test_same_step_serves_each_question_once_in_recorded_order(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    _record(ws, monkeypatch, [
        ("old prompt, question one", "answer one", "stop"),
        ("old prompt, question two", "rejected two", "stop"),
        ("old prompt, question two\n\nYour previous response could not be used: no.",
         "answer two", "stop"),
    ], run_id="rec")

    # The prompts moved since the recording; the step and its refs did not.
    first = _replay(ws, monkeypatch, "new prompt, question one")
    second = _replay(ws, monkeypatch, "new prompt, question two")

    assert [first["content"], second["content"]] == ["answer one", "answer two"]
    assert {record["replay"]["match"] for record in _replayed_records(ws)} == {"same_step"}


def test_same_step_needs_the_same_refs(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    _record(ws, monkeypatch, [("draft F-1", "# Finding one", "stop")], run_id="rec")

    with pytest.raises(llm.LLMError, match="no recorded model response for agent:test"):
        _replay(ws, monkeypatch, "draft F-2", refs=("finding:F-2",))


def test_replay_refuses_what_was_not_recorded(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    _record(ws, monkeypatch, [("draft F-1", "# Finding one", "stop")], run_id="rec")

    with pytest.raises(llm.LLMError, match="Only steps performed once with a live model"):
        _replay(ws, monkeypatch, "a question nobody asked", refs=())
    failed = [
        debug_service.get_call(ws, item["id"])
        for item in debug_service.list_calls(ws)["items"]
        if item["status"] == "failed"
    ]
    assert len(failed) == 1


def test_replay_reports_the_agent_configured_and_local_without_a_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert llm.agent_status()["configured"] is False
    assert llm.agent_status()["replay"] is False

    monkeypatch.setenv(llm_replay.ENV_VAR, "on")

    assert llm.agent_status()["configured"] is True
    assert llm.agent_status()["replay"] is True
    assert llm.status()["configured"] is True
    destination = engagement.destination()
    assert destination["local"] is True
    assert "Nothing leaves this machine" in destination["summary"]


# ---------------------------------------------------------------- identifiers
RECORDED_PROMPT = (
    "Draft the finding for observation OBS-019DFE5622 on row RCM-15F60B, "
    "read from document 3ec281275e (sha1 6639c7dae9663d98448081db6d12dc60efbedb9b)."
)
REBUILT_PROMPT = (
    "Draft the finding for observation OBS-7C1E0A9B33 on row RCM-A0B1C2, "
    "read from document 9f8e7d6c5b (sha1 0123456789abcdef0123456789abcdef01234567)."
)
RECORDED_ANSWER = "# Over limit\n## Condition\nRow RCM-15F60B flagged OBS-019DFE5622 twice."


def test_canonical_reads_each_identifier_as_its_first_appearance():
    text, tokens = llm_replay.canonical("RCM-15F60B then OBS-019DFE5622 then RCM-15F60B, 2024-06-04")
    assert text == "⟨0⟩ then ⟨1⟩ then ⟨0⟩, 2024-06-04"
    assert tokens == ["RCM-15F60B", "OBS-019DFE5622"]


def test_a_rebuilt_engagement_is_answered_with_its_own_identifiers(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    [original] = _record(
        ws, monkeypatch, [(RECORDED_PROMPT, RECORDED_ANSWER, "stop")], run_id="rec",
        refs=("observation:OBS-019DFE5622",),
    )

    message = _replay(ws, monkeypatch, REBUILT_PROMPT, refs=("observation:OBS-7C1E0A9B33",))

    assert message["content"] == (
        "# Over limit\n## Condition\nRow RCM-A0B1C2 flagged OBS-7C1E0A9B33 twice."
    )
    [record] = _replayed_records(ws)
    assert record["replay"]["call_id"] == original
    assert record["replay"]["match"] == "translated"
    assert record["replay"]["translated"] == 2


def test_a_learned_pairing_carries_to_a_later_step(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    _record(ws, monkeypatch, [(RECORDED_PROMPT, RECORDED_ANSWER, "stop")], run_id="rec",
            refs=("observation:OBS-019DFE5622",))
    _record(ws, monkeypatch, [("Old wording of the report section", "Cites RCM-15F60B.", "stop")],
            run_id="rec", stage="agent:report", refs=("rcm:RCM-15F60B",))

    _replay(ws, monkeypatch, REBUILT_PROMPT, refs=("observation:OBS-7C1E0A9B33",))
    later = _replay(ws, monkeypatch, "New wording of the report section",
                    stage="agent:report", refs=("rcm:RCM-A0B1C2",))

    assert later["content"] == "Cites RCM-A0B1C2."
    assert _replayed_records(ws)[-1]["replay"]["match"] == "same_step"


def test_identifiers_alone_never_make_a_different_question_the_same(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    _record(ws, monkeypatch, [(RECORDED_PROMPT, RECORDED_ANSWER, "stop")], run_id="rec",
            refs=("observation:OBS-019DFE5622",))

    with pytest.raises(llm.LLMError, match="no recorded model response"):
        _replay(ws, monkeypatch, REBUILT_PROMPT.replace("Draft the finding", "Retire the finding"),
                refs=())


def test_a_named_source_is_read_and_never_written(workspace_with_data, monkeypatch):
    recorded = workspace_with_data
    _record(recorded, monkeypatch, [(RECORDED_PROMPT, RECORDED_ANSWER, "stop")], run_id="rec",
            refs=("observation:OBS-019DFE5622",))
    source = recorded.root / "telemetry.db"
    calls_before = len(debug_service.list_calls(recorded)["items"])
    rebuilt = workspaces.create_workspace("Rebuilt Engagement")
    monkeypatch.setenv(llm_replay.SOURCE_ENV_VAR, str(source))

    message = _replay(rebuilt, monkeypatch, REBUILT_PROMPT, refs=("observation:OBS-7C1E0A9B33",))

    assert "RCM-A0B1C2" in message["content"]
    assert len(debug_service.list_calls(recorded)["items"]) == calls_before
    [record] = _replayed_records(rebuilt)
    assert record["replay"]["match"] == "translated"


def test_a_missing_source_fails_plainly(workspace_with_data, monkeypatch, tmp_path):
    monkeypatch.setenv(llm_replay.SOURCE_ENV_VAR, str(tmp_path / "absent" / "telemetry.db"))
    with pytest.raises(llm.LLMError, match="names no telemetry database"):
        _replay(workspace_with_data, monkeypatch, "anything")


def test_the_workspace_s_own_identity_does_not_block_a_rebuild(workspace_with_data, monkeypatch):
    """The assistant's manifest names the workspace, its id and its revision;
    a rebuild is named and saved differently, and still asks the same thing.
    A department that shares the workspace's name stays data."""
    ws = workspace_with_data
    recorded = (
        'Workspace manifest:\n{\n "workspace": {\n  "id": "ws_1a1ece863b2e70942eff5db",\n'
        '  "name": "Procurement",\n  "revision": 129\n }\n}\n'
        '{"engagement": "Procurement", "department": "Procurement"}'
    )
    rebuilt = (
        recorded.replace("ws_1a1ece863b2e70942eff5db", "ws_1a1ed5c4062eb57963a0db4")
        .replace('"name": "Procurement"', '"name": "Procurement2"')
        .replace('"engagement": "Procurement"', '"engagement": "Procurement2"')
        .replace("129", "57")
    )
    _record(ws, monkeypatch, [(recorded, "Start the RCM revision.", "stop")], run_id="rec", refs=())

    assert _replay(ws, monkeypatch, rebuilt, refs=())["content"] == "Start the RCM revision."
    with pytest.raises(llm.LLMError, match="no recorded model response"):
        _replay(ws, monkeypatch, rebuilt.replace('"department": "Procurement"', '"department": "Finance"'),
                refs=())


def test_rows_listed_in_id_order_pair_by_what_they_say(workspace_with_data, monkeypatch):
    """A prompt that lists records sorted by their (random) ids lists them in
    another order once the ids are new; each must still pair with the record
    that says the same thing, so the recorded decision lands on the right row."""
    ws = workspace_with_data

    def prompt(rows):
        return json.dumps({"instruction": "shorten the matrix", "ROWS": [
            {"id": row_id, "risk": risk} for row_id, risk in sorted(rows)
        ]})

    recorded = prompt([("RCM-111111", "Split requisitions"), ("RCM-999999", "Duplicate payment")])
    answer = '{"decisions": [{"id": "RCM-111111", "decision": "keep"}, {"id": "RCM-999999", "decision": "retire"}]}'
    _record(ws, monkeypatch, [(recorded, answer, "stop")], run_id="rec", refs=())
    # Today the duplicate-payment row has the smaller id, so it is listed first.
    rebuilt = prompt([("RCM-AAAAAA", "Duplicate payment"), ("RCM-BBBBBB", "Split requisitions")])

    message = _replay(ws, monkeypatch, rebuilt, refs=())

    decisions = {item["id"]: item["decision"] for item in json.loads(message["content"])["decisions"]}
    assert decisions == {"RCM-BBBBBB": "keep", "RCM-AAAAAA": "retire"}
    assert _replayed_records(ws)[-1]["replay"]["match"] == "reordered"


def test_one_question_asked_for_several_rows_answers_each_row_its_own(workspace_with_data, monkeypatch):
    """A run that asks the same question over three matrix rows recorded
    three answers; each rebuilt call gets the answer recorded for its row,
    found through the row pairings learned earlier in the rebuild."""
    ws = workspace_with_data
    rows = '{"rows": [{"id": "RCM-111111", "risk": "Split"}, {"id": "RCM-999999", "risk": "Duplicate"}]}'
    _record(ws, monkeypatch, [(rows, "noted", "stop")], run_id="rec", refs=())
    vouch = "Vouch item ITEM-{item} against goods receipt GRN2024004."
    _record(ws, monkeypatch, [(vouch.format(item="A" * 24), "verdict for split", "stop")],
            run_id="rec", stage="agent:cycle_vouch", refs=("rcm:RCM-111111",))
    _record(ws, monkeypatch, [(vouch.format(item="B" * 24), "verdict for duplicate", "stop")],
            run_id="rec", stage="agent:cycle_vouch", refs=("rcm:RCM-999999",))

    _replay(ws, monkeypatch, rows.replace("RCM-111111", "RCM-CCCCCC").replace("RCM-999999", "RCM-DDDDDD"), refs=())
    duplicate = _replay(ws, monkeypatch, vouch.format(item="C" * 24), stage="agent:cycle_vouch", refs=("rcm:RCM-DDDDDD",))
    split = _replay(ws, monkeypatch, vouch.format(item="D" * 24), stage="agent:cycle_vouch", refs=("rcm:RCM-CCCCCC",))

    assert duplicate["content"] == "verdict for duplicate"
    assert split["content"] == "verdict for split"


def _record_messages(ws, monkeypatch, conversations, *, run_id="rec", stage="agent:test", refs=("finding:F-1",)):
    """Live calls whose requests are whole message lists, for repair shapes."""
    monkeypatch.setenv("GROQ_API_KEY", "live-key")
    for messages, content in conversations:
        monkeypatch.setattr(llm.urllib.request, "urlopen",
                            lambda request, timeout, c=content: FakeResponse(c))
        with debug_store.trace_context(workspace_id=ws.id, workspace_root=str(ws.root),
                                       run_id=run_id, stage=stage, artifact_refs=list(refs)):
            llm.chat(messages, profile="agent")


def test_a_repair_sent_as_two_more_turns_is_skipped_too(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    question = _messages("generate the tests for RCM-1")
    _record_messages(ws, monkeypatch, [
        (question, "not json"),
        (question + [{"role": "assistant", "content": "not json"},
                     {"role": "user", "content": "The preceding JSON response could not be used: no tests array."}],
         '{"tests": []}'),
    ])

    assert _replay(ws, monkeypatch, "generate the tests for RCM-1")["content"] == '{"tests": []}'
    assert len(_replayed_records(ws)[-1]["replay"]["skipped"]) == 1


def test_a_repair_worded_as_repair_the_prior_response_is_skipped(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    _record(ws, monkeypatch, [
        ("place analysis A-1", '{"code": "import polars as pl"}', "stop"),
        ("place analysis A-1\nRepair the prior response: Imports are not allowed.", '{"code": "df"}', "stop"),
    ], run_id="rec")

    assert _replay(ws, monkeypatch, "place analysis A-1")["content"] == '{"code": "df"}'


def test_a_tool_conversation_is_not_mistaken_for_a_repair(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    first = _messages("shorten the matrix")
    second = first + [{"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function",
                       "function": {"name": "start_command", "arguments": "{}"}}]},
                      {"role": "tool", "tool_call_id": "t1", "content": "started"}]
    _record_messages(ws, monkeypatch, [(first, "calling a tool"), (second, "Started the revision.")], refs=())

    assert _replay(ws, monkeypatch, "shorten the matrix", refs=())["content"] == "calling a tool"


def test_replay_matches_non_streamed_recording_when_caller_streams_tools(workspace_with_data, monkeypatch):
    ws = workspace_with_data
    tools = [{
        "type": "function",
        "function": {"name": "list_tables", "description": "List tables", "parameters": {"type": "object", "properties": {}}},
    }]
    monkeypatch.setenv("GROQ_API_KEY", "live-key")
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda request, timeout: FakeResponse("There is 1 table."),
    )
    with debug_store.trace_context(
        workspace_id=ws.id, workspace_root=str(ws.root), chat_id="chat_1",
        stage="assistant.tool_loop",
    ):
        llm.chat(_messages("list the tables"), tools=tools, profile="assistant")

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv(llm_replay.ENV_VAR, "on")
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("network")))
    deltas: list[str] = []
    with debug_store.trace_context(
        workspace_id=ws.id, workspace_root=str(ws.root), chat_id="chat_1",
        stage="assistant.tool_loop",
    ):
        replayed = llm.chat(
            _messages("list the tables"),
            tools=tools,
            profile="assistant",
            on_delta=deltas.append,
            stream_tools=True,
        )
    assert replayed["content"] == "There is 1 table."
    assert deltas == ["There is 1 table."]


def test_read_stream_reassembles_tool_calls_without_index_or_repeated_name_and_honors_cancel():
    import threading

    chunks = [
        b'data: {"choices":[{"delta":{"tool_calls":[{"id":"c1","type":"function","function":{"name":"list_tables","arguments":"{\\"a\\""}}]}}]}\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"id":"c1","function":{"name":"list_tables","arguments":":1}"}}]}}]}\n',
        b'data: {"choices":[{"delta":{"tool_calls":[{"id":"c2","type":"function","function":{"name":"table_profile","arguments":"{\\"t\\":\\"x\\"}"}}]}}]}\n',
        b"data: [DONE]\n",
    ]
    payload, _ = llm._read_stream(iter(chunks), lambda _s: None)
    calls = payload["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 2
    assert calls[0]["id"] == "c1"
    assert calls[0]["function"] == {"name": "list_tables", "arguments": '{"a":1}'}
    assert calls[1]["id"] == "c2"
    assert calls[1]["function"] == {"name": "table_profile", "arguments": '{"t":"x"}'}

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(llm.LLMCancelledError):
        llm._read_stream(iter(chunks), lambda _s: None, cancel_event=cancel)

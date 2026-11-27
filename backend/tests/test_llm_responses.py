"""Tool calls over the Responses API (``LLM_TOOLS_API=responses``).

Callers keep sending chat-shaped requests and reading chat-shaped replies; the
transport rewrites both at the wire. These tests pin both rewrites, the
reasoning round trip a tool conversation depends on, and that nothing changes
for a request the switch does not cover.
"""

import io
import json
import urllib.error

import pytest

from app import assistant_settings, llm

TOOL = {
    "type": "function",
    "function": {
        "name": "submit",
        "description": "Submit the answer.",
        "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
    },
}
FORCED = {"type": "function", "function": {"name": "submit"}}


class _Reply:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _transport(monkeypatch, replies: list) -> list[dict]:
    """Answer each request with the next reply; return what was sent."""
    sent: list[dict] = []

    def urlopen(request, timeout):
        sent.append({"url": request.full_url, "body": json.loads(request.data.decode())})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return _Reply(reply)

    assistant_settings.save({"provider": "openrouter", "model": "vendor/model"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    return sent


def _responses_reply(*output, status="completed", incomplete=None) -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "status": status,
        "error": None,
        "incomplete_details": incomplete,
        "model": "vendor/model",
        "output": list(output),
        "usage": {
            "input_tokens": 120,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 12},
            "total_tokens": 150,
        },
    }


REASONING_ITEM = {
    "id": "rs_1",
    "type": "reasoning",
    "summary": [],
    "content": [{"type": "reasoning_text", "text": "The user wants submit."}],
    "encrypted_content": "opaque",
}
CALL_ITEM = {
    "id": "fc_1",
    "type": "function_call",
    "call_id": "call_1",
    "name": "submit",
    "arguments": '{"x":"ok"}',
}
CHAT_REPLY = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}


def test_tool_requests_stay_on_chat_completions_by_default(monkeypatch):
    sent = _transport(monkeypatch, [CHAT_REPLY])

    llm.chat([{"role": "user", "content": "hi"}], tools=[TOOL], tool_choice=FORCED)

    assert sent[0]["url"].endswith("/chat/completions")
    assert sent[0]["body"]["tools"] == [TOOL]
    assert sent[0]["body"]["tool_choice"] == FORCED


def test_requests_without_tools_stay_on_chat_completions_when_switched(monkeypatch):
    monkeypatch.setenv("LLM_TOOLS_API", "responses")
    sent = _transport(monkeypatch, [CHAT_REPLY])

    llm.chat([{"role": "user", "content": "hi"}], response_format={"type": "json_object"})

    assert sent[0]["url"].endswith("/chat/completions")
    assert "messages" in sent[0]["body"]


def test_a_tool_request_is_rewritten_for_responses(monkeypatch):
    monkeypatch.setenv("LLM_TOOLS_API", "responses")
    sent = _transport(monkeypatch, [_responses_reply(REASONING_ITEM, CALL_ITEM)])

    llm.chat(
        [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "Submit ok."},
        ],
        tools=[TOOL],
        tool_choice=FORCED,
        reasoning="medium",
    )

    assert sent[0]["url"].endswith("/responses")
    body = sent[0]["body"]
    assert "messages" not in body and "max_tokens" not in body
    assert body["input"] == [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": [{"type": "input_text", "text": "Submit ok."}]},
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "name": "submit",
            "parameters": TOOL["function"]["parameters"],
            # Chat never enforced strict schemas; Responses would by default.
            "strict": False,
            "description": "Submit the answer.",
        }
    ]
    assert body["tool_choice"] == {"type": "function", "name": "submit"}
    assert body["max_output_tokens"] == llm.MAX_OUTPUT_TOKENS
    assert body["reasoning"] == {"effort": "medium"}
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]


def test_a_responses_reply_comes_back_chat_shaped(monkeypatch):
    monkeypatch.setenv("LLM_TOOLS_API", "responses")
    _transport(monkeypatch, [_responses_reply(REASONING_ITEM, CALL_ITEM)])

    message = llm.chat([{"role": "user", "content": "Submit ok."}], tools=[TOOL], tool_choice=FORCED)

    call = message["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["function"] == {"name": "submit", "arguments": '{"x":"ok"}'}
    # Carried on the call so the next turn can send it back.
    assert call[llm.RESPONSES_REASONING_KEY] == [REASONING_ITEM]
    assert message["finish_reason"] == "tool_calls"
    assert message["content"] is None
    # The trace is a diagnostic; it never reaches the caller.
    assert "reasoning" not in message
    assert message["usage"]["prompt_tokens"] == 120
    assert message["usage"]["completion_tokens"] == 30
    assert message["usage"]["completion_tokens_details"]["reasoning_tokens"] == 12


def test_a_text_answer_and_a_truncated_one(monkeypatch):
    monkeypatch.setenv("LLM_TOOLS_API", "responses")
    text = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "All done."}],
    }
    _transport(
        monkeypatch,
        [
            _responses_reply(text),
            _responses_reply(status="incomplete", incomplete={"reason": "max_output_tokens"}),
        ],
    )

    answered = llm.chat([{"role": "user", "content": "hi"}], tools=[TOOL])
    truncated = llm.chat([{"role": "user", "content": "hi"}], tools=[TOOL])

    assert answered["content"] == "All done."
    assert answered["finish_reason"] == "stop"
    assert "tool_calls" not in answered
    assert truncated["finish_reason"] == "length"
    assert truncated["content"] == ""


def test_a_tool_conversation_is_sent_back_in_order(monkeypatch):
    """The loop's next turn: reasoning, then the call, then its result."""

    monkeypatch.setenv("LLM_TOOLS_API", "responses")
    sent = _transport(monkeypatch, [_responses_reply(REASONING_ITEM, CALL_ITEM), _responses_reply()])
    first = llm.chat([{"role": "user", "content": "Submit ok."}], tools=[TOOL])
    # What the loop and the chat persist: content and tool_calls, verbatim.
    conversation = [
        {"role": "user", "content": "Submit ok."},
        {"role": "assistant", "content": "", "tool_calls": first["tool_calls"]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"accepted": true}'},
    ]

    llm.chat(conversation, tools=[TOOL])

    assert sent[1]["body"]["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "Submit ok."}]},
        REASONING_ITEM,
        {"type": "function_call", "call_id": "call_1", "name": "submit", "arguments": '{"x":"ok"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": '{"accepted": true}'},
    ]


def test_carried_reasoning_never_reaches_chat_completions(monkeypatch):
    """A conversation begun on /responses and continued on chat/completions."""

    sent = _transport(monkeypatch, [CHAT_REPLY])
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "submit", "arguments": "{}"},
        llm.RESPONSES_REASONING_KEY: [REASONING_ITEM],
    }
    conversation = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
    ]

    llm.chat(conversation, tools=[TOOL])

    sent_call = sent[0]["body"]["messages"][1]["tool_calls"][0]
    assert llm.RESPONSES_REASONING_KEY not in sent_call
    assert sent_call["function"]["name"] == "submit"
    # The caller's own conversation is left as it was.
    assert llm.RESPONSES_REASONING_KEY in call


def test_images_are_sent_as_input_images(monkeypatch):
    monkeypatch.setenv("LLM_TOOLS_API", "responses")
    sent = _transport(monkeypatch, [_responses_reply(CALL_ITEM)])
    image = llm.image_part(b"\x89PNG", "image/png")

    llm.chat(
        [{"role": "user", "content": [{"type": "text", "text": "Read this."}, image]}],
        tools=[TOOL],
    )

    assert sent[0]["body"]["input"][0]["content"] == [
        {"type": "input_text", "text": "Read this."},
        {"type": "input_image", "image_url": image["image_url"]["url"]},
    ]


def test_a_model_refusing_encrypted_reasoning_is_asked_again_without_it(monkeypatch):
    monkeypatch.setenv("LLM_TOOLS_API", "responses")
    refusal = urllib.error.HTTPError(
        "https://openrouter.ai/api/v1/responses",
        400,
        "Bad Request",
        {},
        io.BytesIO(json.dumps({"error": {"message": "Encrypted content is not supported with this model."}}).encode()),
    )
    sent = _transport(monkeypatch, [refusal, _responses_reply(CALL_ITEM)])

    message = llm.chat([{"role": "user", "content": "hi"}], tools=[TOOL])

    assert "include" in sent[0]["body"]
    assert "include" not in sent[1]["body"]
    assert message["tool_calls"][0]["id"] == "call_1"


def test_a_failed_response_is_an_error(monkeypatch):
    monkeypatch.setenv("LLM_TOOLS_API", "responses")
    failed = {**_responses_reply(), "status": "failed", "error": {"code": "invalid_prompt", "message": "Prompt refused."}}
    _transport(monkeypatch, [failed])

    with pytest.raises(llm.LLMError, match="Prompt refused"):
        llm.chat([{"role": "user", "content": "hi"}], tools=[TOOL])


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://localhost:1234/v1", code, "error", {}, io.BytesIO(json.dumps(body).encode())
    )


def _lmstudio_transport(monkeypatch, replies: list) -> None:
    sent = _transport(monkeypatch, replies)
    assistant_settings.save({"provider": "lmstudio", "model": "local-model"})
    del sent


def test_a_missing_responses_route_says_so(monkeypatch):
    monkeypatch.setenv("LLM_TOOLS_API", "responses")
    _lmstudio_transport(monkeypatch, [_http_error(404, {"detail": "Not Found"})])

    with pytest.raises(llm.LLMError) as raised:
        llm.chat([{"role": "user", "content": "hi"}], tools=[TOOL])

    assert "does not serve /responses" in str(raised.value)
    assert "LLM_TOOLS_API=chat" in str(raised.value)
    assert "was not accepted" not in str(raised.value)


def test_a_refused_parameter_is_not_reported_as_a_refused_model(monkeypatch):
    refusal = {"error": {"message": "Function tools with reasoning_effort are not supported for this model."}}
    _lmstudio_transport(monkeypatch, [_http_error(400, refusal)])

    with pytest.raises(llm.LLMError) as raised:
        llm.chat([{"role": "user", "content": "hi"}], tools=[TOOL])

    assert "reasoning_effort" in str(raised.value)
    assert "was not accepted" not in str(raised.value)


def test_a_model_the_server_does_not_have_still_gets_the_lm_studio_hint(monkeypatch):
    missing = {"error": {"message": "Model not found: local-model"}}
    _lmstudio_transport(monkeypatch, [_http_error(404, missing)])

    with pytest.raises(llm.LLMError, match="'local-model' was not accepted"):
        llm.chat([{"role": "user", "content": "hi"}])


def test_an_unknown_setting_is_refused(monkeypatch):
    monkeypatch.setenv("LLM_TOOLS_API", "assistants")
    _transport(monkeypatch, [])

    with pytest.raises(llm.LLMError, match="LLM_TOOLS_API"):
        llm.chat([{"role": "user", "content": "hi"}], tools=[TOOL])


# ------------------------------------------------------- agent & vision profiles
def test_agent_and_vision_profile_resolution(monkeypatch):
    import base64
    from app import assistant_settings

    assistant_settings.save({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    for var in ("AGENT_PROVIDER", "AGENT_BACKEND", "AGENT_MODEL"):
        monkeypatch.delenv(var, raising=False)
    settings = llm._settings("agent")
    assert (settings.backend, settings.model) == ("groq", "llama-3.3-70b-versatile")

    monkeypatch.setenv("AGENT_MODEL", "llama-3.1-8b-instant")
    assert llm._settings("agent").model == "llama-3.1-8b-instant"
    assert llm._settings().model == "llama-3.3-70b-versatile"

    monkeypatch.setenv("AGENT_PROVIDER", "mistral")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    assert llm._settings("agent").model == assistant_settings.PROVIDERS["mistral"]["default_model"]

    monkeypatch.setenv("AGENT_MODEL", "mistral-large-latest")
    monkeypatch.setenv("MISTRAL_API_KEY", "key")
    status = llm.agent_status()
    assert status["configured"] is True
    assert status["provider"] == "mistral"
    assert status["model"] == "mistral-large-latest"

    for var in ("AGENT_VISION_PROVIDER", "AGENT_VISION_BACKEND", "AGENT_VISION_MODEL", "AGENT_VISION_CAPABILITIES"):
        monkeypatch.delenv(var, raising=False)
    assistant_settings.save({"provider": "mistral", "model": "mistral-large-latest", "vision_profile": None})
    assert llm._settings("vision").model == "pixtral-large-latest"
    assert llm.agent_status()["vision_configured"] is True
    part = llm.image_part(b"image", "image/png")
    assert base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) == b"image"

    monkeypatch.setenv("AGENT_PROVIDER", "nonsense")
    bad = llm.agent_status()
    assert bad["configured"] is False and "nonsense" in bad["error"]


def test_agent_and_vision_profiles_honor_base_url_override(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "lmstudio")
    monkeypatch.setenv("AGENT_MODEL", "local-text")
    monkeypatch.setenv("AGENT_VISION_PROVIDER", "lmstudio")
    monkeypatch.setenv("AGENT_VISION_MODEL", "local-vision")
    monkeypatch.setenv("AGENT_VISION_CAPABILITIES", "vision")
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    default_hashes = {
        name: llm.resolve_model_profile(name).profile_hash for name in ("agent", "vision")
    }

    endpoint = "http://gpu01.internal:1234/v1"
    monkeypatch.setenv("LMSTUDIO_BASE_URL", endpoint)
    snapshot = llm.model_profile_snapshot()
    assert snapshot["text"]["base_url"] == endpoint
    assert snapshot["vision"]["base_url"] == endpoint
    assert llm._settings("agent").base_url == endpoint
    assert llm._settings("vision").base_url == endpoint
    assert llm._settings(snapshot["text"]).base_url == endpoint
    assert snapshot["text"]["profile_hash"] == default_hashes["agent"]
    assert snapshot["vision"]["profile_hash"] == default_hashes["vision"]

    monkeypatch.setenv("LMSTUDIO_BASE_URL", "ftp://gpu01.internal/v1")
    invalid = llm.agent_status()
    assert invalid["configured"] is False and "LMSTUDIO_BASE_URL" in invalid["error"]

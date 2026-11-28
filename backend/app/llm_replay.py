"""Answer model calls from recordings instead of a provider.

A demonstration on a machine with no route to a model provider can still show
a step the model performs, provided that step was performed once before with
one. Every call :func:`llm.chat` makes is already recorded whole in a
workspace's telemetry database — the request it sent and the message it got
back — so with ``LLM_REPLAY=on`` the transport looks the request up there and
returns the recorded message without opening a connection.

Where the recordings come from
------------------------------

By default, the calling workspace's own telemetry. ``LLM_REPLAY_SOURCE``
names another one instead — a ``telemetry.db`` file, or a workspace folder
holding one — opened read-only. That is how an engagement is rebuilt from
nothing: a new, empty workspace takes the same steps a recorded one took, and
every answer comes from the recording, which replaying never writes to and so
never prunes.

Only answers the run *kept* are ever served
-------------------------------------------

- A call that failed at the transport never completed, and is not a recording.
- A completed call whose answer was empty, or cut off at the output limit, was
  retried with the same request; the retry is the recording.
- A completed call whose answer a validator rejected was followed by a repair
  turn: the same request with the rejection appended to its last message, or
  with the rejected answer and a correction added as two more turns. The
  original request is answered with the *last* answer of
  that chain, so a replayed unit passes validation on its first turn and the
  rejected draft is never shown, never revalidated, never repaired again.

How a request is matched
------------------------

1. ``exact`` — a recorded call whose request bytes hash to the same sha256.
2. ``translated`` — a recorded call whose request is the same once every
   identifier is read as a placeholder. The app mints its own identifiers —
   a document at import, a matrix row, a test, an observation, evidence, a
   finding — at random, and a rebuilt engagement mints different ones for
   the same things, so its prompts differ from the recording only there. Each
   identifier is replaced by its order of first appearance; two requests that
   then agree name the same things, position for position, and the pairing is
   the translation. The recorded answer is served with every identifier the
   pairing knows rewritten to today's, and the pairing is remembered for the
   steps that follow. Hashes, run ids and timestamps are read the same way,
   because they move with the identifiers they summarise; so are the
   workspace's own id, and its name and revision where code states them.
   Where that strict reading finds nothing, it is tried once more with every
   list of records sorted by its content (``reordered``): a prompt that lists
   rows by id lists them in another order once the ids are new.
3. ``same_step`` — the calls recorded for the same stage over the same
   artifact refs (translated through the remembered pairings), from the most
   recent run that made them, served in their recorded order across the
   successive calls of one run. This survives a prompt whose wording moved;
   the answer still has to pass today's validators, or the unit fails exactly
   as it would have live. A call with no artifact refs has no step identity,
   and only the first two matches answer it.

Nothing else answers. A request no match covers fails with a plain error:
replay repeats what was recorded, it never reaches the network and never
improvises. Where one request was recorded more than once, the newest
recording wins, so performing a step again with a live model is how a
recording is replaced. Replayed calls are recorded in the calling workspace,
marked with the call they were answered from, so the debug console shows what
was replayed; they are never matched themselves.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

from . import debug_store

ENV_VAR = "LLM_REPLAY"
SOURCE_ENV_VAR = "LLM_REPLAY_SOURCE"
_ON = {"1", "on", "true", "yes"}
#: How repair turns that append to the request tell the model its answer was
#: rejected: "...could not be used: <reason>" in most workers, "Repair the
#: prior response: <reason>" in analysis promotion, "...could not be parsed"
#: where the answer was not even JSON.
REPAIR_MARKERS = ("could not be used", "Repair the prior response", "could not be parsed")
#: What reads as an identifier. Broad on purpose: a token that is the same on
#: both sides — a constant, an amount, a date in the data — pairs with itself
#: and changes nothing, while one this misses makes two requests that name the
#: same things look different. Longer shapes come first so a run id is not
#: read as the hex run of digits at its head.
IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"\d{8}-\d{6}-[0-9a-f]{6}"
    r"|(?:chat|msg|call|cmd|ckpt)_[0-9a-z_]{6,40}"
    r"|ws_[0-9a-f]{16,32}"
    r"|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
    r"|[A-Z]{1,6}-[0-9A-Z]{4,40}"
    r"|[0-9a-f]{8,64}"
    r")(?![A-Za-z0-9_])"
)

#: Where code states the calling workspace's own name or revision in a
#: prompt: the assistant's workspace manifest, the report's workspace block,
#: and the report overview's ``engagement``. A rebuilt engagement is named
#: differently and saved a different number of times, so these read as
#: placeholders. Only these keyed values: a department or a document that
#: happens to share the workspace's name is data, and stays as it is.
WORKSPACE_IDENTITY = (
    re.compile(
        r'("workspace":\s*\{\s*"id":\s*"ws_[0-9a-f]+",\s*"name":\s*)"(?:[^"\\]|\\.)*"'
        r'(?:(,\s*"revision":\s*)\d+)?'
    ),
    re.compile(r'("engagement":\s*)"(?:[^"\\]|\\.)*"'),
)

_lock = threading.Lock()
_counters: dict[tuple, int] = {}
#: Per calling workspace: recorded identifier -> today's, and back.
_aliases: dict[str, dict[str, str]] = {}
_reverse: dict[str, dict[str, str]] = {}
#: Per recording source: (signature, canonical hash -> call ids newest first).
_indexes: dict[str, tuple[tuple, dict[str, list[str]]]] = {}


def enabled() -> bool:
    """True when model calls are answered from recordings, read per call."""
    return str(os.environ.get(ENV_VAR) or "").strip().lower() in _ON


def source_path() -> Path | None:
    """The recording named by ``LLM_REPLAY_SOURCE``, if one is."""
    value = str(os.environ.get(SOURCE_ENV_VAR) or "").strip().strip('"')
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_dir():
        path = path / "telemetry.db"
    return path.resolve()


@dataclass(frozen=True)
class Recording:
    call_id: str
    match: str
    message: dict
    payload: dict | None
    #: The calls whose answers were rejected on the way to this one, if any.
    superseded: tuple[str, ...] = ()
    #: How many identifiers in the answer were rewritten to today's.
    translated: int = 0


def request_sha256(body: Mapping) -> str:
    """The hash the live transport records for the bytes it sends."""
    return hashlib.sha256(json.dumps(body).encode("utf-8")).hexdigest()


def canonical(text: str) -> tuple[str, list[str]]:
    """``text`` with each identifier replaced by its order of first
    appearance, and the identifiers in that order."""
    order: dict[str, int] = {}

    def placeholder(match: re.Match) -> str:
        token = match.group(0)
        if token not in order:
            order[token] = len(order)
        return f"⟨{order[token]}⟩"

    return IDENTIFIER.sub(placeholder, text), list(order)


def _without_workspace_identity(value: Any) -> Any:
    """``value`` with the calling workspace's own name and revision, where
    code states them, read as placeholders."""
    if isinstance(value, str):
        def placeholder(match: re.Match) -> str:
            revision = match.group(2) if match.re.groups >= 2 else None
            named = f'{match.group(1)}"⟨workspace⟩"'
            return named + (f"{revision}⟨revision⟩" if revision else "")
        for pattern in WORKSPACE_IDENTITY:
            value = pattern.sub(placeholder, value)
        return value
    if isinstance(value, dict):
        return {key: _without_workspace_identity(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_workspace_identity(item) for item in value]
    return value


def _order_free(value: Any) -> Any:
    """``value`` with every list of records put in an order their content
    decides, not their identifiers. A prompt that lists matrix rows or tests
    sorted by id lists the same rows in another order once the ids are new;
    sorting each list by its records with identifiers blanked puts both in
    one order, so the identifiers then pair by what the records say. A
    message that is JSON text is read as JSON first."""
    if isinstance(value, dict):
        return {key: _order_free(item) for key, item in value.items()}
    if isinstance(value, list):
        items = [_order_free(item) for item in value]
        if items and all(isinstance(item, dict) for item in items):
            items.sort(key=lambda item: IDENTIFIER.sub(
                "⟨⟩", json.dumps(item, sort_keys=True)
            ))
        return items
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        return json.dumps(_order_free(parsed), indent=1)
    return value


def _order_free_request(request: Mapping) -> dict:
    """The request with each message's content order-free; the messages
    themselves keep their order, since a conversation is a sequence."""
    result = dict(request)
    result["messages"] = [
        {**message, "content": _order_free(message.get("content"))}
        if isinstance(message, Mapping) else message
        for message in request.get("messages") or []
    ]
    return result


_STREAM_TRANSPORT_KEYS = ("stream", "stream_options")


def _without_stream_options(request: Any) -> Any:
    """Strip transport-only streaming flags so streamed and non-streamed calls
    for the same prompt match each other in replay mode."""
    if not isinstance(request, Mapping):
        return request
    if not any(key in request for key in _STREAM_TRANSPORT_KEYS):
        return request
    return {
        key: value
        for key, value in request.items()
        if key not in _STREAM_TRANSPORT_KEYS
    }


def _canonical_sha(request: Any, *, order_free: bool = False) -> tuple[str, list[str]]:
    request = _without_workspace_identity(_without_stream_options(request))
    if order_free and isinstance(request, Mapping):
        request = _order_free_request(request)
    text, tokens = canonical(json.dumps(request))
    return hashlib.sha256(text.encode("utf-8")).hexdigest(), tokens


def translate(value: Any, mapping: Mapping[str, str]) -> tuple[Any, int]:
    """``value`` with every identifier ``mapping`` knows rewritten, and how
    many were. Walks strings inside dicts and lists, so a tool call's JSON
    arguments are translated as text."""
    if not mapping:
        return value, 0
    if isinstance(value, str):
        count = 0

        def swap(match: re.Match) -> str:
            nonlocal count
            token = match.group(0)
            replacement = mapping.get(token)
            if replacement is None or replacement == token:
                return token
            count += 1
            return replacement

        return IDENTIFIER.sub(swap, value), count
    if isinstance(value, dict):
        total = 0
        result = {}
        for key, item in value.items():
            result[key], count = translate(item, mapping)
            total += count
        return result, total
    if isinstance(value, list):
        total = 0
        result = []
        for item in value:
            translated, count = translate(item, mapping)
            result.append(translated)
            total += count
        return result, total
    return value, 0


def _refs(context: Mapping) -> tuple[str, ...]:
    return tuple(sorted(str(item) for item in context.get("artifact_refs") or []))


def _usable(record: Mapping) -> bool:
    """An answer the transport would have handed on: not empty, not cut off."""
    message = record.get("normalized_message")
    if not isinstance(message, dict):
        return False
    if str(record.get("finish_reason") or "") == "length":
        return False
    return bool(str(message.get("content") or "").strip() or message.get("tool_calls"))


def _repairs(earlier: Mapping, later: Mapping) -> bool:
    """True when ``later`` is a repair turn for ``earlier``'s request.

    Repairs take one of two shapes, and every repair path builds its turn from
    the original request rather than from the previous repair, so a chain of
    repairs all extend the first request:

    - the same messages, the last one carrying the original text with a
      rejection appended (:data:`REPAIR_MARKERS`);
    - the original messages unchanged, then two more turns: the rejected
      answer as the assistant's, and the correction as the user's. A tool
      conversation also grows, but by an assistant turn and a tool result,
      never a user turn, so it is not read as a repair.
    """
    first = (earlier.get("request") or {}).get("messages") or []
    second = (later.get("request") or {}).get("messages") or []
    if not first:
        return False
    if len(second) == len(first) + 2 and second[: len(first)] == first:
        return [message.get("role") for message in second[len(first):]] == ["assistant", "user"]
    if len(first) != len(second) or first[:-1] != second[:-1]:
        return False
    head, tail = first[-1], second[-1]
    if head.get("role") != tail.get("role"):
        return False
    original, repaired = head.get("content"), tail.get("content")
    if not isinstance(original, str) or not isinstance(repaired, str):
        return False
    appended = repaired[len(original):]
    return (
        len(repaired) > len(original)
        and repaired.startswith(original)
        and any(marker in appended for marker in REPAIR_MARKERS)
    )


def _connect(context: Mapping) -> tuple[sqlite3.Connection | None, str]:
    """The recording to read and a key naming it; the source never written."""
    path = source_path()
    if path is not None:
        if not path.is_file():
            raise FileNotFoundError(f"{SOURCE_ENV_VAR} names no telemetry database: {path}")
        handle = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        handle.row_factory = sqlite3.Row
        return handle, f"file:{path}"
    workspace_id = str(context.get("workspace_id") or "")
    if not workspace_id:
        return None, ""
    return debug_store.connection(workspace_id), f"workspace:{workspace_id}"


def _load(handle, call_id: str) -> dict | None:
    row = handle.execute("SELECT record FROM llm_calls WHERE id = ?", (call_id,)).fetchone()
    return json.loads(row["record"]) if row is not None else None


def _run_calls(handle, run_id: str, stage: str) -> list[dict]:
    """A recorded run's completed calls for one stage, oldest first."""
    rows = handle.execute(
        "SELECT record FROM llm_calls"
        " WHERE status = 'completed' AND run_id = ? AND stage = ?"
        " AND json_extract(record, '$.replay') IS NULL"
        " ORDER BY started_at",
        (run_id, stage),
    ).fetchall()
    return [json.loads(row["record"]) for row in rows]


def _kept(handle, record: dict) -> tuple[dict, tuple[str, ...]]:
    """The answer the run finally kept for this call's request.

    The call itself, unless a later call in the same run repaired it — then
    the last usable repair, with the ids of the answers it replaced.
    """
    correlation = record.get("correlation") or {}
    run_id = str(correlation.get("run_id") or "")
    stage = str(correlation.get("stage") or "")
    if not run_id or not stage:
        return record, ()
    started = str(record.get("started_at") or "")
    chain = [
        item
        for item in _run_calls(handle, run_id, stage)
        if str(item.get("started_at") or "") > started and _repairs(record, item)
    ]
    usable = [item for item in chain if _usable(item)]
    if not usable:
        return record, ()
    final = usable[-1]
    replaced = tuple(
        str(item["id"]) for item in [record, *chain] if item["id"] != final["id"]
    )
    return final, replaced


def _recording(
    handle, record: dict, match: str, mapping: Mapping[str, str] | None = None,
) -> Recording:
    final, replaced = _kept(handle, record)
    message, count = translate(dict(final["normalized_message"]), mapping or {})
    payload = final.get("raw_response")
    payload = dict(payload) if isinstance(payload, dict) else None
    if payload is not None and count:
        choices = [dict(choice) for choice in payload.get("choices") or []]
        if choices:
            choices[0]["message"] = message
        payload["choices"] = choices
    return Recording(
        call_id=str(final["id"]),
        match=match,
        message=message,
        payload=payload,
        superseded=replaced,
        translated=count,
    )


def _same_request(first: Mapping, second: Mapping) -> bool:
    return (first.get("request") or {}) == (second.get("request") or {})


def _exact(handle, body: Mapping) -> Recording | None:
    hashes = [request_sha256(body)]
    stripped = _without_stream_options(body)
    if stripped is not body:
        hashes.append(request_sha256(stripped))
    for digest in hashes:
        rows = handle.execute(
            "SELECT id FROM llm_calls"
            " WHERE status = 'completed'"
            " AND json_extract(record, '$.request_sha256') = ?"
            " AND json_extract(record, '$.replay') IS NULL"
            " ORDER BY started_at DESC",
            (digest,),
        ).fetchall()
        for row in rows:
            record = _load(handle, row["id"])
            if record is not None and _usable(record):
                return _recording(handle, record, "exact")
    return None


def _canonical_index(handle, key: str, *, order_free: bool = False) -> dict[str, list[str]]:
    """Canonical request hash -> recorded call ids, newest first, rebuilt only
    when the recordings change."""
    key = f"{key}|order_free" if order_free else key
    signature = tuple(handle.execute(
        "SELECT count(*), max(started_at) FROM llm_calls"
        " WHERE status = 'completed' AND json_extract(record, '$.replay') IS NULL"
    ).fetchone())
    with _lock:
        cached = _indexes.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
    index: dict[str, list[str]] = {}
    for row in handle.execute(
        "SELECT id, record FROM llm_calls"
        " WHERE status = 'completed' AND json_extract(record, '$.replay') IS NULL"
        " ORDER BY started_at DESC"
    ):
        record = json.loads(row["record"])
        digest, _tokens = _canonical_sha(record.get("request") or {}, order_free=order_free)
        index.setdefault(digest, []).append(str(row["id"]))
    with _lock:
        _indexes[key] = (signature, index)
    return index


def _remember(workspace_id: str, pairs: Mapping[str, str]) -> None:
    with _lock:
        forward = _aliases.setdefault(workspace_id, {})
        backward = _reverse.setdefault(workspace_id, {})
        for recorded, current in pairs.items():
            if recorded != current:
                forward[recorded] = current
                backward[current] = recorded


def _known(workspace_id: str) -> tuple[dict[str, str], dict[str, str]]:
    with _lock:
        return dict(_aliases.get(workspace_id) or {}), dict(_reverse.get(workspace_id) or {})


def _recorded_refs(current: tuple[str, ...], backward: Mapping[str, str]) -> set[str]:
    """Today's step refs, written with the recorded identifiers known so far."""
    return {
        f"{kind}:{backward.get(value, value)}" if sep else backward.get(ref, ref)
        for ref in current
        for kind, sep, value in [ref.partition(":")]
    }


def _by_step(handle, call_ids: list[str], context: Mapping, workspace_id: str) -> list[str]:
    """Candidates for one question, the one recorded for this step first.

    A run can ask the same question several times over different records —
    three document tests vouching the same transaction under three matrix
    rows — and the answers differ. Each goes back to the step it was recorded
    for: the candidate whose refs share the most with today's, read through the
    pairings learned so far. Otherwise the newest stays first.
    """
    if len(call_ids) < 2:
        return call_ids
    _forward, backward = _known(workspace_id)
    wanted = _recorded_refs(_refs(context), backward)
    if not wanted:
        return call_ids
    scored = []
    for position, call_id in enumerate(call_ids):
        row = handle.execute("SELECT correlation FROM llm_calls WHERE id = ?", (call_id,)).fetchone()
        refs = set(_refs(json.loads(row["correlation"] or "{}"))) if row else set()
        scored.append((-len(wanted & refs), position, call_id))
    return [call_id for _score, _position, call_id in sorted(scored)]


def _translated(
    handle, key: str, body: Mapping, workspace_id: str, context: Mapping,
) -> Recording | None:
    """The same question with other identifiers: strictly first, then with
    lists of records compared in an order their content decides."""
    request = debug_store.sanitize(dict(body))
    for order_free, match in ((False, "translated"), (True, "reordered")):
        digest, tokens = _canonical_sha(request, order_free=order_free)
        candidates = _canonical_index(handle, key, order_free=order_free).get(digest, [])
        for call_id in _by_step(handle, candidates, context, workspace_id):
            record = _load(handle, call_id)
            if record is None or not _usable(record):
                continue
            _recorded_digest, recorded_tokens = _canonical_sha(
                record.get("request") or {}, order_free=order_free,
            )
            if len(recorded_tokens) != len(tokens):
                continue
            pairs = dict(zip(recorded_tokens, tokens))
            _remember(workspace_id, pairs)
            forward, _backward = _known(workspace_id)
            return _recording(handle, record, match, {**forward, **pairs})
    return None


def _same_step(handle, context: Mapping, workspace_id: str) -> Recording | None:
    stage = str(context.get("stage") or "")
    current_refs = _refs(context)
    if not stage or not current_refs:
        return None
    forward, backward = _known(workspace_id)
    refs = tuple(sorted(_recorded_refs(current_refs, backward)))
    rows = handle.execute(
        "SELECT id, run_id, correlation FROM llm_calls"
        " WHERE status = 'completed' AND stage = ?"
        " AND json_extract(record, '$.replay') IS NULL"
        " ORDER BY started_at",
        (stage,),
    ).fetchall()
    matching = [
        (str(row["run_id"] or ""), str(row["id"]))
        for row in rows
        if _refs(json.loads(row["correlation"] or "{}")) == refs
    ]
    if not matching:
        return None
    latest_run = matching[-1][0]
    records = [
        record
        for run_id, call_id in matching
        if run_id == latest_run and (record := _load(handle, call_id)) is not None
    ]
    # One entry per question the run asked. A repair, or a retry of an empty
    # or cut-off answer, is the same question asked again, not a new one.
    questions = [
        record
        for index, record in enumerate(records)
        if not any(
            _repairs(earlier, record) or _same_request(earlier, record)
            for earlier in records[:index]
        )
    ]
    owner = str(context.get("run_id") or context.get("chat_id") or "")
    counter = (workspace_id, owner, stage, current_refs)
    with _lock:
        position = _counters.get(counter, 0)
        _counters[counter] = position + 1
    question = questions[min(position, len(questions) - 1)]
    answer = next(
        (
            record
            for record in reversed(records)
            if _same_request(record, question) and _usable(record)
        ),
        None,
    )
    if answer is None:
        return None
    return _recording(handle, answer, "same_step", forward)


def find(body: Mapping, context: Mapping) -> Recording | None:
    """The answer kept for this request in the recordings, or None."""
    workspace_id = str(context.get("workspace_id") or "")
    handle, key = _connect(context)
    if handle is None:
        return None
    try:
        return (
            _exact(handle, body)
            or _translated(handle, key, body, workspace_id, context)
            or _same_step(handle, context, workspace_id)
        )
    finally:
        if key.startswith("file:"):
            handle.close()


def _reset_for_tests() -> None:
    with _lock:
        _counters.clear()
        _aliases.clear()
        _reverse.clear()
        _indexes.clear()


__all__ = [
    "ENV_VAR",
    "IDENTIFIER",
    "REPAIR_MARKERS",
    "Recording",
    "SOURCE_ENV_VAR",
    "canonical",
    "enabled",
    "find",
    "request_sha256",
    "source_path",
    "translate",
]

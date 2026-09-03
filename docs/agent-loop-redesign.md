# Agent loop redesign: a steering model over gated capabilities, in small steps

**Status:** design, not yet implemented. This is the handoff for replacing the
fixed request-to-closure pipeline in front of the workflow engine with a
budgeted model loop that plans, runs capability units, reads what happened,
repairs what it can, and asks when it cannot. Each step lands on its own,
leaves every existing tab button and slash command working, and is measured
against the previous step before the next one starts.

The governing rule: **the model steers; the registry gates.** The model
decides what to run, in what order, with what scope and instruction, and what
to do about the result. The capability registry decides whether a thing may run
at all, and the unit pipeline decides how: declared context, one worker turn,
a persisted proposal, a guarded commit, a receipt. Nothing below changes that
second half. It changes who drives it.

`docs/agent-architecture.md` describes the engine this plan builds on and
`docs/rcm-generation-redesign.md` shows the step format this plan follows.
Every file path below is under `backend/app/` unless it says otherwise. Every
claim about what the code does today was read from the code on 3 September
2026 at commit `0c057d3`; where a step depends on a behaviour that should be
confirmed before building on it, the step says so.

## The evidence

All 45 agent runs and 13 assistant chats in the local workspaces were read.
Every run used `deepseek/deepseek-v4-flash-0731` through OpenRouter; 44 ran in
auto mode, one in permission mode.

| Fact | Count |
|---|---|
| Runs on the workflow engine | 45 |
| Runs on the action engine | 0 |
| Chats containing a model-written reply | 0 of 13 |
| Runs that reached a terminal status with unsettled units | 13 |
| Units failed after the worker's bounded repair allowance was spent | 26 |
| Units failed on an empty provider completion | 6 |
| Units failed on finding support validation after the model turn | 2 |
| Runs ended by the run time limit | 2 |

Every chat message in every workspace came from a tab button, a slash command,
or a whole-message phrase. The coordinator tool loop in `assistant.py` has not
answered a single message in the recorded history. The routing phrase tables
and the bounded router worker have decided nothing a person typed.

The 26 repair-exhausted units are the failures the loop is for. They are, in
the words of the validators that rejected them:

- `reporting.finding`, 15 units: a template section is empty, or the `#`
  title, `**Severity:**` line, or a `##` heading is missing.
- `tests.generate`, 7 units: a step names a population that is not a supplied
  table; a control attribute names a population no step declares; a Polars
  snippet has a syntax error or reads a table under the wrong name; one
  response was not valid JSON because of a `\.` escape.
- `planning.rcm`, 2 units: the memorandum plans a response for a theme no row
  owns.
- `tests.cycle_linkage`, 2 units: the proposal answers 34 (then 45) of 47
  required comparisons.

Each of these is an instruction a person could give in one sentence. Today the
worker gets two or three attempts inside one unit, the unit fails, the stage
folds to failed, the run ends, and a person clicks Retry. The rejection is
persisted and `runtime/unit_pipeline.py` `_linked_repair_seed` does reload it
on a retried run, so the machinery for a second attempt with the errors in
hand exists. Nothing decides to use it.

The 6 empty completions raise `ModelResponseUnusable` at
`runtime/model_gateway.py` with no retry. `docs/rcm-generation-redesign.md`
step 0a already specifies the fix.

Narration, from one live run, `treasuryfull/20260903-071737-67df8c`, first ten
agent messages: "Still generating RCM tests - this is taking longer than
usual." five times; "The first draft didn't match the required shape, so I'm
redoing it." four times; one other. Run `20260903-064223-6b4034` said "Reading
the observation, the target RCM row, the test, the execution result, the
finding template, and the exception rows for Eligible finding drafts." three
times in a row. Every treasuryfull run opens with a plan line that lists the
twelve capabilities it will *not* run before the one it will.

## What one request does today

| Concern | Where |
|---|---|
| Message intake, intent, idempotency | `assistant_chats.py` `send_message`, `_process_message` |
| Slash commands and whole-message phrases | `agent/commands.py` `COMMANDS`, `match_slash`, `match_phrase` |
| Read-only tool loop, at most 8 steps | `assistant.py` `ask`, `_Session`, `READ_TOOLS`, `MAX_STEPS` |
| The two mutating tools the loop is lent | `assistant.py` `Commander`, `_command_schemas`: `start_command(command_id)`, `start_action(request)` |
| Launch policy, one live run per workspace, queueing | `assistant_chats.py` `_launch_command`; `agent/runner.py` `start_command_run`, `steer`, `AgentBusyError` |
| Deterministic classification of a command | `agent/routing.py` `classify_command`, `_single_intent`, the phrase tables `LIFECYCLE_PHRASES`, `GENERATION_RULES`, `SCOPE_EXECUTION_RULES`, `TARGET_OPERATION_MARKERS`, `ISOLATED_OPERATION_MARKERS`, `COMPOUND_SEPARATORS` |
| The one model turn routing may spend | `agent/routing.py` `CommandRouter`, `resolve_pending_route`, `ROUTER_SYSTEM`; `agent/context_bundles.py` |
| Plan materialization and budgets | `agent/routing.py` `install_resolution`; `agent/workflow.py` `materialize` |
| Scope from target refs | `agent/capabilities/_shared.py` `target_rcm_ids` (reads `rcm:` and `observation:` only) |
| Scheduling, folding, finishing | `agent/runtime/workflow_runner.py` `execute`, `_run_stage`, `_fold_pipeline_failure`, `_fold_stage`, `_finish` |
| One unit: context, worker, proposal, approval, commit, receipt | `agent/runtime/unit_pipeline.py` `UnitPipeline.run`; `agent/execution_support.py` `resolve_context` |
| Retry and continue | `agent/runner.py` `retry_run`, `continue_audit`; `routes/agent_routes.py` |
| Progress prose | `agent/narration.py`; `agent/base.py` `_emit_model_wait_heartbeat`; `workflow_runner.py` `_narrate_context`, `_narrate_repair` |
| Chat projection of a run | `assistant_chats.py` `_run_projection`, `get_chat`; `frontend/src/components/agent/ChatRunCard.vue`, `ChatTranscript.vue` |

A text message that matches no phrase reaches `assistant.ask` with the two
mutating tools. If the model calls `start_command`, a registered goal template
starts a workflow run. If it calls `start_action`, the text goes to
`classify_command`; a miss there spends one router turn, and the route is
either a workflow outcome set with whatever `target_refs` the router named, or
the action engine, whose interpreter authors a DAG of registered actions
itself. The run then executes a fixed closure. A failed unit is terminal for
the run. The next message from the auditor, while the run is live, is queued
as a *follow-up command*, not delivered to anything that could change course.

## Where it strains

1. **Classification happens once, before any state is read.** The phrase
   tables and the router turn decide the engine and the outcome set with the
   command text and a 6,000-character bundle. Neither can look at the
   workspace, ask a follow-up, or revise after a result.
2. **A run is a closure, not a conversation.** Steering while live only queues
   a new command. There is no way to say "skip that row" or "do this one again
   with this correction" to a run in progress.
3. **Failure is terminal per unit, and reruns are coarse.** The bounded repair
   loop is the only place errors are read by a model. Retry and Continue start
   whole linked runs, by a person.
4. **Nothing below an RCM row can be addressed.** `target_rcm_ids` understands
   `rcm:` and `observation:`. "Test DT-123 doesn't seem correct, redraft" cannot
   be scoped to DT-123 by any route: the test-generation stage either finds
   nothing to do or regenerates every row's tests under force. The action
   engine can edit a test, but its interpreter writes the change itself,
   without `tests.generate`'s prompt, gates, or declared evidence.
5. **"As appropriate" has no home.** "I uploaded document XX, revise APM and
   RCM as appropriate" matches no phrase, "revise" is not a force phrase in
   `workflow.command_generation_mode`, and currency is `not_assessed` by
   design. The likely outcomes are "Nothing needed doing" or a wholesale
   regeneration. The judgment the auditor is asking for, whether the new
   document changes anything, is the one judgment no component may make.
6. **Two engines, two qualities.** The action engine exists for anything the
   workflow does not own, and its plan is model-generated, which is the shape
   the v3 migration was built to escape.
7. **Narration is a template log read as prose.** Heartbeats, repair notes,
   and context reads are emitted per model call as agent *messages*, so the
   transcript repeats itself in proportion to how hard a stage was.

Two things are not the problem and are not changed: the worker, executor,
context, proposal, and receipt contracts, which recorded no defects across the
45 runs; and the privacy choke points, which the loop inherits unchanged.

## The target

```text
chat message
  |-- slash / whole-message phrase / tab button  -> workflow run (deterministic, as today)
  `-- anything else                               -> coordinator (read tools + plan)
                                                       |-- answer
                                                       `-- take_action -> agent run
                                                                           |
        +------------------------------------------------------------------+
        |  AgentLoop (engine "agent", one thread, budgeted, durable)        |
        |    turn: read inbox -> model turn with tools -> dispatch          |
        |    tools: read state | plan_outcomes | run_outcomes | inspect_run |
        |           rerun_units | ask_auditor | assess_change | actions     |
        |    run_outcomes -> child workflow run, in-process, same handle    |
        |                    (materialize -> WorkflowRunner -> UnitPipeline) |
        +------------------------------------------------------------------+
```

- **One loop per request, durable as a run.** Engine `agent`, on the worker
  thread, through `DefaultRunRuntime` and `DefaultModelGateway`, with a turn
  budget, a child-run budget, a deadline, pause and cancel, and a persisted
  conversation so a crash resumes rather than restarts.
- **Capabilities are tools at unit granularity.** `run_outcomes` runs a
  declared outcome set over named targets as an ordinary child workflow run.
  The registry's dependency closure and readiness still decide what actually
  executes. The loop cannot skip a prerequisite; it can only be told what is
  missing and choose to run it or ask.
- **Failure is an observation.** `inspect_run` returns the validator's errors
  and the rejected proposal's excerpt; `rerun_units` runs the unit again with an
  instruction, once.
- **The auditor's instruction is declared context**, not a prompt patch: a
  bounded, hash-recorded source the worker reads through its bundle.
- **Targets below the RCM row are addressable**: `doctest:`, `datatest:`,
  `finding:`, `document:`.
- **Review is a model turn** at the end of a loop, with next steps as clickable
  suggestions, and an optional per-stage checkpoint in permission mode.
- **Narration is a ledger, not a transcript.** Events stay; prose is written
  once per stage and once at the end.

### What does not change

- Every provider call under `agent/` goes through `DefaultModelGateway`.
- A worker sees only its resolved `ContextBundle`; a manifest is content-free.
- A proposal is persisted before commit; a commit is guarded by parent hash or
  CAS and produces a receipt; interrupted commits are reconciled.
- Row-level table data reaches the provider only under the two declared
  exceptions.
- `reuse_existing` is the default; `force` is explicit; the framework never
  assesses currency on its own.
- Slash commands, whole-message phrases, and tab buttons start a workflow run
  without a model turn.

### Two requests, after the plan

**"Test DT-123 doesn't seem correct, redraft it."** The coordinator reads the
test with `inspect_audit_artifacts`, sees it is a Document Test on row R-4, and
calls `take_action`. The loop calls `plan_outcomes(["tests.specified"],
target_refs=["doctest:DT-123"], generation_mode="force")`, which reports one
unit on R-4 and nothing blocked. It calls `run_outcomes` with the same scope
and `instruction="The auditor says DT-123 does not look right; redraft it."`.
The child run expands one `test_generation` unit carrying
`regenerate_test_ids=["DT-123"]`, resolves the `tests.generate` preset with the
current test and the instruction in the bundle, and the executor replaces
DT-123 in place, preserving the row's other tests. The loop inspects the child,
writes one closing message naming the new title and objective, and suggests
"Run DT-123".

**"I uploaded new document XX, revise APM and RCM as appropriate."** The loop
calls `assess_change(document_ids=["XX"])`. The child run extracts, categorizes,
and analyses XX, then the `planning.delta_review` worker compares the analysis
against the current APM and RCM and proposes: two APM sections to revise, one
RCM row to add, one to revise, with reasons. In permission mode the loop asks
the auditor to confirm; in auto mode it runs `planning.apm_ready` under force
with the two section changes as the instruction, then `planning.rcm_ready`
under force with the row changes as the instruction (row-scoped once
`docs/rcm-generation-redesign.md` step 3 lands). It closes with what changed
and what it left alone, and suggests "Regenerate tests for R-9".

## The steps

Each step has an *invariant* (what must still work when it lands), a *measure*
(what to compare against the previous step on a treasuryfull and a procurement
engagement), and a *rollback*. Steps 0, 1, and 2 are independent of each other
and of the rest. Step 3 builds on 2. Step 4 is the core and stands on 0 and 3.
Steps 5 to 9 build on 4.

---

### Step 0. Stop losing runs to fixable failures

Independent of everything below and worth doing first.

#### 0a. Retry an unusable completion once at the gateway

Exactly as specified in `docs/rcm-generation-redesign.md` step 0a. Six units
across four capabilities in the evidence failed this way. One addition: a
`finish_reason` of `error` counts as unusable and is retried under the same
rule; it is the reason on the one `planning.rcm_ready` run that failed with no
text.

#### 0b. Do not expand a finding unit the executor will refuse

`executors/reporting.py` raises `Finding draft failed support validation` at
line 354 *after* the model turn, so the turn is billed and the unit fails. Two
units in the evidence, both because the supporting Document Test was not
complete. The check that failed is deterministic and depends only on the
observation's test.

- Add `findings.observation_support_issues(workspace, observation) ->
  list[str]`: the subset of `findings.support_issues` computable before a draft
  exists (the linked test exists, is linked to the observation's row, and is
  complete). `support_issues` calls it so the two cannot drift.
- `capabilities/reporting.py` `_finding_units` skips an observation with
  issues. `_findings_ready` reports them: state `blocked`, reason
  `"N eligible observations rest on a test that is not complete"`, details
  `unsupported: [observation ids]`. `narration.next_steps` already surfaces
  readiness reasons.

Test: `tests/test_agent_reporting_finding.py`: an exception observation whose
Document Test has a pending item expands no unit; readiness names it; the
executor's own check still refuses a hand-built draft with the same issue.

**Invariant:** no prompt, schema, or proposal shape changes.
**Measure:** failed units per run by error class (group every run's failed
units by capability and the first 80 characters of the error).
**Rollback:** revert.

---

### Step 1. Narration diet

Independent. Cheap. The most visible change in this plan. Everything here is
in `agent/narration.py`, `agent/base.py`, and
`agent/runtime/workflow_runner.py`; the frontend needs one icon.

#### 1a. One heartbeat per stage

`base.py` `_emit_model_wait_heartbeat` says "Still <label> - this is taking
longer than usual." through `narration.say` on every slow call, so a stage of
seven slow units says it seven times. Keep the *activity* projection live and
say the sentence once:

- The heartbeat consults `self.run["activity"]["task_id"]` and a per-runner set
  `_heartbeat_noted`; a second heartbeat under the same task id updates
  `activity.detail` to `"running long (unit 3 of 7)"` and says nothing.

#### 1b. Repair notes are notes, once per unit

`workflow_runner.py` `_narrate_repair` says `narration.repair_note()` on every
repair. Make it `narration.note(kind="repair", unit_id=...)` with the unit's
subject: `"Test for <row risk>: the first draft failed 3 checks; redoing it."`
A second repair of the same unit is silent. Notes reach the narration rail, not
the message column.

#### 1c. Context reads are said once per stage

`_narrate_context` says "Reading X, Y, and Z for <stage>" per unit. Say it for
the stage's first unit only; later units differ only in which row they read,
and the per-unit provenance already reaches the transcript through
`context_reads` and `_coalesced_context_reads`.

#### 1d. The plan line names what will run

`narration.plan_sentence` appends "A, B, ... and L are already done, so I'll
reuse them" on every run. `routing._explanation` passes `reused` only when the
request named something already satisfied (`set(requested) & set(reused)`);
otherwise the sentence is the running titles alone. The full reuse list stays
on `workflow.reused_capabilities` for the plan spine.

#### 1e. Frontend

`frontend/src/components/agent/AgentNarration.vue`: `kind === 'repair'` draws
`pi pi-wrench`. Nothing else.

Tests, in `tests/test_agent_narration.py`: a heartbeat under one task id
produces one message however many times it fires; a repair produces one note
and no message; `plan_sentence` omits reuse unless requested overlaps reused;
the closing text is unchanged.

**Invariant:** every event still reaches the telemetry stream; `run.json`
`narration` keeps its cap; the plan spine still shows reuse.
**Measure:** agent messages per run and per stage on a treasuryfull
`tests.specified` regeneration (run `67df8c` is the baseline: 10 messages in
its first stage, 9 of them repeats).
**Rollback:** revert.

---

### Step 2. Address what lives below the RCM row

Independent. Makes "redraft DT-123" expressible as one unit through the real
generation worker, before any loop exists.

#### 2a. A target scope that understands more than rows

`capabilities/_shared.py`: add

```python
@dataclass(frozen=True)
class TargetScope:
    rcm_ids: tuple[str, ...]
    test_ids: tuple[str, ...]          # datatest: and doctest:
    observation_ids: tuple[str, ...]
    finding_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    explicit: bool                     # any ref other than workspace:current

def target_scope(workspace, scope) -> TargetScope: ...
```

It parses `rcm:`, `datatest:`, `doctest:`, `observation:`, `finding:`, and
`document:` from `scope["target_refs"]`, maps a test to its row through
`findings._test_rcm_id` (moved to `_shared` as `test_rcm_id`), and a finding to
its row through `source_observation_id`. `target_rcm_ids` becomes a wrapper
returning `TargetScope.rcm_ids` so every existing caller is unchanged.

#### 2b. Regenerate one test, not the row

`capabilities/tests.py` `_generation_units`: when `target_scope(...).test_ids`
is non-empty, expand one unit per owning row with
`input_payload={"row": row, "regenerate_test_ids": [ids on this row]}`; force
is implied for those rows. The payload changes the unit's `input_sha1`, so a
proposal for the whole row is never mistaken for one for the test.

`context/presets.py` preset `tests.generate`: add an optional source
`current_tests` (source type `tests`, selector `artifacts.current`,
representation `current_artifact`, budget 6 items, 24,000 characters). Today the
generation turn never sees the row's existing tests, so a regeneration under
force is a cold draft. `context/adapters.py` `test_generate_scope` supplies the
row's tests from `_shared.all_tests`, marking the ones named in
`regenerate_test_ids` in candidate metadata.

`workers/tests.py` `_generation_prompt_payload`: read
`request.unit_input.get("regenerate_test_ids")`; when set, add
`current_tests` and `regenerate_test_ids` to the payload and one line to
`instructions`: "Return a replacement for each id in regenerate_test_ids and
nothing else; carry `replaces: <id>` on each; keep the other tests as they
are." `validate_generate_proposal`: when the input names ids, require exactly
one test per id, each with a matching `replaces`.

`executors/tests.py`: `TestGenerateExecutorTarget` gains
`regenerate_test_ids: tuple[str, ...] = ()`. `execute_test_generation` and
`reconcile_test_generation`: with ids set, update those records in place (same
id, `revised_by: "agent"`, a history entry with the run id), preserve every
other test on the row regardless of `created_by`. An explicitly named test may
be overwritten even if auditor-authored: naming it is the permission.
`audit_execution.py` `_bind_test_generate` passes the ids from the unit input
into both the target and `unit_input`.

#### 2c. Redraft one finding

`capabilities/reporting.py` `_finding_units`: with `finding_ids` in scope,
expand the unit for the finding's source observation under force.
`executors/reporting.py` `execute_finding` already updates an agent-sourced
existing draft in place and refuses an auditor-sourced one; an explicitly named
finding is overwritable on the same rule as 2b.

#### 2d. The message and the run endpoint carry the scope

`assistant_chats.py` `OUTCOME_RUN_CONTEXT_KEYS` gains `generation_mode`.
`_launch_command` passes it on the command. `routes/agent_routes.py`
`create_run` already accepts `target_refs` and `generation_mode`.

Frontend: a "Redraft" action on a test row in
`frontend/src/components/DataTestsTab.vue` and `DocTestsTab.vue` sends
`requested_outcomes=["tests.specified"]`, `run_context={"target_refs":
["doctest:<id>"], "generation_mode": "force"}` through
`useAssistantChat.send`. `DataTestsTab.vue` has uncommitted user changes at
the time of writing; build on them rather than over them.

Tests: `tests/test_agent_capabilities_tests.py` for scope parsing and unit
expansion; `tests/test_agent_tests_generate_worker.py` for the payload,
validator, and a bundle carrying `current_tests`;
`tests/test_agent_tests_generate_executor.py` for in-place replacement with
siblings untouched (assert their hashes); `tests/test_assistant_chats.py` for
the run-context key.

**Invariant:** a request with no test refs expands and commits exactly as
today; the `tests.generate` preset hash changes, so uncommitted proposals from
earlier runs regenerate rather than reuse (acceptable: they are proposals, not
artifacts).
**Measure:** "redraft DT-x" from the tab is one unit, one model turn, one
record changed.
**Rollback:** revert; the `revised_by` field is additive.

---

### Step 3. The auditor's instruction as declared context

Builds on 2 (the scope builders it extends). A worker may only read its bundle,
so the instruction enters the bundle: declared, bounded, hash-recorded.

#### 3a. Declare the representation

`context/model.py`: add `"auditor_instruction": "allow_auditor_instruction"`
to the representation-to-permission table. `context/presets.py`: register a
deterministic selector `instructions.current` with
`supported_source_types=("instructions",)`, one item, no ranking.

#### 3b. Declare the source on the presets that accept steering

Add an optional source to `tests.generate`, `planning.apm`, `planning.rcm`,
`reporting.finding_draft`, and `analysis.definitions`:

```python
ContextSource(
    id="instruction",
    source_type="instructions",
    required=False,
    selector=ContextSelector(selector_id="instructions.current"),
    representations=(ContextRepresentation("auditor_instruction"),),
    budget=ContextBudget(max_items=1, max_characters=2_000),
)
```

Privacy: `allow_auditor_instruction` is granted on exactly these presets.

#### 3c. Supply the candidate

`context/adapters.py`: `instruction_candidates(text) ->
tuple[ContextCandidate, ...]`: `source_ref=f"instruction:{sha1(text)[:12]}"`,
`source={"sha1": ..., "characters": len}`, representation
`auditor_instruction: text`. The manifest records the ref, hash, and size, as
for every source; the text lives only in the local bundle. Each of the five
scope builders (`test_generate_scope`, `rcm_scope`, `finding_draft_scope`,
`analysis_definition_scope`, and the APM scope assembled in
`audit_execution._bind_apm`) takes `instruction: str | None` and adds the
candidate under source id `instruction`.

#### 3d. Plumb it

`run["context"]["instruction"]` is the durable home. `OUTCOME_RUN_CONTEXT_KEYS`
gains `instruction`; `routing.install_resolution` copies it to
`scope["instruction"]`; the five binders pass `scope.get("instruction")` to
their scope builder.

`runner.retry_run(workspace, run_id, *, target_refs=None, instruction=None)`:
`target_refs` narrows the linked command (default: carried refs as today);
`instruction` lands in the new run's context. `routes/agent_routes.py`
`retry` accepts both in its body. `ChatRunCard.vue` Retry gains an optional
"Tell the agent what to change" field.

Note on `_linked_repair_seed`: it reuses a rejection only under an exact
execution identity, and the manifest hash is part of that identity, so a retry
*with* an instruction never loads the seed. That is correct: the instruction is
the seed. Step 5's loop supplies the validator errors in the instruction text
itself.

#### 3e. Workers read it

`workers/tests.py` `_generation_prompt_payload`, `workers/planning.py`
`run_apm_worker` and `run_rcm_worker`, `workers/reporting.py`
`run_finding_worker`, `workers/analysis.py` (definitions): when the bundle
holds an `instruction` item, add `"auditor_instruction": <text>` to the payload
and one system-prompt sentence: "`auditor_instruction`, when present, states
what the auditor wants changed. Follow it over the default instructions, never
over the response contract." The prompt hash changes; that is the point of the
hash.

`narration._SOURCE_LABELS["instruction"] = ("your instruction", "your
instruction")` so the context line reads "Reading your instruction, the RCM
row, ...".

Tests: `tests/test_agent_context_resolver.py`: the manifest for a bundle with
an instruction carries hash and size only, and a 3,000-character instruction is
truncated with a recorded truncation; `tests/test_agent_context_adapters.py`
for each scope builder; the four worker test files for a bundle with and
without the item; `tests/test_agent_runner.py` for `retry_run` narrowing and
instruction; `tests/test_agent_routing.py` for `install_resolution`.

**Invariant:** a run with no instruction resolves the same sources as before
(the optional source is absent, manifested as such); the row-level privacy
boundary is untouched.
**Measure:** on a finding unit that failed with "narrative section 'Condition'
is empty", a retry with the instruction "Every template section must contain
text" succeeds on the first worker attempt.
**Rollback:** revert the presets; the run-context key is ignored by older code.

---

### Step 4. The agent engine: a durable, budgeted tool loop

The core. Builds on 0 (so the loop is not the first thing to meet an empty
completion) and 3 (so a rerun can carry an instruction). Everything in this
step is additive; no route that exists today changes.

#### 4a. The engine

`agent/store.py`: `AGENT_ENGINE = "agent"`; `COMMAND_ENGINES` and
`RUN_ENGINES` include it. `agent/routing.py`: `ROUTE_AGENT = "agent"`,
`ENGINE_BY_ROUTE[ROUTE_AGENT] = store.AGENT_ENGINE`; `classify_command`
returns an agent route for a command whose `source == "loop"` before any phrase
matching. `store.new_command_run` accepts the source. `runner._execute`
dispatches `AGENT_ENGINE` to `agent_loop.AgentLoop(workspace, run,
handle).execute()`. `tests/test_agent_final_boundaries.py`
`test_the_process_layer_dispatches_to_engines_and_owns_no_scheduling` updates
its expected engine set; `runner.py` still imports no capability, workflow,
worker, or executor module.

#### 4b. The loop

New module `agent/agent_loop.py`, `class AgentLoop(BaseRunner)`. `BaseRunner`
gives it the runtime, the gateway, projections, and `MODEL_WAIT_LABELS`. Add
`"agent:loop": "Working out what to do next"` to that table.

```text
execute():
  mark_started
  conversation = ConversationStore(workspace, run_id).load()
                 or seed(system, run["context"]["conversation_seed"], command text)
  while True:
    runtime.checkpoint()                       # cancel, pause, deadline
    drain inbox -> append each steering text as a user turn
    reserve one loop turn (limits.max_loop_turns) or finish("budget")
    message = gateway.complete(LOOP_SYSTEM, "", activity,
                               tools=TOOLS, conversation=conversation,
                               return_message=True)
    append assistant turn; persist conversation
    if no tool_calls: say(content); finish; break
    for call in tool_calls:
      result = dispatch(call)                  # tool errors are returned, not raised
      append tool turn (bounded); persist
      if call was finish: break out
```

Terminal status: `completed` when `finish` was called and every child run
completed; `completed_with_open_items` when any child ended with open items or
`ask_auditor` went unanswered at cancel; `completed_with_failures` when a child
failed and was not repaired; `failed` on an unhandled error; `cancelled`.

Budgets on the run's `limits`: `max_loop_turns` (default 24), `max_child_runs`
(6), `max_tool_calls` (60), `max_auditor_questions` (3). The loop's own model
turns are charged through `reserve_model_turn` like every other turn. The
parent's deadline is extended by each child's elapsed time, so the loop's
deadline is about the loop; a child keeps its own.

#### 4c. The conversation sidecar

`AgentRuns/<run_id>/conversation.json`: `{"schema": 1, "messages": [...]}`
written after every turn through the runtime's atomic write. Tool results are
truncated at `MAX_TOOL_RESULT_CHARS = 6_000` before they are appended. When the
serialized conversation exceeds `MAX_CONVERSATION_CHARS = 60_000`, tool result
bodies from turns older than the last six are replaced by
`{"omitted": true, "summary": <first 200 characters>}`; the assistant's own
turns are kept whole. On resume the sidecar is reloaded and the loop continues
at the next turn; a child run left non-terminal is resumed first (4e).

The sidecar holds what the assistant's read tools return, which today already
reaches the provider under the read-only assistant's rules (bounded previews of
real rows). It is local, per run, and deleted with the run. `run.json` stays
content-free apart from the loop's own prose in `messages`.

#### 4d. The tools

The read tools move from `assistant.py` `_Session` to a new
`assistant_tools.py` as functions over a workspace and a small session (frame
cache, artifacts, chat id). `assistant.py` and the loop both call them; the
schemas and handlers stay registered once. `list_artifacts` and `get_artifact`
from `action_runner.py` join them.

New tools, in `agent/loop_tools.py`, each returning JSON-safe dicts and
raising `WorkspaceError` for the loop to hand back as a tool error:

| Tool | Does | Reads or writes |
|---|---|---|
| `plan_outcomes(requested_outcomes, target_refs=[], generation_mode="reuse_existing")` | `routing.validate_requested_outcomes` picks the definition; `workflow.materialize` over that registry with the scope `install_resolution` would build; returns `{definition, stages: [{capability, title, units, readiness_before}], reused, blocked: [{capability, reasons}], estimated_model_turns}` | reads |
| `run_outcomes(requested_outcomes, target_refs=[], generation_mode, instruction=None, review_each_stage=False)` | 4e; returns the `inspect_run` shape of the terminal child | writes, through a child run |
| `inspect_run(run_id, unit_id=None)` | status, error, `narration.blockers`, stages with unit counts; per failed or blocked unit its `error` and, from `UnitSidecarStore.load_rejection`, `validation_errors` (at most 8, 2,000 characters) and `response_excerpt` (600 characters) | reads |
| `rerun_units(run_id, unit_ids, instruction=None)` | `runner.retry_run` narrowed to those units' `parent_refs`, run as a child; refuses a unit already rerun once in this loop (`run["repairs"]`) | writes, through a child run |
| `run_action(request)` | a child action run, as `start_action` today; retired in step 8 | writes |
| `ask_auditor(question, options=[])` | with options: `runtime.wait_for_interaction` on a `clarification` interaction; without: `runtime.wait_for_input`; returns the answer; counts against `max_auditor_questions` | blocks |
| `finish(summary, suggestions=[])` | ends the loop; the summary is the closing message; suggestions per step 6 | writes the run record |

`generation_mode="force"` with `target_refs=["workspace:current"]` is refused
by `run_outcomes` unless `run["context"]["force_confirmed"]` is set, which the
coordinator sets only when the auditor's own message contains a force phrase
from `workflow.command_generation_mode`. Whole-workspace regeneration stays an
explicit human ask.

The system prompt `prompts.LOOP_SYSTEM`, tag `[agent:loop]`, carries the
operating rules of step 5 and nothing domain-specific.

#### 4e. Child runs, in process

`agent/runner.py`:

```python
def run_child_run(workspace, parent_run, command, context) -> dict:
    """Create, route, execute inline, and return one child command run."""
```

It calls `store.new_command_run(..., parent_run_id=parent["id"], context=...)`,
copies `model_profiles` from the parent, `routing.resolve_route`, appends the
child id to `parent_run["children"]`, registers a child handle, executes
through `_run_engine(workspace, child, handle)` (factored out of `_execute` so
both paths share the engine switch), unregisters the handle, and returns the
reloaded terminal record. No thread is started and `start_command_run` is not
called, so the one-live-run rule holds by construction: the workspace still has
one thread. `live_handles()` deduplicates by handle identity.

`RunHandle.child_of(parent, run_id)`: shares `cancel`, `cancel_context`,
`pause_requested`, and `resume` with the parent (cancel or pause the loop and
the child stops with it) and owns its own `inbox`, `command_queue`,
`decisions`, `interaction_responses`, and the two `resolved` events, so an
answer to the child's approval reaches the child and a steering message to the
loop waits for the loop.

A child interrupted by a crash is found on resume through `parent["children"]`
with a non-terminal status and resumed by the same inline path before the loop
takes its next turn; `workflow.recovery` and the sidecar rules already make
that safe.

`steer` for an `agent`-engine run appends to `handle.inbox` (steering) rather
than `command_queue` (a follow-up), so the loop reads it at its next turn. The
chat's `_launch_command` needs no change: an active loop run is a command run,
and `steer` decides.

#### 4f. The hand-off from the coordinator

`assistant.py` `Commander` gains `take_action(brief)`: "Hand this conversation
to the durable agent to carry work out. Use it when the request needs the
workspace changed and no single registered command covers it." The handler
builds `command={"source": "loop", "text": brief}` and
`context={"conversation_seed": <the coordinator's turns so far, bounded as in
4c>}` and starts the run through `_launch_command`'s policy (queue behind an
active run, one run per message). `start_command` stays as the cheap path for a
registered command; `start_action` is retired in step 8. `COMMAND_RULES` gains
the sentence that picks between them.

#### 4g. Chat and frontend

`_run_projection` adds `engine` and `children` (child run ids). A child run
carries the parent's `chat_id` and `source_message_id`, so its card already
lands in the transcript; `ChatTranscript.vue` indents a card whose
`parent_run_id` is a loop run in the same chat, and `ChatRunCard.vue` shows the
loop's label from `MODEL_WAIT_LABELS`. The loop's user-facing turns are its
`messages`, projected as today.

#### 4h. Documentation gates

`AGENTS.md` section 4 replaces "The assistant and agent do not use arbitrary
tool loops inside `agent/`" with: "The agent loop is the one tool loop under
`agent/`. It is bounded by `max_loop_turns`, `max_child_runs`,
`max_tool_calls`, and the run deadline, calls the provider only through
`ModelGateway`, and changes the workspace only through child runs and
registered actions." Section 3 "Engines" and `docs/agent-architecture.md`
"Migration Direction" state the new final set `{workflow, agent, intake}`
(after step 8) and describe the loop as a third scheduler composed with
`RunRuntime`.

Tests, new `tests/test_agent_loop.py`, scripted through `fake_agent_llm` under
the `agent:loop` tag:

1. A loop that answers without tools ends `completed` with one message and no
   child.
2. `plan_outcomes` then `run_outcomes` for `planning.apm_ready` on
   `workspace_with_data`: one child run, `completed`, the loop's `finish`
   message is the run's last message.
3. A child whose `tests.generate` script fails validation twice: the loop's
   `inspect_run` result carries the validator errors; `rerun_units` with an
   instruction produces a second child whose bundle carries the instruction
   (assert through the manifest's source ids) and succeeds.
4. `max_loop_turns` reached: status `completed_with_open_items`, closing message
   says so, no child left non-terminal.
5. Cancel during a child: both records `cancelled`, committed work kept.
6. Kill the thread after the second turn; `resume_run` reloads the sidecar and
   the loop takes turn three with the same conversation.
7. Steering text sent to the loop while a child runs is delivered at the next
   loop turn, not to the child.
8. `run_outcomes` with whole-workspace force and no `force_confirmed` returns a
   tool error and starts no child.

**Invariant:** every existing route, endpoint, and test passes unchanged; no
new provider call site (`test_the_only_provider_call_site_in_the_agent_is_the_model_gateway`
still holds); a child run's sidecars are byte-for-byte what a top-level run
would write.
**Measure:** for the three failure classes in the evidence, manual retries
per failed run (target: zero for validation-exhaustion and empty-completion
classes); loop turns and provider tokens per request.
**Rollback:** the engine is additive; remove the dispatch arm and the
`take_action` tool.

---

### Step 5. Operating rules: self-repair, scoping, asking

Builds on 4, 3, and 2. This step is mostly `prompts.LOOP_SYSTEM` plus the
guards that make the prompt unable to overreach.

Rules, in the prompt:

- Read before acting: `get_audit_progress` or `inspect_audit_artifacts` first
  when the request names an artifact or a state.
- Plan before running: `plan_outcomes` before the first `run_outcomes`; if it
  reports blocked capabilities, run the prerequisite or ask, never assume.
- Scope narrowly: name targets whenever the request names them; never widen a
  named request to the workspace.
- After a child ends with anything but `completed`, call `inspect_run`.
  Validation exhaustion: one `rerun_units` with an instruction that restates
  the validator's errors in plain terms. Empty completion: one plain
  `rerun_units`. Blocked or review-required: `ask_auditor` if the question has
  options, else `finish` naming the blocker. Limit reached: `finish` and say
  so.
- Ask at most when it changes what you would do; three questions per request.
- `finish` names what was produced, what was left, and why.

Guards, in code: the per-unit rerun cap in `rerun_units`; `max_child_runs`;
the force refusal in `run_outcomes`; `max_auditor_questions`; a `finish` with
a child still non-terminal is a tool error.

Tests, extending `tests/test_agent_loop.py`: rerun cap enforced on the second
attempt; a scripted loop that skips `plan_outcomes` still cannot run a blocked
outcome (the child materializes nothing and reports the blocker); the
"Condition is empty" scenario from the evidence resolves in one rerun.

**Invariant:** no guard is prompt-only.
**Measure:** fraction of validation-class failures resolved with no person
involved, on a full treasuryfull `findings.drafted` and `tests.specified`
regeneration; auditor questions per request.
**Rollback:** revert the prompt; the guards are harmless without it.

---

### Step 6. Review at the end of a run

Builds on 4.

#### 6a. The loop's closing turn is the review

`finish(summary, suggestions)`: the summary becomes the run's closing agent
message (a loop run does not append `narration.closing_text`; child runs keep
theirs). `suggestions` is validated to at most four entries of
`{"label": str, "requested_outcomes": [...], "target_refs": [...]}` or
`{"label": str, "message": str}`; outcomes must be registered. Persisted on
`run["suggestions"]`.

`assistant_chats.get_chat`: suggestions from the chat's most recent terminal
loop run come first, then `narration.next_steps`. `frontend/src/types.ts`
`AssistantSuggestion` gains optional `message` and `target_refs`;
`ConsoleThread.vue` `nextStep` sends a `message` suggestion as chat text and an
outcome suggestion as today with its target refs in `run_context`.

#### 6b. Review a run that had no loop

A tab-button or slash run that ends with anything but `completed`, and that no
loop run has since inspected (`run["reviewed_by_run_id"]` unset), earns one
suggestion in `get_chat`: `{"label": "Review this run with the agent",
"message": "Review run <id>"}`. The coordinator recognizes the phrase and calls
`take_action`; the loop's first tool call is `inspect_run`. `inspect_run`
stamps `reviewed_by_run_id` on the inspected run.

#### 6c. A stage checkpoint in permission mode

`audit_execution.build_audit_workflow_runner` `before_stage`: when
`run["mode"] == "permission"` and `run["context"].get("review_each_stage")`,
raise an interaction `{"type": "stage_review", "prompt": "<title> is next:
<n> items. Continue, skip, or stop?", "options": ["continue", "skip", "stop"]}`
through `runtime.wait_for_interaction`. `skip` marks the stage and its units
`skipped`; `stop` raises `Cancelled`. `run_outcomes(review_each_stage=True)`
passes the flag; the Run buttons may pass it too.
`frontend/src/components/agent/AgentInteractionCard.vue` already renders
options.

Tests: `tests/test_assistant_chats.py` for suggestion ordering and the review
chip; a new `tests/test_agent_stage_review.py` for continue, skip, and stop.

**Invariant:** a run in auto mode never waits on the checkpoint;
`narration.next_steps` keeps working when no loop has run.
**Measure:** clicks from a finished run to the next started run, from the
telemetry event stream.
**Rollback:** revert; `suggestions` and `reviewed_by_run_id` are additive
fields.

---

### Step 7. The loop as the front door

Builds on 4 and 5. Removes the routing that the evidence shows no one used, and
gives every typed request one path.

#### 7a. The coordinator

`_process_message`: unchanged order of precedence (local slash commands,
pending interactions, slash commands, whole-message phrases), then the
coordinator with read tools, `plan_outcomes`, `start_command`, and
`take_action`. `start_action` is removed from `Commander`.

#### 7b. Routing

`classify_command` keeps: explicit `requested_outcomes`, goal templates,
`LIFECYCLE_PHRASES`, and the `source == "loop"` agent route. Delete
`GENERATION_RULES`, `SCOPE_EXECUTION_RULES`, `TARGET_OPERATION_MARKERS`,
`ISOLATED_OPERATION_MARKERS`, `SPECIFIC_EDIT_VERBS`, `SPECIFIC_TARGET_MARKERS`,
`COMPOUND_SEPARATORS`, `_segments`, `CommandRouter`, `resolve_pending_route`,
`ROUTER_SYSTEM`, `validate_router_result`, and `context_bundles.py`. A text
command that is none of the kept cases routes `agent`; `route.status ==
"pending"` no longer exists, so `dispatch_engine` loses its pending branch and
`resolve_route` always returns an engine. `workflow_owned_request` is kept
until step 8 deletes its only caller.

`steer` after a terminal loop run: the follow-up is a new loop run whose
`conversation_seed` is the previous run's closing message and its child run
ids, not its whole conversation.

`tests/test_agent_routing.py` is rewritten: keep the explicit-outcome,
template, and lifecycle tests that still apply; delete the phrase-table,
compound-clarification, and router-worker tests (a compound request is now the
loop's problem, and it handles one by running two children).

#### 7c. Documentation

`docs/agent-architecture.md` "Routing" is rewritten to two sentences: explicit
outcomes and templates route deterministically to `WorkflowRunner`; text routes
to the agent loop, which requests outcomes through the same validation.
`AGENTS.md` "From command to execution" follows.

**Invariant:** every slash command and whole-message phrase still starts a
workflow run without a model turn; a tab button still names its outcomes.
**Measure:** zero `router_worker` decisions; zero runs that ended "Nothing
needed doing" against a message that asked for work.
**Rollback:** reinstate the deleted tables from history; they are pure
functions with no state.

---

### Step 8. Retire the action engine

Builds on 7. The action catalog stays; its scheduler goes.

- `agent/loop_tools.py` `action_tools()`: one tool per registered action in
  `actions.REGISTRY`, name equal to the action type, parameters equal to its
  argument schema plus `target: {kind, id}`; excluded: `classify_import_batch`,
  `apply_import_batch`, and the `*_procedure` legacy trio. Executing one:
  `ledger.append_actions(run, [action])` on the loop run's own `actions` ledger
  (the record already has `actions`, `rejected_proposals`,
  `target_adjustments`), `actions.canonicalize_action_fields`, approval in
  permission mode through `request_approval` when `actions.approval_required`,
  then `actions._execute` with its snapshot and receipt. `undo_action` stays a
  tool.
- Delete `action_runner.py`, `prompts.COMMAND_INTERPRETER_SYSTEM`,
  `prompts.COMMAND_PLANNER_SYSTEM`, `routing.ROUTE_ACTION` handling,
  `routing.workflow_owned_request`, the `run_action` tool, and
  `store.ACTION_ENGINE`. `RUN_ENGINES` is final at `{workflow, agent, intake}`.
  `tests/test_command_agent.py` loses the interpreter and planner tests and
  keeps the ledger, canonicalization, and executor tests, which move to
  `tests/test_agent_actions.py`.
- `AuditWorkflowExecution` and its siblings inherit from `BaseRunner` directly
  (they inherit from `ActionRunner` today only for construction).

Tests: `ACTION_COVERAGE` still passes through the tool path; a permission-mode
loop that calls `edit_rcm_row` waits on an approval batch;
`test_the_two_schedulers_do_not_import_each_other` becomes a test that
`agent_loop.py` imports no capability module directly (it composes through
`workflow_dispatch` and `loop_tools`).

**Invariant:** every action in the catalog is reachable, approved, receipted,
and undoable as before.
**Measure:** actions executed per loop run; approval waits in permission mode.
**Rollback:** the action engine is one module and two prompts; restore from
history.

---

### Step 9. Delta review for new evidence

Builds on 4 and 3. Row-scoped RCM revision waits on
`docs/rcm-generation-redesign.md` step 3; until then the RCM half regenerates
the matrix under force with the assessment as its instruction.

#### 9a. The capability

`capabilities/planning.py`: `planning.change_assessed`, on the audit registry,
in no template and not in `FULL_AUDIT_OUTCOMES`. Depends on
`documents.analysis_generated`, `planning.apm_ready`, `planning.rcm_ready`.
Readiness: satisfied when `Planning/.delta/<basis_sha1>.json` exists, where the
basis is the sha1 over the named documents' analysis hashes, `apm_sha1`, and
the RCM rows' hashes; `missing` otherwise. Expansion: one proposal-only unit
`change_assessment` with the named `document:` refs as parents; with no
document named the capability expands nothing and reports "name the documents
to assess".

#### 9b. The worker and its context

Preset `planning.delta`: `new_document_analyses` (the named documents'
`Documents/.analysis` sidecars, required, 40,000 characters), `current_apm`
(32,000), `current_rcm` (rows, 40,000), `planning_context`, and the optional
`instruction`. Worker `planning.delta_review`, tag `[agent:delta_review]`,
response contract:

```json
{
  "impact": "none | apm | rcm | both",
  "summary": "one paragraph",
  "apm_changes": [{"section": "APM heading", "change": "...", "reason": "...", "citation": "document id"}],
  "rcm_changes": [{"rcm_id": "R-9 or null", "change": "revise | add | retire", "summary": "...", "reason": "..."}]
}
```

Semantic validator: every `section` is a heading of the workspace APM template;
every non-null `rcm_id` exists; `impact` agrees with the two lists. Executor
`planning.delta`: writes the assessment file under the planning revision;
receipt names it. The Planning tab may show it later; the loop reads it now.

#### 9c. The tool

`assess_change(document_ids)`: `run_outcomes(["planning.change_assessed"],
target_refs=[document:...])`, then returns the assessment. The prompt rule:
impact `apm` or `both` runs `planning.apm_ready` under force with
`apm_changes` rendered as the instruction; `rcm` or `both` runs
`planning.rcm_ready` under force with `rcm_changes` as the instruction, row
scoped once available; permission mode asks first with the summary; `none`
finishes with the summary. Because the RCM depends on the APM, the loop runs
them in that order.

`routes/document_routes.py` already calls `notify_evidence_available` on
upload; `get_chat` adds one suggestion when a document newer than the APM
exists and no assessment covers it: `{"label": "Assess what <file> changes",
"message": "Assess what <file> changes for the APM and RCM"}`.

Tests: `tests/test_agent_planning_worker.py` for the worker with constructed
bundles; `tests/test_agent_capabilities_tests.py` sibling for readiness and
expansion; `tests/test_agent_loop.py` for the full scenario in both modes.

**Invariant:** nothing regenerates without an explicit request; the framework
still never assesses currency on its own, because the assessment is a
requested outcome, not a watch.
**Measure:** on treasuryfull, upload one policy and ask; count APM sections
and RCM rows changed against a whole regeneration's churn.
**Rollback:** the capability is off every template; delete it.

---

## Sequencing

| Step | Depends on | Touches | Risk |
|---|---|---|---|
| 0 | none | gateway, findings readiness | low |
| 1 | none | narration only | low |
| 2 | none | scope, test worker and executor, tests tab | medium |
| 3 | 2 | context model, five presets, five workers, retry API | medium |
| 4 | 0, 3 | new engine, runner, coordinator, chat projection | high |
| 5 | 4, 3, 2 | prompt and guards | low |
| 6 | 4 | chat suggestions, stage checkpoint | low |
| 7 | 4, 5 | routing deletions | medium |
| 8 | 7 | action engine deletion | medium |
| 9 | 4, 3; RCM redesign step 3 for row scope | new capability, worker, executor | medium |

Land them in that order. Steps 0 to 3 are each a day or two and immediately
useful without the loop: a Redraft button that works, a Retry that takes an
instruction, a transcript that stops repeating itself. Step 4 is the only step
that should take a week. Do not start step 7 until step 5 has been measured on
a full treasuryfull regeneration.

## What to amend in the standing documents

- `AGENTS.md` section 3, Engines: three engines; the loop described in one
  paragraph; `RUN_ENGINES` final set updated at step 4 and again at step 8.
- `AGENTS.md` section 3, From command to execution: rewritten at step 7.
- `AGENTS.md` section 4, the "no arbitrary tool loops" convention: replaced at
  step 4 by the bounded-loop statement.
- `docs/agent-architecture.md`: System Flow, Components, Routing, and
  Migration Direction updated at steps 4, 7, and 8. The Context Model section
  gains one sentence at step 3: the auditor's instruction is a declared,
  bounded source with its own permission, and is the only runtime-supplied
  source.

## Open questions

- **Model profile for the loop.** The loop runs on the `agent` profile through
  the gateway. Tool-calling quality of `deepseek-v4-flash` in a multi-turn loop
  is unmeasured; step 4's tests script the model, so the first live run is the
  measurement. If it is poor, the loop's tool set is small enough to drive with
  `tool_choice` forcing on the first turn.
- **Cost.** A loop that reads state on every request spends more than a phrase
  match. `get_audit_progress` is compact; `inspect_audit_artifacts` is not.
  Measure prompt tokens per request at step 5 and cap the read tools' output
  before widening anything.
- **Whether the synchronous coordinator survives.** Step 4 keeps a sync loop for
  questions and escalates for work. If in practice every message escalates,
  fold the coordinator into the loop and accept a run card per question.
- **Steering a child directly.** Step 4 delivers a steering message to the loop
  at its next turn, which is after the child finishes. A child that runs for
  ten minutes cannot be redirected mid-stage. The stage checkpoint of step 6c
  is the first answer; a `stop after this unit` control on the child card is
  the second, and is not in this plan.

"""E5 acceptance: alternate routes survive and only analysis recipes persist."""
import copy
import json

import polars as pl
import pytest

from app import workspaces, analysis_payloads, analysis_results
from app.analysis_inputs import InputWorkspace, validate_input, compact_frame_map, source_refs
from app.agent import alignment_catalog, probes, register, runner, store
from app.agent.capabilities import ANALYSIS_REGISTRY, AUDIT_REGISTRY
from app.agent.context import ContextResolver, analysis_reading_scope
from app.agent.workers import analysis as worker
from app.agent.workers.model import WorkerRequest, WorkerResponseValidationError
from app.agent.analysis_execution import build_analysis_workflow_runner
from app.agent.routing import resolve_route


def table(ws, name, columns):
    ws.add_table(name + ".csv", pl.DataFrame(columns).write_csv().encode())


@pytest.fixture
def mixed():
    ws = workspaces.create_workspace("Mixed links")
    table(ws, "invoices", {"INVOICE_ID": list(range(25)),
        "PO_NUMBER_LINK": [f'PO{i}' for i in range(20)] + [f'REQ{i}' for i in range(5)],
        "AMOUNT": [100.0]*24 + [200.0]})
    table(ws, "orders", {"PO_NUMBER": [f'PO{i}' for i in range(20)], "ORDER_AMOUNT": [100.0]*20})
    table(ws, "requisitions", {"REQ_NUMBER": [f'REQ{i}' for i in range(5)], "REQ_AMOUNT": [100.0]*5})
    return ws


def route(ws, root, right, key=None, depth=1):
    return next(r for r in alignment_catalog.catalog(ws, [t["name"] for t in ws.tables])["recipes"]
                if r["root"] == root and len(r["joins"]) == depth and r["joins"][-1]["right"] == right
                and (key is None or r["joins"][0]["left_on"] == [key]))


def analysis(ws, recipe, other="REQ_AMOUNT"):
    return ws.add_analysis({"title":"Aligned comparison","kind":"analytics","table":recipe["name"],
        "alignment": recipe, "spec":{"test":"compare_columns","params":{"column":"AMOUNT","other":other,"op":"le"}}})


def test_mixed_link_keeps_both_routes_without_durable_joins(mixed):
    cat = alignment_catalog.catalog(mixed, ["invoices","orders","requisitions"])
    req = route(mixed,"invoices","requisitions","PO_NUMBER_LINK")
    po = route(mixed,"invoices","orders","PO_NUMBER_LINK")
    assert req != po
    weak = next(r for r in cat["routes"] if r["left"]=="invoices" and r["right"]=="requisitions"
                and r["left_on"]==["PO_NUMBER_LINK"])
    assert weak["strength"] == "weak"
    assert weak["diagnostics"]["matched_keys"] == 5
    entry = analysis(mixed,req)
    mixed = mixed.reload()
    payload = analysis_payloads.compute_payload(mixed,entry)
    assert payload["error"] is None
    assert (payload["population"], payload["tested"], payload["exception_rows"]) == (25,5,1)
    assert analysis_payloads.analysis_export_frame(mixed, entry).height > 0
    assert mixed.get_frame(entry["table"]).height == 25
    assert mixed.joins == []
    assert entry["table"] not in mixed.table_names()
    assert entry["alignment"] == req


def test_alignment_rejects_changed_lookup_multiplicity(mixed):
    entry=analysis(mixed,route(mixed,"invoices","requisitions","PO_NUMBER_LINK"))
    path=mixed.data_dir / next(t["file"] for t in mixed.tables if t["name"]=="requisitions")
    pl.DataFrame({"REQ_NUMBER":["REQ0","REQ0"],"REQ_AMOUNT":[100.,200.]}).write_csv(path)
    fresh=mixed.reload()
    payload=analysis_payloads.compute_payload(fresh,entry)
    assert "unique" in payload["error"]
    assert not fresh.joins


def test_only_declared_inputs_invalidate_result(mixed):
    recipe=route(mixed,"invoices","requisitions","PO_NUMBER_LINK")
    item=analysis(mixed,recipe)
    before=analysis_results.analysis_input_sha1(mixed,item)
    table(mixed,"unrelated",{"A":[1,2]})
    assert analysis_results.analysis_input_sha1(mixed.reload(),item)==before
    changed=copy.deepcopy(item);changed["alignment"]["joins"][0]["left_on"]=["INVOICE_ID"]
    assert analysis_results.analysis_input_sha1(mixed.reload(),changed)!=before
    python={**item,"kind":"python","spec":{"code":f"result = tables[{recipe["name"]!r}].head(2)"}}
    py_before=analysis_results.analysis_input_sha1(mixed,python)
    table(mixed,"irrelevant",{"B":[3,4]})
    assert analysis_results.analysis_input_sha1(mixed.reload(),python)==py_before
    assert analysis_payloads.compute_payload(mixed,python)["error"] is None
    assert analysis_payloads.analysis_export_frame(mixed,python).height==2


def test_compact_map_reconstructs_real_columns_and_rejects_missing(mixed):
    view=alignment_catalog.view(mixed,["invoices","orders","requisitions"])
    cap=ANALYSIS_REGISTRY.get("analysis.register_ready")
    _,bundle=ContextResolver().resolve(mixed,cap,{"id":"analysis_reading"},analysis_reading_scope(view,view.table_names()))
    request=WorkerRequest(worker_id="analysis.reading",capability_id=cap.id,unit_id="analysis_reading",context=bundle)
    columns=worker._reading_frames(request)
    assert columns
    for name,names in columns.items():
        assert names == set(view.get_frame(name).columns)
    compact=compact_frame_map(view,view.table_names())
    assert all("columns" not in value for name,value in compact.items() if name.startswith("aligned_"))
    target=route(mixed,"invoices","requisitions")["name"]
    with pytest.raises(WorkerResponseValidationError):
        worker.validate_reading_proposal({"keep":[],"decline":[],"unanswerable":[],
            "add":[{"frame":target,"columns":["invented"],"assertion":"a","why":"b"}]},request)


def test_eda_graph_removes_utility_turn_but_audit_keeps_its_graph():
    path=ANALYSIS_REGISTRY.closure(["analysis.executed"])
    assert "data.join_utility_ready" not in path
    assert "data.joins_ready" not in path
    assert path.index("analysis.definitions_ready") < path.index("analysis.inputs_ready") < path.index("analysis.executed")
    assert "data.join_utility_ready" in AUDIT_REGISTRY.closure(["analysis.executed"])
    assert "analysis.inputs_ready" not in AUDIT_REGISTRY.closure(["analysis.executed"])


@pytest.fixture
def authority():
    ws=workspaces.create_workspace("Authority roles")
    table(ws,"requisitions",{"REQ_ID":list(range(55)), "FIN_APPROVED_BY_ID":["S1","S2","S3"]*18+["S1"],
        "REQUESTED_BY_ID":["S2","S3","S1"]*18+["S2"], "AMOUNT":[90.0]*52+[110.0]*3})
    table(ws,"staff",{"STAFF_ID":["S1","S2","S3"], "JOB_TITLE":["Junior","Senior","Manager"],
        "EMPLOYEE_NAME":["Secret Alice","Secret Bob","Secret Carl"]})
    table(ws,"limits",{"JOB_TITLE":["Junior","Senior","Manager"],"LIMIT":[100.0]*3})
    return ws


def test_two_hop_authority_is_nominated_and_keeps_roles_distinct(authority):
    a=route(authority,"requisitions","limits","FIN_APPROVED_BY_ID",depth=2)
    b=route(authority,"requisitions","limits","REQUESTED_BY_ID",depth=2)
    view=alignment_catalog.view(authority,["requisitions","staff","limits"])
    swept={r["name"]:probes.probe_frame(view,r["name"]) for r in (a,b)}
    floor=register.build_floor(view,swept)
    amounts=[n for n in floor if n.test=="compare_columns" and n.params.get("column")=="AMOUNT"
             and n.params.get("other")=="LIMIT"]
    assert len(amounts)==2
    assert len({n.semantic_id for n in amounts})==2
    assert all((n.tested,n.flagged)==(55,3) for n in amounts)
    domains=json.dumps(probes.value_domains(view,a["name"]))
    assert "Secret Alice" not in domains


def test_full_workflow_keeps_floor_with_failed_reading(authority, monkeypatch):
    from app import llm
    from conftest import FakeAgentLLM
    fake=FakeAgentLLM({"agent:analysis_reading":["invalid","invalid"]})
    monkeypatch.setattr(llm,"chat",fake)
    run=store.new_command_run(authority,"auto",{"text":"Run data analysis", "requested_outcomes":["analysis.executed"]})
    assert resolve_route(authority,run)=="workflow"
    execution=build_analysis_workflow_runner(authority,run,runner.RunHandle(authority.id,run["id"]))
    execution.execute()
    fresh=authority.reload()
    assert fresh.joins==[]
    items=[a for a in fresh.analyses if a.get("alignment") and a.get("spec",{}).get("test")=="compare_columns"
           and a["spec"]["params"].get("column")=="AMOUNT" and a["spec"]["params"].get("other")=="LIMIT"]
    assert items, (run.get("warnings"),[(s["capability"],s["status"],[(u["id"],u["error"]) for u in s["units"]]) for s in run["workflow"]["stages"]])
    assert all(a.get("last_result",{}).get("exception_count")==3 for a in items)
    assert all(analysis_results.analysis_result_state(fresh,a)=="current" for a in items)


def test_promoted_code_is_independent_of_saved_analysis(mixed):
    from app.analysis_inputs import carry_input_code, execution_frames
    from app import sandbox
    recipe=route(mixed,"invoices","requisitions","PO_NUMBER_LINK")
    item=analysis(mixed,recipe)
    code=f"result = tables[{recipe["name"]!r}].filter(pl.col('AMOUNT') > pl.col('REQ_AMOUNT'))"
    carried=carry_input_code(mixed,item,code)
    mixed.remove_analysis(item["id"])
    result,_=sandbox.run(carried,{name:mixed.get_frame(name) for name in mixed.table_names()})
    assert result.height==1
    assert not mixed.joins


def test_edited_python_preview_uses_saved_recipe_without_persisting(mixed):
    from app.routes.analyses_routes import preview_analysis
    recipe=route(mixed,"invoices","requisitions","PO_NUMBER_LINK")
    item=mixed.add_analysis({"title":"Python input","kind":"python","table":recipe["name"],
        "alignment":recipe,"spec":{"code":f"result = tables[{recipe["name"]!r}].head(2)"}})
    before=mixed.reload().revision
    payload=preview_analysis(mixed.id,item["id"],{"spec":{"code":f"result = tables[{recipe["name"]!r}].head(4)"}})
    assert payload["error"] is None and payload["total_rows"]==4
    assert mixed.reload().revision==before
    assert mixed.reload()._analysis(item["id"])["spec"]==item["spec"]


def test_zero_match_route_retains_its_orphan_evidence():
    ws=workspaces.create_workspace("Orphans")
    table(ws,"ledger",{"CUSTOMER_ID":["X1","X2"],"AMOUNT":[1.,2.]})
    table(ws,"customers",{"CUSTOMER_ID":["C1","C2"],"NAME":["private a","private b"]})
    cat=alignment_catalog.catalog(ws,["ledger","customers"])
    candidate=next(r for r in cat["routes"] if r["left"]=="ledger")
    assert candidate["strength"]=="weak"
    assert candidate["diagnostics"]["matched_keys"]==0
    assert candidate["diagnostics"]["unmatched_keys"]==2
    assert route(ws,"ledger","customers")


def test_chain_uses_the_dimension_column_after_a_name_collision(authority):
    frame=authority.get_frame("requisitions").with_columns(pl.lit("WRONG").alias("JOB_TITLE"))
    path=authority.data_dir / next(t["file"] for t in authority.tables if t["name"]=="requisitions")
    frame.write_csv(path)
    authority=authority.reload()
    recipe=route(authority,"requisitions","limits","FIN_APPROVED_BY_ID",depth=2)
    assert recipe["joins"][1]["left_on"]==["JOB_TITLE_right"]
    result=InputWorkspace(authority,[recipe]).get_frame(recipe["name"])
    assert result["LIMIT"].is_not_null().sum()==55


def test_saved_alignment_keeps_population_provenance_after_reload(mixed):
    from app.agent import joins
    recipe=route(mixed,"invoices","requisitions","PO_NUMBER_LINK")
    item=analysis(mixed,recipe)
    ws=mixed.reload()
    assert joins.frame_root(ws,item["table"])=="invoices"
    assert joins.frame_grain(ws,item["table"])=="invoices"
    assert joins.frame_lineage(ws,item["table"])=={"invoices","requisitions"}
    assert joins.column_origins(ws,item["table"])["REQ_AMOUNT"]=="requisitions"
    assert "PO_NUMBER_LINK" in joins.frame_route(ws,item["table"])["requisitions"]


def test_legacy_result_fingerprint_is_unchanged(mixed):
    import hashlib
    item={"kind":"analytics","table":"invoices","spec":{"test":"duplicates","params":{"columns":["INVOICE_ID"]}}}
    prior={"kind":item["kind"],"spec":item["spec"],"outcome_policy":{},
           "inputs":{"invoices":mixed._table_signature("invoices")}}
    expected=hashlib.sha1(json.dumps(prior,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
    assert analysis_results.analysis_input_sha1(mixed,item)==expected


def test_dependency_descriptors_cannot_be_selected_as_assertion_targets(mixed):
    view = alignment_catalog.view(mixed, [t["name"] for t in mixed.tables])
    target = route(mixed, "invoices", "requisitions")["name"]
    capability = ANALYSIS_REGISTRY.get("analysis.register_ready")
    _, bundle = ContextResolver().resolve(
        mixed, capability, {"id": "reading"}, analysis_reading_scope(view, [target])
    )
    request = WorkerRequest(
        worker_id="analysis.reading", capability_id=capability.id,
        unit_id="reading", context=bundle,
    )
    assert worker._reading_frames(request) == {target: set(view.get_frame(target).columns)}
    with pytest.raises(WorkerResponseValidationError):
        worker.validate_reading_proposal(
            {"keep": [], "decline": [], "unanswerable": [], "add": [
                {"frame": "invoices", "columns": ["AMOUNT"],
                 "assertion": "Check invoice amount", "why": "dependency only"}
            ]}, request,
        )


def test_result_cannot_commit_after_an_alignment_source_changes(mixed):
    from app.agent.executors import analysis as executors
    from app.agent.executors.model import ExecutorRequest
    from app.workspace_transactions import ParentConflict, parent_hashes

    item = analysis(mixed, route(mixed, "invoices", "requisitions", "PO_NUMBER_LINK"))
    computed = analysis_results.execute_analysis(mixed, item, run_id="input-conflict")
    request = ExecutorRequest(
        executor_id=executors.EXECUTION_EXECUTOR_ID,
        capability_id="analysis.executed", unit_id="execution",
        proposal={"result": computed.result}, expected_revision=mixed.revision,
        expected_parents=parent_hashes(mixed, [f"analysis:{item['id']}"]),
    )
    target = executors.AnalysisExecutionExecutorTarget(mixed, "input-conflict", item["id"])
    path = mixed.data_dir / next(t["file"] for t in mixed.tables if t["name"] == "requisitions")
    mixed.get_frame("requisitions").with_columns(pl.lit(500.0).alias("REQ_AMOUNT")).write_csv(path)

    with pytest.raises(ParentConflict, match="analysis_input"):
        executors.execute_analysis_run(request, target)
    assert not mixed.reload()._analysis(item["id"]).get("last_result")


def test_definition_reconciliation_checks_every_recipe_source(mixed):
    from app.agent.executors import analysis as executors
    from app.agent.executors.model import ExecutorRequest
    from app.workspace_transactions import parent_hashes

    recipe = route(mixed, "invoices", "requisitions", "PO_NUMBER_LINK")
    refs = source_refs(recipe)
    request = ExecutorRequest(
        executor_id=executors.DEFINITIONS_EXECUTOR_ID,
        capability_id="analysis.definitions_ready", unit_id="definition",
        proposal={"analyses": [{
            "title": "Authority", "kind": "analytics", "semantic_id": "authority",
            "table": recipe["name"], "spec": {"test": "compare_columns", "params": {
                "column": "AMOUNT", "other": "REQ_AMOUNT", "op": "le",
            }},
        }]},
        expected_revision=mixed.revision, expected_parents=parent_hashes(mixed, refs),
    )
    target = executors.AnalysisDefinitionExecutorTarget(
        mixed, "definition-conflict", recipe["name"], refs[0], alignment=recipe,
    )
    # Alter the second guarded source; checking only the first would miss it.
    path = mixed.data_dir / next(t["file"] for t in mixed.tables if t["name"] == "requisitions")
    mixed.get_frame("requisitions").with_columns(pl.lit(500.0).alias("REQ_AMOUNT")).write_csv(path)
    reconciled = executors.reconcile_analysis_definitions(request, target)
    assert reconciled.disposition == "conflict"
    assert "table:requisitions" in reconciled.reason

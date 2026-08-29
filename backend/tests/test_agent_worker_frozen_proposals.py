"""Every semantic validator must accept the frozen proposal it is really given.

``ResponseSchema.validate`` returns ``_frozen_json(...)``, which turns every
array into a tuple, and the registry hands *that* to the semantic validator. A
validator written against ``json.loads`` output therefore sees a shape no test
that calls it directly ever produces — and ``isinstance(value, list)`` is False
for every array the model could possibly send.

That is not hypothetical. A live 27-row RCM was discarded whole because nine
rows carried the citations they were asked for: ``criteria_refs`` arrived as a
tuple, the row gate rejected each one, and the worker's own quarantine pass —
which runs on plain ``json.loads`` output — could not see the failure it was
built to absorb. Forty-nine passing tests, every one of them plain dicts.

So each case here runs one worker's happy path through the validator twice, in
both shapes, and requires the same answer. ``test_every_registered_semantic_
validator_is_covered`` is what keeps the set honest: a new worker cannot join
the registry without joining this guard.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from app.agent.workers import WORKERS
from app.agent.workers.model import _frozen_json
from test_analysis_reading_worker import reading_request  # noqa: F401


def _plain(value: object) -> object:
    """Frozen containers back to the plain JSON shapes assertions compare."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


# ``worker_id`` -> ``builder(getfixture) -> (proposal, request)``, where
# ``getfixture`` is ``pytest``'s own accessor for cases whose bundle already
# exists as a fixture in the module that owns it.
# The proposal must be a happy path: this guard is about shape, so a case that
# rejects in both shapes agrees for the wrong reason and proves nothing.
BUILDERS: dict[str, object] = {}


def builds(worker_id: str):
    def register(builder):
        BUILDERS[worker_id] = builder
        return builder

    return register


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #


@builds("planning.rcm")
def _rcm(getfixture):
    from test_agent_planning_rcm_worker import (
        MATRIX_SUMMARY,
        SOP_SUMMARY,
        _document_bundle,
        _request,
        _row,
    )

    request = _request(
        _document_bundle(("d_sop", SOP_SUMMARY), ("d_matrix", MATRIX_SUMMARY))
    )
    # The field the live failure was about: a row that cites nothing takes an
    # early return and never reaches the check that rejected the real matrix.
    row = _row(criteria_refs=[{"document": 1, "citations": ["C4", "C7"]}])
    return {"rows": [row]}, request


@builds("planning.context")
def _planning_context(getfixture):
    from test_agent_planning_context_worker import _bundle

    return {"context": {"objective": "Assess approvals"}}, _bundle()


# --------------------------------------------------------------------------- #
# fieldwork
# --------------------------------------------------------------------------- #


@builds("fieldwork.document_qa")
def _document_qa(getfixture):
    from app.agent.workers.model import WorkerRequest
    from test_agent_fieldwork_execution import CAPABILITY_ID, _qa_bundle, _qa_workspace

    ws, test, item_id, document_id = _qa_workspace()
    unit, bundle = _qa_bundle(ws, test, item_id, document_id)
    request = WorkerRequest(
        worker_id="fieldwork.document_qa",
        capability_id=CAPABILITY_ID,
        unit_id=unit["id"],
        context=bundle,
    )
    proposal = {
        "answer": "The controller approved it.",
        "conclusion": "The controller approved it.",
        "outcome": "accepted",
        "citations": [{"page": 1, "excerpt": "The purchase order was approved"}],
    }
    return proposal, request


# --------------------------------------------------------------------------- #
# intake
# --------------------------------------------------------------------------- #


@builds("intake.classification")
def _classification(getfixture):
    from app.agent.workers.intake import STAGED_FILE_SOURCE_ID

    class _Item:
        source_id = STAGED_FILE_SOURCE_ID
        content = {"id": "itm_1", "relative_path": "Audit/policy.txt"}

    class _Request:
        context = type("Bundle", (), {"items": (_Item(),)})()

    proposal = {
        "items": [
            {
                "id": "itm_1",
                "route": "document",
                "document_category": "policy",
                "confidence": "high",
            }
        ]
    }
    return proposal, _Request()


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #


@builds("analysis.join_utility")
def _join_utility(getfixture):
    from test_agent_join_utility_worker import _request, _retain

    return {"decisions": [_retain()]}, _request()


@builds("analysis.reading")
def _reading(getfixture):
    proposal = {
        "keep": [],
        "add": [
            {
                "frame": "invoices",
                "columns": ["invoice_no"],
                "assertion": "invoices carries no repeated invoice_no.",
                "why": "A reused key is the shape a double posting takes.",
            }
        ],
        "decline": [],
        "unanswerable": [],
    }
    return proposal, getfixture("reading_request")


@builds("analysis.promotion")
def _promotion(getfixture):
    from test_analysis_promotion import (
        PYTHON_CODE,
        _flagging_analysis,
        _workspace,
        _worker_request,
    )

    ws = _workspace()
    analysis = _flagging_analysis(ws)
    proposal = {
        "promote": True,
        "rcm_id": ws.rcm[0]["id"],
        "title": "Payments above the approved commitment",
        "objective": "Determine whether payments exceeded the approved amount.",
        "step": {
            "label": "Compare",
            "instruction": "Compare each payment to its approved amount.",
            "population": "transactions",
            "code": PYTHON_CODE,
        },
    }
    return proposal, _worker_request(ws, analysis["id"])


@builds("analysis.definitions")
def _definitions(getfixture):
    from app.agent.capabilities import ANALYSIS_REGISTRY
    from app.agent.context import ContextResolver, analysis_definition_scope
    from app.agent.workers.model import WorkerRequest

    ws = getfixture("workspace_with_data")
    capability = ANALYSIS_REGISTRY.get("analysis.definitions_ready")
    _manifest, bundle = ContextResolver().resolve(
        ws,
        capability,
        {"id": "analysis_definitions:transactions"},
        analysis_definition_scope(ws, "transactions"),
    )
    request = WorkerRequest(
        worker_id="analysis.definitions",
        capability_id=capability.id,
        unit_id="analysis_definitions:transactions",
        context=bundle,
    )
    proposal = {
        "analyses": [
            {
                "title": "Outlier test",
                "kind": "analytics",
                # A `columns` parameter: the list-valued shape the spec walker
                # reads positionally, and the one a freeze would empty.
                "spec": {"test": "duplicates", "params": {"columns": ["invoice_no"]}},
                "note": "Reused keys signal double postings.",
            }
        ]
    }
    return proposal, request


@builds("analysis.summary")
def _summary(getfixture):
    from app import workspaces
    from test_workflow_analysis import (
        DUPLICATES,
        _payload,
        _ref_of,
        _saved,
        _summary_request,
    )

    ws = getfixture("workspace_with_data")
    analysis = _saved(ws, "Duplicates", DUPLICATES)
    request = _summary_request(workspaces.load_workspace(ws.id))
    ref = _ref_of(request, analysis["id"])
    proposal = _payload(
        findings=[{"prose": f"Invoices are duplicated ([#{ref}]).", "procedures": [ref]}]
    )
    return proposal, request


# --------------------------------------------------------------------------- #
# reporting, tests, and the rest of planning
# --------------------------------------------------------------------------- #


@builds("planning.apm")
def _apm(getfixture):
    from test_agent_planning_worker import _request

    proposal = {
        "apm_markdown": (
            "# Engagement\n\nAssess procurement approvals.\n\n"
            "# Scope\n\nPurchase commitments.\n"
        )
    }
    return proposal, _request()


@builds("reporting.finding")
def _finding(getfixture):
    from test_agent_reporting_finding import _draft, _request

    return {"finding": _draft()}, _request()


@builds("tests.generate")
def _generate(getfixture):
    from test_agent_tests_generate_worker import _data_test, _request

    return {"tests": [_data_test()]}, _request()


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #

CHUNK_TEXT = (
    "The purchase order was approved by the controller on 3 March, and the "
    "invoice was matched to the goods received note before payment."
)
EXCERPT = "approved by the controller on 3 March"
SOURCE_SHA1 = "0" * 40


def _documents_request(worker_id, capability_id, *supplied):
    """A bundle carrying exactly the document sources one worker declares."""
    from app.agent.context import (
        ContextBundle,
        ContextBundleItem,
        ContextRepresentation,
        supplied_size,
        total_supplied_size,
    )
    from app.agent.workers.model import WorkerRequest

    items = tuple(
        ContextBundleItem(
            source_id=source_id,
            source_ref=source_ref,
            representation=ContextRepresentation(representation),
            content=content,
            supplied_size=supplied_size(content),
        )
        for source_id, source_ref, representation, content in supplied
    )
    return WorkerRequest(
        worker_id=worker_id,
        capability_id=capability_id,
        unit_id="document_analysis:d1",
        context=ContextBundle(
            capability_id=capability_id,
            unit_id="document_analysis:d1",
            items=items,
            supplied_size=total_supplied_size(item.supplied_size for item in items),
        ),
    )


@builds("documents.classification")
def _classification(getfixture):
    from app.agent.workers.documents import DOCUMENT_CLASSIFICATION_SOURCE_ID
    from app.agent.context import (
        ContextBundle,
        ContextBundleItem,
        ContextRepresentation,
        supplied_size,
        total_supplied_size,
    )
    from app.agent.workers.model import WorkerRequest

    content = {"document_id": "d1", "text": CHUNK_TEXT}
    items = (
        ContextBundleItem(
            source_id=DOCUMENT_CLASSIFICATION_SOURCE_ID,
            source_ref="document:d1",
            representation=ContextRepresentation("document_metadata"),
            content=content,
            supplied_size=supplied_size(content),
        ),
    )
    request = WorkerRequest(
        worker_id="documents.classification",
        capability_id="documents.types_classified",
        unit_id="document_classification:d1",
        context=ContextBundle(
            capability_id="documents.types_classified",
            unit_id="document_classification:d1",
            items=items,
            supplied_size=total_supplied_size(item.supplied_size for item in items),
        ),
        unit_input={
            "document_id": "d1",
            "text": CHUNK_TEXT,
            "selectable_types": ["purchase_order", "vendor_invoice", "other"],
        },
    )
    proposal = {
        "document_type": "vendor_invoice",
        "document_type_other": "",
        "confidence": "high",
        "rationale": "The header reads Invoice and states an invoice number.",
    }
    return proposal, request


def _schema_request(worker_id: str, unit_input: dict):
    from app.agent.context import (
        ContextBundle,
        ContextBundleItem,
        ContextRepresentation,
        supplied_size,
        total_supplied_size,
    )
    from app.agent.workers.documents import DOCUMENT_SCHEMA_SOURCE_ID
    from app.agent.workers.model import WorkerRequest

    content = {"document_id": "d1", "text": CHUNK_TEXT}
    items = (
        ContextBundleItem(
            source_id=DOCUMENT_SCHEMA_SOURCE_ID,
            source_ref="document:d1:schema-sample",
            representation=ContextRepresentation("raw_pages"),
            content=content,
            supplied_size=supplied_size(content),
        ),
    )
    return WorkerRequest(
        worker_id=worker_id,
        capability_id="documents.schemas_induced",
        unit_id="document_schema:vendor_invoice",
        context=ContextBundle(
            capability_id="documents.schemas_induced",
            unit_id="document_schema:vendor_invoice",
            items=items,
            supplied_size=total_supplied_size(item.supplied_size for item in items),
        ),
        unit_input=unit_input,
    )


@builds("documents.schema_sample")
def _schema_sample(getfixture):
    request = _schema_request(
        "documents.schema_sample",
        {"document_type": "vendor_invoice", "document_id": "d1", "text": CHUNK_TEXT},
    )
    proposal = {
        "fields": [
            {
                "name": "invoice_number", "role": "identifier",
                "value_type": "identifier", "cardinality": "one",
                "verbatim": True, "confidence": "high", "label": "Invoice number",
            },
            {
                "name": "total_amount", "role": "attribute",
                "value_type": "number", "cardinality": "one",
                "verbatim": True, "confidence": "high", "label": "Total",
            },
        ]
    }
    return proposal, request


@builds("documents.schema_reconcile")
def _schema_reconcile(getfixture):
    request = _schema_request(
        "documents.schema_reconcile",
        {
            "document_type": "vendor_invoice",
            "conflicts": [
                {"name": "reference", "attribute": "value_type",
                 "values": ["identifier", "text"]},
            ],
        },
    )
    proposal = {
        "resolutions": [
            {
                "name": "reference", "attribute": "value_type",
                "value": "identifier",
                "reason": "It ties this invoice to the order it bills against.",
            }
        ]
    }
    return proposal, request


@builds("documents.analysis_structured")
def _structured(getfixture):
    from app.agent.workers.documents import DOCUMENT_STRUCTURED_SOURCE_ID
    from app.agent.context import (
        ContextBundle,
        ContextBundleItem,
        ContextRepresentation,
        supplied_size,
        total_supplied_size,
    )
    from app.agent.workers.model import WorkerRequest

    content = {"id": "ch1", "page": 1, "text": CHUNK_TEXT}
    items = (
        ContextBundleItem(
            source_id=DOCUMENT_STRUCTURED_SOURCE_ID,
            source_ref="document:d1:chunk:ch1",
            representation=ContextRepresentation("raw_pages"),
            content=content,
            supplied_size=supplied_size(content),
        ),
    )
    request = WorkerRequest(
        worker_id="documents.analysis_structured",
        capability_id="documents.analysis_chunks_ready",
        unit_id="document_chunk:d1:ch1",
        context=ContextBundle(
            capability_id="documents.analysis_chunks_ready",
            unit_id="document_chunk:d1:ch1",
            items=items,
            supplied_size=total_supplied_size(item.supplied_size for item in items),
        ),
        unit_input={
            "document_id": "d1",
            "document_type": "vendor_invoice",
            "schema_fields": [
                {"name": "invoice_number", "role": "identifier",
                 "value_type": "identifier", "cardinality": "one",
                 "verbatim": True, "confidence": "high"},
            ],
        },
    )
    proposal = {
        "analysis_profile": "structured",
        "records": [
            {
                "fields": [
                    {"name": "invoice_number", "entry": 1,
                     "value": "INV-1042", "citation": "c1"},
                ],
                "additional_fields": [],
            }
        ],
        "audit_notes": [],
        "citations": [{"id": "c1", "page": 1, "excerpt": EXCERPT}],
    }
    return proposal, request


@builds("tests.cycle_linkage")
def _cycle_linkage(getfixture):
    from app.agent.workers.tests import CYCLE_SCHEMA_SOURCE_ID
    from app.agent.context import (
        ContextBundle,
        ContextBundleItem,
        ContextRepresentation,
        supplied_size,
        total_supplied_size,
    )
    from app.agent.workers.model import WorkerRequest

    schemas = [
        {
            "document_type": "vendor_invoice",
            "fields": [
                {"name": "invoice_number", "role": "identifier",
                 "value_type": "identifier", "cardinality": "one"},
                {"name": "order_number", "role": "identifier",
                 "value_type": "identifier", "cardinality": "one"},
                {"name": "total_amount", "role": "attribute",
                 "value_type": "number", "cardinality": "one"},
            ],
        },
        {
            "document_type": "purchase_order",
            "fields": [
                {"name": "order_number", "role": "identifier",
                 "value_type": "identifier", "cardinality": "one"},
                {"name": "total_amount", "role": "attribute",
                 "value_type": "number", "cardinality": "one"},
            ],
        },
    ]
    content = {"schemas": schemas}
    items = (
        ContextBundleItem(
            source_id=CYCLE_SCHEMA_SOURCE_ID,
            source_ref="workspace:schemas",
            representation=ContextRepresentation("current_artifact"),
            content=content,
            supplied_size=supplied_size(content),
        ),
    )
    request = WorkerRequest(
        worker_id="tests.cycle_linkage",
        capability_id="tests.cycle_ruleset_proposed",
        unit_id="cycle_linkage:ws",
        context=ContextBundle(
            capability_id="tests.cycle_ruleset_proposed",
            unit_id="cycle_linkage:ws",
            items=items,
            supplied_size=total_supplied_size(item.supplied_size for item in items),
        ),
        unit_input={"schemas": schemas, "tables": []},
    )
    proposal = {
        "cycle_label": "Procure to pay",
        "roles": [
            {"name": "invoice", "document_type": "vendor_invoice",
             "cardinality": "one", "required": True},
            {"name": "order", "document_type": "purchase_order",
             "cardinality": "one", "required": True},
        ],
        "anchor": {"table": "register", "column": "INVOICE_NO",
                   "role": "invoice", "field": "invoice_number"},
        "join_keys": [{
            "id": "jk_order", "match": "normalized_equal",
            "left": {"role": "invoice", "field": "order_number"},
            "right": {"role": "order", "field": "order_number"},
            "rationale": "An invoice cites the order it bills against.",
        }],
        "assertions": [{
            "id": "as_total", "label": "Totals agree",
            "left": {"role": "invoice", "field": "total_amount"},
            "right": {"role": "order", "field": "total_amount"},
            "operator": "numeric_within", "tolerance": {"absolute": 1},
            "rationale": "The amount billed must be the amount ordered.",
        }],
    }
    return proposal, request


def _text_proposal(**overrides):
    value = {
        "summary_markdown": "The order was approved and matched before payment.",
        "audit_notes_markdown": "Approval precedes payment on this voucher.",
        "citations": [{"id": "1", "page": 1, "excerpt": EXCERPT}],
    }
    value.update(overrides)
    return value


@builds("documents.analysis_chunk")
def _chunk(getfixture):
    from app.agent.workers.documents import (
        DOCUMENT_CHUNK_SOURCE_ID,
        DOCUMENT_METADATA_SOURCE_ID,
    )

    request = _documents_request(
        "documents.analysis_chunk",
        "documents.analysis_chunks_ready",
        (
            DOCUMENT_CHUNK_SOURCE_ID,
            "document:d1#ch1",
            "document_chunk",
            {"id": "ch1", "pages": [1], "text": CHUNK_TEXT},
        ),
        (
            DOCUMENT_METADATA_SOURCE_ID,
            "document:d1",
            "document_metadata",
            {"document_id": "d1", "source_sha1": SOURCE_SHA1},
        ),
    )
    return _text_proposal(), request


@builds("documents.analysis_reduction")
def _reduction(getfixture):
    from app.agent.workers.documents import CHUNK_ANALYSES_SOURCE_ID

    analysis = {
        "chunk_id": "ch1",
        "derived_text_markdown": CHUNK_TEXT,
        "summary_markdown": "The order was approved.",
        "audit_notes_markdown": "Approval precedes payment.",
        "citations": [
            {
                "id": "1",
                "page": 1,
                "excerpt": EXCERPT,
                "source_sha1": SOURCE_SHA1,
            }
        ],
    }
    request = _documents_request(
        "documents.analysis_reduction",
        "documents.analysis_generated",
        (
            CHUNK_ANALYSES_SOURCE_ID,
            "document:d1#ch1",
            "document_chunk",
            analysis,
        ),
    )
    proposal = {
        "summary_markdown": "The order was approved and matched before payment.",
        "audit_notes_markdown": "Approval precedes payment on this voucher.",
    }
    return proposal, request


@builds("documents.analysis_visual_page")
def _visual(getfixture):
    from app.agent.workers.documents import (
        DOCUMENT_METADATA_SOURCE_ID,
        DOCUMENT_VISUAL_SOURCE_ID,
    )

    request = _documents_request(
        "documents.analysis_visual_page",
        "documents.analysis_chunks_ready",
        (
            DOCUMENT_VISUAL_SOURCE_ID,
            "document:d1#p1",
            "document_page_image",
            {"page": 1, "tile_order": 1, "media_id": "m1"},
        ),
        (
            DOCUMENT_METADATA_SOURCE_ID,
            "document:d1",
            "document_metadata",
            {"document_id": "d1", "source_sha1": SOURCE_SHA1},
        ),
    )
    proposal = {
        "summary_markdown": "The scanned page shows an approved order.",
        "audit_notes_markdown": "The approval signature is present.",
        "transcription_markdown": CHUNK_TEXT,
        "citations": [
            {
                "kind": "visual",
                "page": 1,
                "tile_order": 1,
                "description": "The approval signature block.",
            }
        ],
    }
    return proposal, request


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("worker_id", sorted(BUILDERS))
def test_semantic_validator_agrees_across_proposal_shapes(worker_id, request):
    proposal, worker_request = BUILDERS[worker_id](request.getfixturevalue)
    validator = WORKERS.get(worker_id).semantic_validator

    plain = validator(_plain(proposal), worker_request)
    frozen = validator(_frozen_json(_plain(proposal)), worker_request)

    assert _plain(frozen) == _plain(plain)


def test_every_registered_semantic_validator_is_covered():
    registered = {
        definition.worker_id
        for definition in WORKERS.all()
        if definition.semantic_validator is not None
    }

    assert set(BUILDERS) == registered

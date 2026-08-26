"""Public runtime contracts shared by agent schedulers."""

from .model_gateway import (
    DefaultModelGateway,
    ModelCapabilityError,
    ModelGateway,
    ModelResponseUnusable,
    PreparedMediaError,
    VisionRequestRejected,
)
from .interactions import submit_approval_response, submit_interaction_response
from .run_runtime import Cancelled, DefaultRunRuntime, LimitExceeded, RunRuntime
from .unit_pipeline import (
    ProposalExecutionIdentity,
    UnitPipeline,
    UnitPipelineConflict,
    UnitPipelineError,
    UnitPipelineOutcome,
    UnitPipelineRequest,
    UnitSidecarStore,
    UnitSidecarValidationError,
)
from .workflow_runner import (
    BoundUnitPipeline,
    CapabilityExecution,
    CapabilityExecutionRegistry,
    DeterministicUnitResult,
    FinishProjection,
    WorkflowRunner,
    first_unit_error,
    fold_terminal_status,
    unsettled_capabilities,
)

__all__ = [
    "Cancelled",
    "DefaultModelGateway",
    "DefaultRunRuntime",
    "LimitExceeded",
    "ModelCapabilityError",
    "ModelResponseUnusable",
    "ModelGateway",
    "PreparedMediaError",
    "RunRuntime",
    "ProposalExecutionIdentity",
    "submit_approval_response",
    "submit_interaction_response",
    "UnitPipeline",
    "UnitPipelineConflict",
    "UnitPipelineError",
    "UnitPipelineOutcome",
    "UnitPipelineRequest",
    "UnitSidecarStore",
    "UnitSidecarValidationError",
    "FinishProjection",
    "BoundUnitPipeline",
    "CapabilityExecution",
    "CapabilityExecutionRegistry",
    "DeterministicUnitResult",
    "WorkflowRunner",
    "VisionRequestRejected",
]

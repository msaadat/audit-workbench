"""Public model-worker contracts and registry."""

from .model import (
    MAX_REPAIR_ATTEMPTS,
    WORKERS,
    WorkerAttempt,
    WorkerContractError,
    WorkerDefinition,
    WorkerRegistry,
    WorkerRepairPolicy,
    WorkerRepairSeed,
    WorkerRequest,
    WorkerResponseSchema,
    WorkerResponseValidationError,
    WorkerResult,
    WorkerRunError,
)
from .planning import (
    APM_RESPONSE_SCHEMA,
    APM_SYSTEM,
    APM_WORKER,
    APM_WORKER_ID,
)
# Importing the grouped worker modules is what registers them in ``WORKERS``.
from . import analysis as analysis  # noqa: F401
from . import documents as documents  # noqa: F401
from . import fieldwork as fieldwork  # noqa: F401
from . import intake as intake  # noqa: F401
from . import reporting as reporting  # noqa: F401
from . import tests as tests  # noqa: F401

__all__ = [
    "MAX_REPAIR_ATTEMPTS",
    "WORKERS",
    "APM_RESPONSE_SCHEMA",
    "APM_SYSTEM",
    "APM_WORKER",
    "APM_WORKER_ID",
    "WorkerAttempt",
    "WorkerContractError",
    "WorkerDefinition",
    "WorkerRegistry",
    "WorkerRepairPolicy",
    "WorkerRepairSeed",
    "WorkerRequest",
    "WorkerResponseSchema",
    "WorkerResponseValidationError",
    "WorkerResult",
    "WorkerRunError",
]

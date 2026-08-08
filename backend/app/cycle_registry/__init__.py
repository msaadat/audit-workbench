"""Generic transaction-evidence registry and built-in cycle packs."""

from __future__ import annotations

from . import common
from .models import *  # noqa: F403 - public registry contract
from .packs import payroll, procure_to_pay
from .registry import CycleRegistry, RegistryError, canonical_sha256

DEFAULT_REGISTRY = CycleRegistry(
    normalizers=common.NORMALIZERS,
    identifier_kinds=(
        *common.IDENTIFIER_KINDS,
        *procure_to_pay.IDENTIFIER_KINDS,
        *payroll.IDENTIFIER_KINDS,
    ),
    field_kinds=(
        *common.FIELD_KINDS,
        *procure_to_pay.FIELD_KINDS,
        *payroll.FIELD_KINDS,
    ),
    record_kinds=(
        *common.RECORD_KINDS,
        *procure_to_pay.RECORD_KINDS,
        *payroll.RECORD_KINDS,
    ),
    evidence_kinds=common.EVIDENCE_KINDS,
    packs=(procure_to_pay.PACK, payroll.PACK),
)

__all__ = [
    "CyclePackDefinition",
    "CycleRegistry",
    "DEFAULT_REGISTRY",
    "DateLifecycleStage",
    "EvidenceKindDefinition",
    "FieldAttributeDefinition",
    "FieldKindDefinition",
    "IdentifierKindDefinition",
    "NormalizerDefinition",
    "RecordKindDefinition",
    "RegistryReference",
    "RegistryError",
    "canonical_sha256",
]

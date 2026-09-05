"""Normalized context declarations, manifests, and local worker bundles.

These models are deliberately behavior-free and declaration-only.  Context
policy is authored in registered application capability or preset definitions;
there is no per-run or per-workspace auditor override in this contract.  The
JSON contracts keep declarations and execution records stable while making the
privacy boundary explicit: manifests contain only references, hashes, metrics,
and decisions; raw supplied content exists only in :class:`ContextBundle`.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Self


ROW_LEVEL_TABLE_REPRESENTATIONS = frozenset({"table_rows"})
PREPARED_MEDIA_HANDLE_FIELDS = frozenset(
    {
        "cache_key",
        "source_ref",
        "source_sha1",
        "prepared_sha256",
        "page",
        "frame",
        "variant",
        "tile_order",
        "mime",
        "width",
        "height",
        "prepared_byte_count",
        "pixel_count",
        "policy_hash",
        "prepared_set_hash",
    }
)


def _reject_row_level_table_representation(
    representation: "ContextRepresentation",
    field_name: str,
) -> None:
    if representation.kind in ROW_LEVEL_TABLE_REPRESENTATIONS:
        raise ValueError(
            f"{field_name} cannot use row-level table representation "
            f"'{representation.kind}'."
        )


def _normalized_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string.")
    return text


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return value


def _json_value(value: object, field_name: str) -> Any:
    """Return a detached, deterministically ordered JSON-compatible value."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must not contain non-finite numbers.")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{field_name} object keys must be strings.")
        return {
            key: _json_value(value[key], f"{field_name}.{key}")
            for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, f"{field_name}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ValueError(f"{field_name} must contain only JSON-compatible values.")


def _object_payload(
    value: object,
    field_name: str,
    *,
    fields: set[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object.")
    unknown = sorted(set(value) - fields)
    if unknown:
        raise ValueError(f"Unknown {field_name} field '{unknown[0]}'.")
    return dict(value)


class _JSONModel:
    """Small explicit JSON contract shared by the context dataclasses."""

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        raise NotImplementedError

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, value: str) -> Self:
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"{cls.__name__} JSON is invalid.") from error
        return cls.from_dict(payload)


@dataclass(frozen=True)
class ContextRepresentation(_JSONModel):
    """One permitted representation of a declared context source."""

    kind: str
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _normalized_text(self.kind, "representation.kind"))
        options = _json_value(self.options, "representation.options")
        if not isinstance(options, dict):
            raise ValueError("representation.options must be an object.")
        object.__setattr__(self, "options", options)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "options": _json_value(self.options, "representation.options")}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "representation",
            fields={"kind", "options"},
        )
        return cls(
            kind=payload.get("kind"),
            options=payload.get("options") or {},
        )


@dataclass(frozen=True)
class ContextBudget(_JSONModel):
    """Independent hard limits for text and prepared media."""

    max_items: int
    max_characters: int
    max_estimated_tokens: int | None = None
    max_media_items: int | None = None
    max_media_bytes: int | None = None
    max_media_pixels: int | None = None
    max_estimated_image_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_items", _positive_int(self.max_items, "budget.max_items"))
        object.__setattr__(
            self,
            "max_characters",
            _positive_int(self.max_characters, "budget.max_characters"),
        )
        if self.max_estimated_tokens is not None:
            object.__setattr__(
                self,
                "max_estimated_tokens",
                _positive_int(
                    self.max_estimated_tokens,
                    "budget.max_estimated_tokens",
                ),
            )
        for name in (
            "max_media_items",
            "max_media_bytes",
            "max_media_pixels",
            "max_estimated_image_tokens",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self, name, _positive_int(value, f"budget.{name}")
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_items": self.max_items,
            "max_characters": self.max_characters,
            "max_estimated_tokens": self.max_estimated_tokens,
            "max_media_items": self.max_media_items,
            "max_media_bytes": self.max_media_bytes,
            "max_media_pixels": self.max_media_pixels,
            "max_estimated_image_tokens": self.max_estimated_image_tokens,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "budget",
            fields={
                "max_items",
                "max_characters",
                "max_estimated_tokens",
                "max_media_items",
                "max_media_bytes",
                "max_media_pixels",
                "max_estimated_image_tokens",
            },
        )
        return cls(
            max_items=payload.get("max_items"),
            max_characters=payload.get("max_characters"),
            max_estimated_tokens=payload.get("max_estimated_tokens"),
            max_media_items=payload.get("max_media_items"),
            max_media_bytes=payload.get("max_media_bytes"),
            max_media_pixels=payload.get("max_media_pixels"),
            max_estimated_image_tokens=payload.get("max_estimated_image_tokens"),
        )


@dataclass(frozen=True)
class ContextPrivacy(_JSONModel):
    """Explicit provider permissions for sensitive context classes.

    Content-class permissions default to false.  Later registry validation
    maps representation kinds to these permissions and rejects invalid source,
    representation, and privacy combinations before resolution.
    """

    allow_provider: bool = True
    allow_planning_context: bool = False
    allow_template_text: bool = False
    allow_document_text: bool = False
    allow_document_images: bool = False
    # Technical metadata about a locally staged file — its relative path, size,
    # timestamp, MIME type, and local parser verdict. Deliberately its own
    # permission: a staged file's *content* (spreadsheet cells, rows, previews,
    # formulas, comments, extracted document text) is a different content class
    # with no representation registered at all.
    allow_file_metadata: bool = False
    # The field names, roles and value types an induced document schema states.
    # Its own permission because it is its own content class: induction read the
    # documents to derive it, but what travels is the shape they share and never
    # a value any of them printed. A context permitted to see a schema is not
    # thereby permitted to see the text it was induced from.
    allow_document_schemas: bool = False
    allow_table_metadata: bool = False
    allow_table_profiles: bool = False
    allow_table_aggregates: bool = False
    allow_table_rows: bool = False
    # The complete rows of one table, gated on the table itself being small
    # (row-count capped, checked by the adapter before a candidate is even
    # built). Deliberately separate from ``allow_table_rows``: that permits an
    # arbitrary slice of a table of any size, whereas this permits only a
    # whole small table, where "small" is the reason a profile stops being a
    # faithful description — a 4-row approval matrix's aggregate max/null
    # statistics cannot say what its one exceptional row actually contains.
    allow_small_table_rows: bool = False
    # What a saved procedure concluded: its definition, verdict, and bounded
    # statistics. Aggregate in the same sense as a table profile.
    allow_analysis_results: bool = False
    # The rows a procedure *flagged*, and the one permission in this contract
    # that admits row-level engagement data to a provider. It is deliberately
    # separate from ``allow_table_rows``: that permits an arbitrary slice of a
    # table, whereas this permits only rows a declared test already identified
    # as exceptions, capped, and only for a capability that has to describe
    # them. A summary that can say "1 invoice was backdated" but not which one
    # is not a summary an auditor can use.
    allow_analysis_exception_rows: bool = False
    # The rows a *Data Test* flagged, for the one capability that must describe
    # them: drafting a finding. Deliberately separate from
    # ``allow_analysis_exception_rows`` — that admits the rows a saved
    # exploratory procedure flagged to the EDA memo, this admits the rows a
    # durable, RCM-linked test flagged to the finding narrative — so revoking
    # one never silently revokes the other. Capped by row count and characters
    # in the adapter before the resolver's budget is even applied. A finding
    # that can say "1 invoice was paid before it was verified" but not which
    # invoice is a finding management cannot act on.
    allow_datatest_exception_rows: bool = False
    # The written EDA memo. Its own permission rather than a reuse of
    # ``allow_analysis_results``: the memo is prose that may quote the
    # identifiers of flagged rows, so it is a wider content class than the
    # bounded verdicts and statistics that permission covers — and a narrower
    # one than the raw flagged rows.
    allow_analysis_summary: bool = False
    # The complete set of distinct values in a column that has few of them — a
    # status vocabulary, a set of designations. Its own permission because it is
    # its own content class: a domain says what values *exist* in a column and
    # never which row holds which, so it discloses the population's categories
    # without disclosing any record. It is what lets a procedure be written
    # against ``Rejected`` at all; a profile reporting "7 distinct values" cannot
    # name one, and a test cannot filter on a value nobody was allowed to see.
    allow_value_domains: bool = False
    # What the auditor typed about this piece of work — "every template section
    # must contain text", "DT-123 does not look right". Its own permission
    # because it is the one source supplied at run time rather than read from
    # the workspace: it is not an artifact anybody can point at afterwards, so
    # it earns a declaration saying exactly which turns may carry one. Bounded
    # and hash-recorded like every other source; the manifest keeps the hash and
    # the size, never the words.
    allow_auditor_instruction: bool = False

    _FIELDS: ClassVar[tuple[str, ...]] = (
        "allow_provider",
        "allow_planning_context",
        "allow_template_text",
        "allow_document_text",
        "allow_document_images",
        "allow_file_metadata",
        "allow_document_schemas",
        "allow_table_metadata",
        "allow_table_profiles",
        "allow_table_aggregates",
        "allow_table_rows",
        "allow_small_table_rows",
        "allow_analysis_results",
        "allow_analysis_exception_rows",
        "allow_datatest_exception_rows",
        "allow_analysis_summary",
        "allow_value_domains",
        "allow_auditor_instruction",
    )

    def __post_init__(self) -> None:
        for name in self._FIELDS:
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"privacy.{name} must be a boolean.")

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self._FIELDS}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(value, "privacy", fields=set(cls._FIELDS))
        defaults = cls()
        return cls(**{name: payload.get(name, getattr(defaults, name)) for name in cls._FIELDS})


@dataclass(frozen=True)
class ContextSelector(_JSONModel):
    """A named deterministic source selector declaration."""

    selector_id: str
    configuration: dict[str, Any] = field(default_factory=dict)

    kind: ClassVar[str] = "deterministic"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selector_id",
            _normalized_text(self.selector_id, "selector.selector_id"),
        )
        configuration = _json_value(self.configuration, "selector.configuration")
        if not isinstance(configuration, dict):
            raise ValueError("selector.configuration must be an object.")
        object.__setattr__(self, "configuration", configuration)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "selector_id": self.selector_id,
            "configuration": _json_value(
                self.configuration,
                "selector.configuration",
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "selector",
            fields={"kind", "selector_id", "configuration"},
        )
        if payload.get("kind") != cls.kind:
            raise ValueError("selector.kind must be 'deterministic'.")
        return cls(
            selector_id=payload.get("selector_id"),
            configuration=payload.get("configuration") or {},
        )


@dataclass(frozen=True)
class AutoSelect(_JSONModel):
    """A bounded declaration for a named automatic local selector."""

    selector_id: str
    item_limit: int
    configuration: dict[str, Any] = field(default_factory=dict)

    kind: ClassVar[str] = "auto"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selector_id",
            _normalized_text(self.selector_id, "selector.selector_id"),
        )
        object.__setattr__(
            self,
            "item_limit",
            _positive_int(self.item_limit, "selector.item_limit"),
        )
        configuration = _json_value(self.configuration, "selector.configuration")
        if not isinstance(configuration, dict):
            raise ValueError("selector.configuration must be an object.")
        object.__setattr__(self, "configuration", configuration)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "selector_id": self.selector_id,
            "item_limit": self.item_limit,
            "configuration": _json_value(
                self.configuration,
                "selector.configuration",
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "selector",
            fields={"kind", "selector_id", "item_limit", "configuration"},
        )
        if payload.get("kind") != cls.kind:
            raise ValueError("selector.kind must be 'auto'.")
        return cls(
            selector_id=payload.get("selector_id"),
            item_limit=payload.get("item_limit"),
            configuration=payload.get("configuration") or {},
        )


Selector = ContextSelector | AutoSelect


def _selector_from_dict(value: object) -> Selector:
    if not isinstance(value, Mapping):
        raise ValueError("source.selector must be an object.")
    kind = value.get("kind")
    if kind == ContextSelector.kind:
        return ContextSelector.from_dict(value)
    if kind == AutoSelect.kind:
        return AutoSelect.from_dict(value)
    raise ValueError("source.selector.kind must be 'deterministic' or 'auto'.")


@dataclass(frozen=True)
class ContextSource(_JSONModel):
    """One required or optional source permitted by a context declaration."""

    id: str
    source_type: str
    required: bool
    selector: Selector
    representations: tuple[ContextRepresentation, ...]
    budget: ContextBudget

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _normalized_text(self.id, "source.id"))
        object.__setattr__(
            self,
            "source_type",
            _normalized_text(self.source_type, "source.source_type"),
        )
        if not isinstance(self.required, bool):
            raise ValueError("source.required must be a boolean.")
        if not isinstance(self.selector, (ContextSelector, AutoSelect)):
            raise ValueError("source.selector must be a ContextSelector or AutoSelect.")
        representations = tuple(self.representations)
        if not representations or any(
            not isinstance(item, ContextRepresentation) for item in representations
        ):
            raise ValueError("source.representations must contain at least one representation.")
        object.__setattr__(self, "representations", representations)
        if not isinstance(self.budget, ContextBudget):
            raise ValueError("source.budget must be a ContextBudget.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "required": self.required,
            "selector": self.selector.to_dict(),
            "representations": [item.to_dict() for item in self.representations],
            "budget": self.budget.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "source",
            fields={
                "id",
                "source_type",
                "required",
                "selector",
                "representations",
                "budget",
            },
        )
        representations = payload.get("representations")
        if not isinstance(representations, list):
            raise ValueError("source.representations must be an array.")
        return cls(
            id=payload.get("id"),
            source_type=payload.get("source_type"),
            required=payload.get("required"),
            selector=_selector_from_dict(payload.get("selector")),
            representations=tuple(
                ContextRepresentation.from_dict(item) for item in representations
            ),
            budget=ContextBudget.from_dict(payload.get("budget")),
        )


@dataclass(frozen=True)
class ContextSpec(_JSONModel):
    """The complete code-authored declaration of context a worker may receive.

    Auditor input curation and explicit regeneration can change the candidate
    material resolved under this policy, but cannot widen the policy itself.
    Unknown fields are rejected during deserialization so an undeclared runtime
    override cannot enter the normalized contract.
    """

    sources: tuple[ContextSource, ...]
    budget: ContextBudget
    privacy: ContextPrivacy = field(default_factory=ContextPrivacy)

    def __post_init__(self) -> None:
        sources = tuple(self.sources)
        if any(not isinstance(item, ContextSource) for item in sources):
            raise ValueError("context_spec.sources must contain only ContextSource values.")
        source_ids = [item.id for item in sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("context_spec source ids must be unique.")
        object.__setattr__(self, "sources", sources)
        if not isinstance(self.budget, ContextBudget):
            raise ValueError("context_spec.budget must be a ContextBudget.")
        if not isinstance(self.privacy, ContextPrivacy):
            raise ValueError("context_spec.privacy must be a ContextPrivacy.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": [item.to_dict() for item in self.sources],
            "budget": self.budget.to_dict(),
            "privacy": self.privacy.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "context_spec",
            fields={"sources", "budget", "privacy"},
        )
        sources = payload.get("sources")
        if not isinstance(sources, list):
            raise ValueError("context_spec.sources must be an array.")
        return cls(
            sources=tuple(ContextSource.from_dict(item) for item in sources),
            budget=ContextBudget.from_dict(payload.get("budget")),
            privacy=ContextPrivacy.from_dict(payload.get("privacy") or {}),
        )


@dataclass(frozen=True)
class ContextSize(_JSONModel):
    """Measured context size recorded after selection and representation."""

    items: int
    characters: int
    estimated_tokens: int
    media_items: int = 0
    media_bytes: int = 0
    media_pixels: int = 0
    estimated_image_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", _non_negative_int(self.items, "size.items"))
        object.__setattr__(
            self,
            "characters",
            _non_negative_int(self.characters, "size.characters"),
        )
        object.__setattr__(
            self,
            "estimated_tokens",
            _non_negative_int(self.estimated_tokens, "size.estimated_tokens"),
        )
        for name in (
            "media_items",
            "media_bytes",
            "media_pixels",
            "estimated_image_tokens",
        ):
            object.__setattr__(
                self,
                name,
                _non_negative_int(getattr(self, name), f"size.{name}"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "characters": self.characters,
            "estimated_tokens": self.estimated_tokens,
            "media_items": self.media_items,
            "media_bytes": self.media_bytes,
            "media_pixels": self.media_pixels,
            "estimated_image_tokens": self.estimated_image_tokens,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "size",
            fields={
                "items",
                "characters",
                "estimated_tokens",
                "media_items",
                "media_bytes",
                "media_pixels",
                "estimated_image_tokens",
            },
        )
        return cls(
            items=payload.get("items"),
            characters=payload.get("characters"),
            estimated_tokens=payload.get("estimated_tokens"),
            media_items=payload.get("media_items", 0),
            media_bytes=payload.get("media_bytes", 0),
            media_pixels=payload.get("media_pixels", 0),
            estimated_image_tokens=payload.get("estimated_image_tokens", 0),
        )


@dataclass(frozen=True)
class ContextSelection(_JSONModel):
    """Content-free record of one source item supplied to the worker."""

    source_id: str
    source_type: str
    source_ref: str
    source_hash: str
    selector_kind: str
    selector_id: str
    selector_definition_hash: str
    reason: str
    representation: ContextRepresentation
    supplied_size: ContextSize
    media: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in (
            "source_id",
            "source_type",
            "source_ref",
            "source_hash",
            "selector_kind",
            "selector_id",
            "selector_definition_hash",
            "reason",
        ):
            object.__setattr__(self, name, _normalized_text(getattr(self, name), f"selection.{name}"))
        if not isinstance(self.representation, ContextRepresentation):
            raise ValueError("selection.representation must be a ContextRepresentation.")
        _reject_row_level_table_representation(
            self.representation,
            "selection.representation",
        )
        if not isinstance(self.supplied_size, ContextSize):
            raise ValueError("selection.supplied_size must be a ContextSize.")
        if self.media is not None:
            media = _json_value(self.media, "selection.media")
            if not isinstance(media, dict):
                raise ValueError("selection.media must be an object.")
            serialized = json.dumps(media, sort_keys=True, separators=(",", ":")).casefold()
            if (
                "base64," in serialized
                or "data:image" in serialized
                or '"path"' in serialized
                or "cache_key" in media
            ):
                raise ValueError(
                    "Manifest media records cannot contain bytes, data URIs, cache "
                    "keys, or local paths."
                )
            object.__setattr__(self, "media", media)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "source_hash": self.source_hash,
            "selector_kind": self.selector_kind,
            "selector_id": self.selector_id,
            "selector_definition_hash": self.selector_definition_hash,
            "reason": self.reason,
            "representation": self.representation.to_dict(),
            "supplied_size": self.supplied_size.to_dict(),
            "media": (
                None
                if self.media is None
                else _json_value(self.media, "selection.media")
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        fields = {
            "source_id",
            "source_type",
            "source_ref",
            "source_hash",
            "selector_kind",
            "selector_id",
            "selector_definition_hash",
            "reason",
            "representation",
            "supplied_size",
            "media",
        }
        payload = _object_payload(value, "selection", fields=fields)
        return cls(
            **{name: payload.get(name) for name in fields - {"representation", "supplied_size"}},
            representation=ContextRepresentation.from_dict(payload.get("representation")),
            supplied_size=ContextSize.from_dict(payload.get("supplied_size")),
        )


@dataclass(frozen=True)
class ContextOmission(_JSONModel):
    """Content-free record of a declared or candidate item not supplied."""

    source_id: str
    reason: str
    source_ref: str | None = None
    source_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _normalized_text(self.source_id, "omission.source_id"))
        object.__setattr__(self, "reason", _normalized_text(self.reason, "omission.reason"))
        for name in ("source_ref", "source_hash"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _normalized_text(value, f"omission.{name}"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_ref": self.source_ref,
            "source_hash": self.source_hash,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "omission",
            fields={"source_id", "source_ref", "source_hash", "reason"},
        )
        return cls(**payload)


@dataclass(frozen=True)
class ContextTruncation(_JSONModel):
    """Content-free record of a selected item reduced to fit policy."""

    source_id: str
    source_ref: str
    reason: str
    original_size: ContextSize
    supplied_size: ContextSize

    def __post_init__(self) -> None:
        for name in ("source_id", "source_ref", "reason"):
            object.__setattr__(self, name, _normalized_text(getattr(self, name), f"truncation.{name}"))
        if not isinstance(self.original_size, ContextSize):
            raise ValueError("truncation.original_size must be a ContextSize.")
        if not isinstance(self.supplied_size, ContextSize):
            raise ValueError("truncation.supplied_size must be a ContextSize.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_ref": self.source_ref,
            "reason": self.reason,
            "original_size": self.original_size.to_dict(),
            "supplied_size": self.supplied_size.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "truncation",
            fields={
                "source_id",
                "source_ref",
                "reason",
                "original_size",
                "supplied_size",
            },
        )
        return cls(
            source_id=payload.get("source_id"),
            source_ref=payload.get("source_ref"),
            reason=payload.get("reason"),
            original_size=ContextSize.from_dict(payload.get("original_size")),
            supplied_size=ContextSize.from_dict(payload.get("supplied_size")),
        )


@dataclass(frozen=True)
class ContextPrivacyDecision(_JSONModel):
    """One explicit allow/withhold decision made before worker invocation."""

    source_id: str
    representation: str
    allowed: bool
    reason: str
    source_ref: str | None = None

    def __post_init__(self) -> None:
        for name in ("source_id", "representation", "reason"):
            object.__setattr__(self, name, _normalized_text(getattr(self, name), f"privacy_decision.{name}"))
        if not isinstance(self.allowed, bool):
            raise ValueError("privacy_decision.allowed must be a boolean.")
        if self.source_ref is not None:
            object.__setattr__(
                self,
                "source_ref",
                _normalized_text(self.source_ref, "privacy_decision.source_ref"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_ref": self.source_ref,
            "representation": self.representation,
            "allowed": self.allowed,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "privacy_decision",
            fields={"source_id", "source_ref", "representation", "allowed", "reason"},
        )
        return cls(**payload)


@dataclass(frozen=True)
class ContextManifest(_JSONModel):
    """Persistable, content-free record of one resolved context bundle."""

    capability_id: str
    unit_id: str
    context_spec_hash: str
    resolver_hash: str
    selections: tuple[ContextSelection, ...]
    omissions: tuple[ContextOmission, ...]
    truncations: tuple[ContextTruncation, ...]
    privacy_decisions: tuple[ContextPrivacyDecision, ...]
    supplied_size: ContextSize

    def __post_init__(self) -> None:
        for name in ("capability_id", "unit_id", "context_spec_hash", "resolver_hash"):
            object.__setattr__(self, name, _normalized_text(getattr(self, name), f"manifest.{name}"))
        typed_sequences = (
            ("selections", ContextSelection),
            ("omissions", ContextOmission),
            ("truncations", ContextTruncation),
            ("privacy_decisions", ContextPrivacyDecision),
        )
        for name, item_type in typed_sequences:
            values = tuple(getattr(self, name))
            if any(not isinstance(item, item_type) for item in values):
                raise ValueError(f"manifest.{name} contains an invalid record.")
            object.__setattr__(self, name, values)
        if not isinstance(self.supplied_size, ContextSize):
            raise ValueError("manifest.supplied_size must be a ContextSize.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "unit_id": self.unit_id,
            "context_spec_hash": self.context_spec_hash,
            "resolver_hash": self.resolver_hash,
            "selections": [item.to_dict() for item in self.selections],
            "omissions": [item.to_dict() for item in self.omissions],
            "truncations": [item.to_dict() for item in self.truncations],
            "privacy_decisions": [item.to_dict() for item in self.privacy_decisions],
            "supplied_size": self.supplied_size.to_dict(),
        }

    @property
    def manifest_hash(self) -> str:
        """Return the canonical content identity for this manifest."""
        return f"sha256:{hashlib.sha256(self.to_json().encode('utf-8')).hexdigest()}"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        fields = {
            "capability_id",
            "unit_id",
            "context_spec_hash",
            "resolver_hash",
            "selections",
            "omissions",
            "truncations",
            "privacy_decisions",
            "supplied_size",
        }
        payload = _object_payload(value, "manifest", fields=fields)
        sequence_fields = (
            ("selections", ContextSelection),
            ("omissions", ContextOmission),
            ("truncations", ContextTruncation),
            ("privacy_decisions", ContextPrivacyDecision),
        )
        parsed: dict[str, tuple[_JSONModel, ...]] = {}
        for name, item_type in sequence_fields:
            items = payload.get(name)
            if not isinstance(items, list):
                raise ValueError(f"manifest.{name} must be an array.")
            parsed[name] = tuple(item_type.from_dict(item) for item in items)
        return cls(
            capability_id=payload.get("capability_id"),
            unit_id=payload.get("unit_id"),
            context_spec_hash=payload.get("context_spec_hash"),
            resolver_hash=payload.get("resolver_hash"),
            selections=parsed["selections"],
            omissions=parsed["omissions"],
            truncations=parsed["truncations"],
            privacy_decisions=parsed["privacy_decisions"],
            supplied_size=ContextSize.from_dict(payload.get("supplied_size")),
        )


@dataclass(frozen=True)
class ContextBundleItem(_JSONModel):
    """One local content item supplied to a worker; never a manifest record."""

    source_id: str
    source_ref: str
    representation: ContextRepresentation
    content: Any
    supplied_size: ContextSize

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _normalized_text(self.source_id, "bundle_item.source_id"))
        object.__setattr__(self, "source_ref", _normalized_text(self.source_ref, "bundle_item.source_ref"))
        if not isinstance(self.representation, ContextRepresentation):
            raise ValueError("bundle_item.representation must be a ContextRepresentation.")
        _reject_row_level_table_representation(
            self.representation,
            "bundle_item.representation",
        )
        normalized_content = _json_value(self.content, "bundle_item.content")
        if self.representation.kind == "page_image":
            if (
                not isinstance(normalized_content, dict)
                or set(normalized_content) != PREPARED_MEDIA_HANDLE_FIELDS
            ):
                raise ValueError(
                    "A page_image bundle item must carry exactly one prepared-media "
                    "handle."
                )
            serialized = json.dumps(
                normalized_content, sort_keys=True, separators=(",", ":")
            ).casefold()
            if "base64," in serialized or "data:image" in serialized or '"path"' in serialized:
                raise ValueError(
                    "Prepared-media handles cannot contain bytes, data URIs, or paths."
                )
        object.__setattr__(self, "content", normalized_content)
        if not isinstance(self.supplied_size, ContextSize):
            raise ValueError("bundle_item.supplied_size must be a ContextSize.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_ref": self.source_ref,
            "representation": self.representation.to_dict(),
            "content": _json_value(self.content, "bundle_item.content"),
            "supplied_size": self.supplied_size.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "bundle_item",
            fields={"source_id", "source_ref", "representation", "content", "supplied_size"},
        )
        return cls(
            source_id=payload.get("source_id"),
            source_ref=payload.get("source_ref"),
            representation=ContextRepresentation.from_dict(payload.get("representation")),
            content=payload.get("content"),
            supplied_size=ContextSize.from_dict(payload.get("supplied_size")),
        )


@dataclass(frozen=True)
class ContextBundle(_JSONModel):
    """Bounded local material available to a worker.

    Unlike :class:`ContextManifest`, this object contains source content and is
    not a durable sidecar or provider-provenance payload.
    """

    capability_id: str
    unit_id: str
    items: tuple[ContextBundleItem, ...]
    supplied_size: ContextSize

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability_id", _normalized_text(self.capability_id, "bundle.capability_id"))
        object.__setattr__(self, "unit_id", _normalized_text(self.unit_id, "bundle.unit_id"))
        items = tuple(self.items)
        if any(not isinstance(item, ContextBundleItem) for item in items):
            raise ValueError("bundle.items must contain only ContextBundleItem values.")
        object.__setattr__(self, "items", items)
        if not isinstance(self.supplied_size, ContextSize):
            raise ValueError("bundle.supplied_size must be a ContextSize.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "unit_id": self.unit_id,
            "items": [item.to_dict() for item in self.items],
            "supplied_size": self.supplied_size.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = _object_payload(
            value,
            "bundle",
            fields={"capability_id", "unit_id", "items", "supplied_size"},
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("bundle.items must be an array.")
        return cls(
            capability_id=payload.get("capability_id"),
            unit_id=payload.get("unit_id"),
            items=tuple(ContextBundleItem.from_dict(item) for item in items),
            supplied_size=ContextSize.from_dict(payload.get("supplied_size")),
        )

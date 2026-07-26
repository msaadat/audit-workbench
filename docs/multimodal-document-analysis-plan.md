# Multimodal Document Analysis Plan

## Status and recommendation

This document plans the conversion of Audit Workbench's text-only document-analysis workflow into multimodal document analysis by default whenever the configured agent model profile supports vision.

Implementation should be delivered in three phases:

1. Multimodal runtime and context contracts.
2. Image preparation and the document-analysis vertical slice.
3. Durable derived text, downstream reuse, UI, and rollout.

Multimodal behavior should be enabled by default only after the final phase gate passes. Vision must remain confined to document analysis: each stable image or rendered PDF page is analyzed in its document-analysis map unit, while planning, RCM, search, document tests, document chat, and reporting consume the persisted visual transcription and summary without resending the image.

Privacy and consent design are outside this planning pass. Existing privacy safeguards, especially the prohibition on sending row-level table data, must remain intact.

### Clean-slate assumption

This plan is implemented against a clean slate. No workspace persisted by an earlier build has to remain readable: existing runs, proposal and receipt sidecars, generated analyses, extraction caches, and search indexes are discarded and regenerated.

That assumption is load-bearing, because three serializers that feed identity hashes emit every declared field unconditionally, so any added field changes hashes for every workflow, not only for documents:

- `ContextPrivacy.to_dict` (`backend/app/agent/context/model.py`) emits all of `_FIELDS`, so adding `allow_document_images` changes `context_spec_hash` for every registered preset.
- `WorkerDefinition.to_dict` (`backend/app/agent/workers/model.py`) feeds `definition_hash`, so adding `required_model_capabilities` changes it for every registered worker.
- `ProposalExecutionIdentity.from_dict` (`backend/app/agent/runtime/unit_pipeline.py`) rejects any payload whose field set is not exactly the expected set, so the four added identity fields make every previously written sidecar unreadable.

Under a clean slate this is the correct outcome and needs no mitigation. New fields are added unconditionally, readers stay strict, and no omit-when-absent compatibility rule is introduced anywhere in the identity path. If the clean-slate assumption is ever withdrawn, each of the three call sites above must instead omit its new field when absent or default, and a regression test must pin a pre-change fixture's `identity_hash` byte-for-byte — that work is deliberately not in scope here.

The one compatibility rule that survives is API-facing, not identity-facing: the legacy `provider` and `model` response fields stay populated for existing HTTP clients.

## Desired behavior

| Source | Vision available | Vision unavailable |
| --- | --- | --- |
| Text-only page | Existing text worker | Existing text worker |
| Standalone image | One visual map unit, followed by local commit or text-only reduction | Open item with `document_requires_vision`; no model call |
| Scanned/image-only PDF page | One visual map unit per page | Page omitted with an explicit reason |
| PDF page with no text and no detected image | One visual map unit per page (see the routing predicate below) | Page omitted with an explicit reason |
| Mixed text/image PDF page | Extracted text analyzed by the text worker; visual coverage only on explicit opt-in | Extracted text analyzed; visual coverage omitted |
| DOCX embedded images | Deferred initially; extracted text remains analyzable | Same |
| Low-text DOCX with embedded images | Open item with `document_visual_source_unsupported`; no model call | Same |

For `Workspaces/pr/Documents/cce691ea6b.png` (`Org Chart.png`), the successful path should:

1. Prepare one normalized overview plus deterministic detail tiles.
2. Send all prepared parts in one vision request.
3. Produce an AI-derived visual transcription, summary, audit notes, and typed visual page/region anchors.
4. Commit directly without a reduction call when it is the document's only map unit.
5. Supply the persisted summary and transcription to planning and later workflows without another vision request.

A bounded validation repair may resend the same unit, consistent with current worker behavior. On the ordinary successful path, resume, planning, RCM, search, and document chat must not cause another vision request.

## Product and safety boundaries

- Only the document-analysis map capability may receive images.
- Reduction receives validated map proposals, never source images.
- Downstream workflows receive generated summaries, AI-derived transcription, and typed anchors.
- AI-derived transcription must remain distinct from deterministic text extraction and from auditor-confirmed evidence.
- A generated visual summary does not prove that a control operated.
- Existing row-level table restrictions remain unchanged.
- Initial support covers standalone image files and rendered PDF pages. Server-side rendering of embedded DOCX images is a later extension because current extraction has no stable DOCX page/image mapping.
- Vision is opt-in for any page that already yielded usable extracted text. Cost scales with page count, so a page is never sent to a model merely because it contains an image.
- Known lack of vision is a deterministic open-item outcome, not a provider error.
- If a profile is declared vision-capable but the provider rejects the image request, surface a typed execution error. Do not silently spend a second text fallback call after a paid vision attempt.

## Phase 1: Multimodal runtime and context contracts

Phase 1 must not alter document-analysis behavior. It establishes typed requests, profile selection, media budgets, and durable identities.

### 1. Model profiles and capability resolution

Touchpoints:

- `backend/app/assistant_settings.py`
- `backend/app/llm.py`
- `backend/app/agent/runtime/model_gateway.py`
- `backend/app/agent/base.py`
- `backend/app/agent/workers/model.py`

Replace provider-wide `vision: bool` as the authoritative decision with a resolved model-profile record containing:

- profile name;
- provider and model;
- declared capabilities such as `vision`;
- configuration source;
- stable profile hash.

Keep the existing `AGENT_VISION_PROVIDER`, `AGENT_VISION_BACKEND`, and `AGENT_VISION_MODEL` overrides. Extend persisted non-secret settings with an optional agent-vision profile while continuing to accept the existing assistant-only settings file.

For known provider models, resolve capabilities from model-level catalog metadata. For arbitrary/custom model names, particularly LM Studio, require an explicit vision-capable declaration. Do not probe a provider with a paid call to discover capabilities.

Snapshot the resolved text and vision profiles when a new run starts. A resumed run uses its snapshot so a settings change cannot silently change the provider or model partway through execution.

### 2. Keep one model gateway

Generalize `ModelGateway.complete` from a string-only user prompt to a typed request with ordered content parts:

- text;
- prepared image, referenced by handle rather than by value.

The existing signature must keep working unchanged. `complete(system, user, activity, *, attempt)` has thirteen production call sites across `agent/workers/` plus test doubles in `test_agent_worker_models.py` and `test_agent_runtime_contracts.py`, and Phase 1 is required to be behavior-neutral. Prepared media therefore arrives through a new keyword-only argument — a `str` `user` remains valid and continues to mean a single text part. Only the new visual worker passes the new argument.

Registered workers declare their required model capabilities. Only `DefaultModelGateway` may:

- resolve the profile;
- choose `profile="agent"` or `profile="vision"`;
- read prepared image bytes from the prepared-media cache;
- create OpenAI-compatible `image_url` parts through `llm.image_part`;
- call `llm.chat`;
- reserve and reconcile budgets;
- record telemetry and provenance.

Image bytes never travel through the worker, the context bundle, or the request object. A worker names prepared media by handle — prepared-byte SHA-256 plus the cache key — and the gateway is the only component that dereferences a handle into bytes, immediately before `llm.chat`. This keeps the single component that may see image content identical to the single component that may call a provider.

The existing one-provider-call-site architecture gate must continue to pass.

The gateway should:

- key provider concurrency on the resolved provider/model;
- hash canonical text plus image identities for `prompt_version`;
- set `vision_used=True` when an image part was sent;
- record the actual profile, provider, and model;
- redact inline base64 from durable debug records, retaining MIME, byte count, dimensions, and hashes.

### 3. Worker and unit-pipeline identity

Add `required_model_capabilities` to `WorkerDefinition` and include it in `definition_hash`.

Extend `ProposalExecutionIdentity` in `backend/app/agent/runtime/unit_pipeline.py` with:

- model-profile hash;
- input modalities;
- prepared-media hashes;
- image-preparation policy hash.

Add stable proposal-reuse rejection reasons:

- `model_profile_changed`;
- `input_modalities_changed`;
- `prepared_media_changed`;
- `media_policy_changed`.

No image bytes, data URIs, or absolute paths may be written to proposal or receipt sidecars.

Allow a capability to declare per-unit context bindings. `documents.analysis_chunks_ready` then selects:

- the existing `documents.analysis_chunk` preset for text units;
- a new `documents.analysis_visual_page` preset for visual units.

The selected binding must be part of the capability and proposal execution identity rather than an undeclared runtime override.

### 4. Context models, resolver, and manifests

Touchpoints:

- `backend/app/agent/context/model.py`
- `backend/app/agent/context/presets.py`
- `backend/app/agent/context/resolver.py`
- `backend/app/agent/context/manifest.py`
- `backend/app/agent/context/adapters.py`

Add:

- `ContextPrivacy.allow_document_images`, defaulting to `False`;
- a `page_image` representation mapped only to that permission;
- a JSON-representable prepared-media handle carried as the bundle item's content;
- independent media limits for image count, decoded bytes, pixels, and estimated image tokens.

Continue measuring textual characters and estimated text tokens separately.

A bundle item cannot carry bytes. `ContextBundleItem.__post_init__` forces `content` through `_json_value`, and the class is serializable through `to_dict`; relaxing that is what would turn "sidecars contain no binary content" from an enforced invariant into a convention. The `page_image` bundle item therefore contains only:

- source reference;
- source and prepared hashes;
- page/frame;
- variant/tile order;
- MIME;
- dimensions;
- prepared-byte count and pixel count.

That handle is sufficient for the gateway to load the prepared bytes from `Documents/.prepared/`, and it is safe to serialize anywhere a bundle already is. A handle whose prepared file is missing or whose bytes do not match the recorded SHA-256 is a typed preparation failure, not a silent re-preparation.

Manifests must remain content-free. A media selection records only:

- source reference;
- source/prepared hashes;
- page/frame;
- variant/tile order;
- MIME;
- dimensions;
- supplied media metrics;
- selector and preparation policy identities.

It must not contain bytes, base64, or a local path.

New manifest fields are added unconditionally under the clean-slate assumption; no omit-when-absent rule is introduced. Register `documents.analysis_visual_page`; no other preset may declare `page_image`.

### Phase 1 acceptance criteria

- All existing tests pass with unchanged document behavior. Identity-hash changes caused by the three added serializer fields are expected and are absorbed by regenerating fixtures, not by compatibility shims.
- `complete` still accepts a `str` user prompt at all thirteen existing call sites.
- Multimodal requests are budgeted and attributed through `DefaultModelGateway`.
- Static tests still find exactly one direct agent provider-call module.
- A context manifest or bundle containing an image selection contains no bytes, base64, or local path — only a prepared-media handle.
- `DefaultModelGateway` is the only module that reads from `Documents/.prepared/`.
- No planning, APM, RCM, reporting, document-test, intake, analysis, or chat preset can resolve `page_image`.

## Phase 2: Image preparation and document-analysis vertical slice

### 1. Deterministic media preparation

Add a document-domain module such as `backend/app/document_media.py`, called through:

- `backend/app/documents.py`;
- `backend/app/agent/executors/documents.py`;
- document context adapters.

Recommended portable dependencies are Pillow and `pypdfium2`. This avoids requiring a system Poppler installation in the portable distribution.

Preparation rules:

- Sniff and decode file content instead of trusting only the extension.
- Accept PNG, JPEG, WebP, BMP, TIFF, and PDF pages.
- Normalize to sRGB PNG.
- Apply EXIF orientation.
- Flatten transparency on white.
- Strip metadata.
- Render PDF pages at 160 DPI.
- Bound an overview to a 2,048-pixel long edge and 4 megapixels.
- For unusually wide, tall, or dense sources, include one overview followed by deterministic row-major detail tiles.
- Use at most four image parts, 12 megapixels, and 12 MiB of prepared bytes per unit.
- Use 64-pixel tile overlap.
- Default to at most 20 visual pages per document. Persist the effective bound in workflow scope.
- Treat multi-frame TIFF frames as pages within the same bound.

Cache prepared media below `Documents/.prepared/` using source- and policy-addressed directories. A prepared part identity covers:

- source SHA-1;
- page/frame;
- overview/tile order and bounds;
- normalized MIME and dimensions;
- prepared-byte SHA-256;
- preparation implementation and policy version.

The 3,840 by 716 Org Chart should become one overview plus ordered horizontal detail tiles in the same model request.

### 2. Widen the existing document workflow

Touchpoints:

- `backend/app/agent/capabilities/documents.py`
- `backend/app/agent/documents_execution.py`
- `backend/app/agent/workflows/documents.py`
- `backend/app/agent/workflows/audit.py`

Keep the current workflow and capability IDs to avoid a structural workflow migration. Internally widen `documents.text_ready` to mean “model-usable document content ready” and change its user-facing title to “Document content.”

Replace the internal text-only `analyzable()` and `chunk_specs()` assumptions with generalized analysis-unit specifications:

- `document_chunk_analysis`;
- `document_visual_page_analysis`.

Preserve existing text unit IDs exactly. Visual unit IDs must include document, page/frame, and prepared-set identity.

#### Visual routing predicate

Which pages reach a vision model is the single most cost-consequential decision in this plan, and it must be a stated predicate rather than a judgement about relevance. `embedded_images` in `_pdf_pages` counts every `/Image` XObject, so a letterhead logo would otherwise make every page of an ordinary policy PDF "visual" and turn one document into twenty vision calls.

A page is routed to the visual worker when, and only when, one of the following holds:

1. The document is a standalone image file.
2. The page's extraction is `image_only` — that is, below `MIN_TEXT_CHARACTERS` of extracted text and carrying at least one detected image.
3. The page yielded no usable extracted text and no detected image, and the document is a PDF. This covers vector-only pages and scans built from inline `BI`/`ID` images, which `_pdf_pages` currently labels `image_only=False` with zero characters. Without this clause, exactly the scanned PDFs most in need of vision are never routed anywhere.
4. The auditor explicitly opted the document into full visual coverage through workflow scope.

A page that produced usable extracted text is never routed to vision by clause 2 or 3, regardless of how many images it contains. Clause 4 is the only path to visual coverage of a text-bearing page, it is per-document rather than global, and it is subject to the same visual page bound.

Clause 3 also requires widening the extraction state that reaches analysis. A PDF whose pages all have zero characters and no detected image currently resolves to `state="failed"` in `extract_document`, and `analyzable()` rejects `failed` outright. `failed` must be distinguished: a genuine extraction error stays `failed` and is reported, while a structurally valid PDF that simply carries no extractable text becomes eligible for clause 3 rather than being discarded as broken.

#### Remaining behavior

- Text-only pages use the unchanged text worker.
- Standalone image files produce one visual page unit.
- Mixed PDF units contain both extracted page text and prepared images, and are produced only under clause 4.
- A low-text DOCX carrying embedded images is currently labelled `image_only` by `_docx_page`, which makes it ineligible for both the text worker and — because DOCX rendering is deferred — the visual worker. It must settle with a distinct `document_visual_source_unsupported` reason rather than `document_requires_vision`, which would otherwise promise the auditor that configuring a vision profile will make the document analyzable.
- Unit expansion remains read-only.
- Preparation occurs at the deterministic readiness/execution boundary, not while asking readiness.
- Known vision unavailability returns `awaiting_confirmation`.
- Preparation failure affects that page/document without discarding successful sibling proposals.

The map capability remains `all_settled_parallel`. Its binder selects the worker by unit kind.

Reduction already reads map proposals in document source order: `_chunk_analyses` iterates `chunk_specs` output rather than sorting unit IDs. This is an existing property to preserve when visual units join the same ordering, not new work.

Coverage should separately record:

- text-analyzed pages;
- vision-analyzed pages;
- omitted pages;
- structured omission reasons such as `vision_unavailable`, `visual_page_limit`, `media_preparation_failed`, or `map_unit_failed`.

Update dynamic limits from the actual number and type of map units.

### 3. Visual worker and visual evidence

Add `documents.analysis_visual_page` in `backend/app/agent/workers/documents.py`. Leave the existing text worker and prompt intact.

The visual worker requires the `vision` capability and returns:

- `transcription_markdown`;
- `summary_markdown`;
- `audit_notes_markdown`;
- typed citations.

Support two citation forms:

1. Text citation:
   - current page and exact-excerpt validation.
2. Visual citation:
   - supplied page;
   - optional normalized region;
   - short visual description;
   - evidence kind `visual`.

The server, not the model, injects source and prepared-image hashes after validating that the page, image part, tile, and normalized region were actually supplied.

Visual descriptions are not verbatim quotations and cannot be independently exact-matched. The UI and downstream evidence model must label them accordingly.

### 4. Reduction and commit

Reduction receives only validated map proposals. It never receives image bytes or prepared-image references capable of loading the source.

It should:

- preserve ordered transcription;
- consolidate summaries and notes;
- preserve both text and visual anchors;
- carry map-unit modality and generation profile metadata.

When a document has one map proposal, retain the current local single-chunk reduction optimization. For a standalone image such as the Org Chart, the map response is committed locally, making the successful path exactly one vision model turn.

### Phase 2 acceptance criteria

- The Org Chart produces a durable generated analysis.
- Its successful path performs one vision map call and no reduction model call.
- Its map proposal contains transcription, summary, notes, and validated visual anchors.
- Provenance records `vision_used=True` and the vision profile identity.
- A resume with unchanged source, prepared media, policy, context, worker, and profile performs no additional model call.
- A profile or prepared-media identity change rejects proposal reuse with the expected reason.
- Mixed PDF documents preserve successful text analysis when a visual page cannot be analyzed.
- A text-bearing PDF carrying a logo on every page produces zero visual units and zero vision calls.
- A vector-only PDF page with no text and no detected image is routed to the visual worker rather than discarded as a failed extraction.

## Phase 3: Durable derived text, downstream reuse, UI, and rollout

### 1. Artifact schema and validity

Touchpoints:

- `backend/app/document_analysis.py`
- `backend/app/agent/executors/documents.py`
- `backend/app/agent/documents_execution.py`

Introduce a generated-analysis schema containing:

- `derived_text_markdown`;
- `derived_text_sha256`;
- typed citations;
- `vision_used`;
- `generation_profiles`;
- prepared-media-set hash;
- richer coverage.

Do not write AI-derived transcription into `Documents/.extracted` and do not change an image-only document into a deterministically extracted text document.

Include derived text, typed citations, and prepared-media identity in:

- analysis content hashes;
- cache identity;
- executor reconciliation;
- receipt output.

`cache_identity` currently joins four positional values with `\0` and feeds the `current`/`stale` verdict in `_authoritative_status`. The prepared-media component is added as a fifth positional value, unconditionally — under the clean slate there are no prior analyses whose verdict could flip, and a text-only document simply contributes an empty media identity. Bump `ANALYSIS_SCHEMA_VERSION` in the same change so the derivation is explicit rather than implied.

Keep legacy `provider` and `model` fields for existing API clients. `generation_profiles` becomes authoritative for mixed text and vision generation.

The executor must not look up the current model through `llm.agent_status()` at commit time. It should persist the exact profile metadata carried by the accepted run/proposal identities.

### 2. Downstream use without resending images

Touchpoints:

- `backend/app/document_context.py`
- `backend/app/agent/context/adapters.py`
- `backend/app/agent/context/presets.py`
- `backend/app/document_search.py`
- document-test and document-chat adapters.

Add a bounded `derived_text` or `vision_transcript` representation.

Rules:

- Planning continues to prefer the current document summary.
- Detail-oriented document-test and chat contexts may consume bounded derived text and visual anchors.
- Other workflows never receive raw images.
- Derived text is explicitly marked as model-generated and not auditor-confirmed.
- Downstream generated content must preserve its source analysis ID and original document hash.

Local search should index deterministic extracted text plus current visual transcription. Search-index identity must include the generated analysis content identity. Mark each search chunk with:

- `origin: extracted_text`; or
- `origin: vision_transcript`.

`extract_document` currently sets `search_index_state="unsupported"` whenever extraction resolves to `image_only`, which is precisely the case this section makes indexable. That assignment must become conditional: a document with a current visual transcription is `pending` and then indexed like any other, and only a document with neither extractable text nor a visual transcription stays `unsupported`. Committing a visual analysis must move the document out of `unsupported`, otherwise derived text is persisted but never reaches the index and the acceptance criterion below passes vacuously.

Search, planning, RCM, reporting, and document chat must not invoke vision after a current analysis exists.

### 3. Budget accounting

Extend `backend/app/agent/runtime/run_runtime.py` to reserve before a call using:

- text characters divided by four;
- a provider-specific image estimator when registered;
- a conservative fallback of 4,096 estimated tokens per prepared image part;
- request-byte and image-count limits.

Provider-reported `prompt_tokens` or `input_tokens` remain authoritative after a call. If the provider omits usage, retain the conservative estimate as charged usage.

Track per call and per worker:

- text-token estimate;
- image-token estimate;
- image count;
- prepared bytes and pixels;
- provider-reported prompt/completion tokens;
- vision profile identity;
- retry number.

Update document workflow dynamic limits using actual text and visual unit estimates rather than a fixed per-chunk multiplier.

Do not enforce monetary cost unless explicit provider/model pricing is configured. Token, image-count, byte, pixel, turn, unit, and page budgets remain the hard controls.

#### Reconciling the per-turn allowance with the media bounds

The current dynamic limit in `_refresh_dynamic_limits` allows `calculated * 12_000` estimated prompt tokens, an even 12,000 per turn. The media bounds in this plan permit four image parts per unit at a 4,096-token conservative estimate — 16,384 tokens before any text. As written, a single visual unit exceeds the per-turn allowance and a mostly-visual run would fail with "estimated prompt-token limit reached" partway through rather than completing with bounded coverage.

The per-turn allowance must therefore be computed per modality rather than as one multiplier:

- text units keep the existing 12,000-token allowance;
- visual units are allowed the per-unit image bound plus a text headroom — 4 parts times 4,096, plus 4,000 for the prompt and any extracted page text, or 20,480 tokens;
- the run total is the sum over the units actually expanded, not a unit count times a single constant.

`max_model_turns` and `max_completion_tokens` continue to follow the unit count. A test must assert that a document expanded to the full visual page bound fits inside the budget the same expansion computes for it, so the two numbers cannot drift apart again.

### 4. API, settings, and frontend

Backend touchpoints:

- `backend/app/routes/agent_routes.py`
- `backend/app/routes/document_routes.py`
- `backend/app/assistant_settings.py`
- `backend/app/llm.py`

Frontend touchpoints:

- `frontend/src/types.ts`
- `frontend/src/components/DocumentsTab.vue`
- `frontend/src/composables/documentStatus.ts`
- `frontend/src/composables/useAgentRun.ts`
- a new model-settings dialog or equivalent settings surface.

Preserve current flat status fields while adding nested text and vision profile status, including a user-facing unavailability reason.

Add stable status/error codes:

- `document_requires_vision` — analyzable once a vision profile is configured;
- `document_visual_source_unsupported` — a visual source this build cannot prepare, such as a low-text DOCX, which configuring a vision profile will not fix;
- `visual_preparation_failed`;
- `visual_page_limit_reached`;
- `vision_request_rejected`.

The first two must stay distinct in the UI. Collapsing them would tell an auditor that buying a vision profile will make a DOCX analyzable when it will not.

Frontend behavior:

- Include image-only documents in batch analysis when vision is available.
- When vision is unavailable, allow the user to start analysis but explain that the image will remain an open item without a model charge.
- Offer per-document opt-in to full visual coverage for text-bearing documents, since routing clause 4 has no other trigger, and show the visual page bound that opt-in is subject to.
- Replace the unconditional “Image-only source” problem label with “Visual source—analysis available” when a current visual analysis exists.
- Display text and visual coverage separately.
- Display omission reasons.
- Render visual citations as page/region descriptions and open the original image/PDF page.
- Show vision profile, `vision_used`, prepared-media hash, and transcription hash in technical provenance.
- Expose vision-profile configuration. The current Vue frontend has no consumer for the existing assistant-settings PATCH endpoint, so this requires an actual settings surface rather than types alone.

## Migration and invalidation

Under the clean-slate assumption there is no migration to perform. Workspaces persisted by an earlier build are discarded rather than upgraded, so this plan introduces no legacy contract flag, no compatibility branch, and no dual-reader path. Specifically, none of the following are in scope: preserving old generated analyses or their validity verdicts, resuming pre-existing runs, reading pre-existing proposal or receipt sidecars, or keeping old manifest hashes verifiable.

What remains in scope is ordinary invalidation within the new implementation:

- Source replacement invalidates prepared media, visual transcription, analysis, and search identity through the source hash.
- A prepared-media file that is missing, or whose bytes do not match the recorded prepared SHA-256, is a typed preparation failure rather than a silent re-preparation.
- Preserve existing text worker IDs, unit IDs, and preset IDs. Not for compatibility — they are still the right identifiers, and changing them would churn the text path this plan is meant to leave alone.
- The legacy `provider` and `model` fields stay populated in API responses, which is an HTTP client contract rather than a storage one.

If the clean-slate assumption is withdrawn later, the migration work is the omit-when-absent treatment of the three identity serializers named in the clean-slate section, plus a versioned generated-analysis reader. That is a separate piece of work and is deliberately excluded here.

## Tests and phase gates

### Runtime and transport

Extend `backend/tests/test_agent_llm.py`:

- vision profile resolution;
- custom-model capability declarations;
- multimodal message encoding;
- capability mismatch;
- sanitized debug payload;
- correct provider/model selection.

Extend `backend/tests/test_agent_runtime_contracts.py`:

- media pre-charge;
- provider-usage reconciliation;
- no-usage conservative charging;
- run profile snapshot;
- `vision_used=True`;
- per-worker media metrics;
- one provider call site.

### Context

Extend:

- `backend/tests/test_agent_context_models.py`;
- `backend/tests/test_agent_context_resolver.py`;
- `backend/tests/test_agent_context_adapters.py`.

Cover:

- `allow_document_images` defaults false;
- media count/byte/pixel/token bounds;
- stable selection and tile ordering;
- normalized-media hashes;
- no bytes, base64, or paths in manifests or bundles — a `page_image` bundle item carries only a handle;
- a bundle carrying a `page_image` item still round-trips through `to_dict`/`from_dict`;
- only the document visual-analysis preset can resolve `page_image`;
- row-level table protection remains unchanged.

### Unit pipeline

Extend `backend/tests/test_agent_unit_pipeline.py`:

- unchanged visual proposal resumes without a provider call;
- changed prepared bytes reject reuse;
- changed preparation policy rejects reuse;
- changed model profile rejects reuse;
- changed prompt/context/worker/schema still reject with existing exact reasons;
- proposals and receipts contain no binary content;
- interrupted commit reconciliation remains idempotent.

### Document workflow

Extend `backend/tests/test_workflow_documents.py` with:

- standalone PNG success;
- unusually wide image tiling and ordering;
- scanned/image-only PDF;
- vector-only PDF page with no text and no detected image routed by clause 3;
- structurally broken PDF still resolving to `failed` rather than being routed to vision;
- text-bearing PDF page carrying a logo on every page producing **zero** visual units;
- the same document under explicit opt-in producing visual units bounded by the visual page limit;
- low-text DOCX settling with `document_visual_source_unsupported` and no model call;
- no-vision deterministic fallback;
- mixed-document partial visual coverage;
- corrupt image;
- oversized image;
- visual page limit;
- a document expanded to the full visual page bound fitting the budget its own expansion computes;
- one failed visual unit preserving sibling proposals;
- reduction receiving no images;
- single visual unit committing without a reduction call;
- downstream planning, RCM, search, and chat causing zero additional vision calls.

### Artifact, search, and executor

Add or extend tests for:

- transcription and typed visual anchors surviving commit and reload;
- prepared-media identity participating in validity;
- interrupted commit producing no duplicate artifact;
- source replacement invalidating prepared media and derived text;
- a prepared file whose bytes no longer match its recorded hash producing a typed failure, not a re-preparation;
- committing a visual analysis moving the document out of `search_index_state="unsupported"`;
- search indexing derived text locally;
- search results marking `vision_transcript` origin;
- no vision call during indexing or retrieval.

### Frontend and packaging

- Add component tests for image-only eligibility, vision-unavailable status, visual coverage, visual citations, and provenance.
- Run the production frontend build.
- Add a portable Windows packaging smoke test for Pillow and PDFium.

### Org Chart integration gate

The decisive end-to-end gate should import or copy `Workspaces/pr/Documents/cce691ea6b.png` and assert:

1. One visual map unit is created.
2. Exactly one successful vision model call occurs.
3. No reduction model call occurs.
4. A generated analysis exists with transcription, summary, notes, and visual anchors.
5. `vision_used=True` and the expected profile are recorded.
6. Planning context receives the summary or derived text.
7. RCM generation, search indexing/query, and document chat do not resend the image.
8. Reopening/resuming the map unit does not increase the vision-call count.

A paired no-vision test must assert:

1. No provider call occurs.
2. The run completes with open items.
3. The unit carries `document_requires_vision`.
4. No generated analysis is created.

## Explicit design decisions

The following choices should be approved before implementation:

1. **Renderer:** use Pillow plus `pypdfium2` for portable image normalization and PDF rendering.
2. **Visual citations:** use page/region visual anchors rather than pretending a visual description is a verbatim excerpt.
3. **Derived text status:** keep AI transcription separate from deterministic extraction and auditor-confirmed evidence.
4. **DOCX images:** defer server-side embedded-image rendering initially and surface unsupported visual coverage.
5. **Capability detection:** use declared model capabilities rather than paid provider probes.
6. **Default bounds:** 20 visual pages per document, four image parts per unit, 12 MiB and 12 megapixels per unit, and 4,096 estimated tokens per image part — with the per-turn token allowance computed per modality so a visual unit's own bound fits inside it.
7. **Clean slate:** discard workspaces written by earlier builds rather than migrating them, which is what permits added identity fields, strict readers, and freely changing hashes.
8. **Visual routing:** route on the stated four-clause predicate. A page that already yielded usable extracted text reaches a model only under explicit auditor opt-in, because the available `embedded_images` signal cannot distinguish a letterhead from evidence.
9. **Image bytes:** carry prepared media by handle everywhere except inside `DefaultModelGateway`, so the only component that may see image content is the only component that may call a provider.
10. **Runtime provider rejection:** fail or leave an explicit open item; do not silently fall back after spending a vision call.
11. **Evidence use:** downstream workers may use generated transcription and summary as attributed secondary context, but not as proof that the source fact was independently verified or that a control operated.

## Definition of done

Multimodal document analysis is complete when:

- a configured vision-capable profile makes supported image and scanned-PDF content analyzable by default;
- deterministic text fallback remains available;
- known lack of vision causes no image provider call;
- every model call remains budgeted and attributed through `DefaultModelGateway`;
- manifests, bundles, and sidecars remain content-free with respect to binary image data, carrying prepared media by handle;
- visual inputs are bounded, normalized, hashed, and stably ordered;
- a text-bearing page never reaches a vision model without explicit auditor opt-in;
- a document expanded to its visual page bound fits the budget that expansion computes for it;
- a successful visual unit is resumable without rebilling;
- image content is sent only during document analysis;
- durable transcription and summary are reusable throughout the agent workflow, including the local search index;
- typed visual provenance is visible to the auditor;
- the Org Chart integration gate and all architecture boundary gates pass.

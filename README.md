# Audit Workbench

A local-first audit workbench spanning data analysis, engagement planning,
document evidence, fieldwork, findings, and report drafting. Structured data
is computed on your machine. Configured models receive bounded, unmasked result
previews and attached document context so local models can work with real
engagement values.

## Capabilities

- **Workspaces** — one per engagement. Each workspace is persistent and holds
  its source files, table/join definitions, planning artifacts, evidence,
  findings, report, and durable assistant runs.
- **Incremental audit-folder intake** — select a mixed local folder, compare it
  with prior imports, stage only new/changed files, review editable routing
  proposals, and import supported tables and documents idempotently. Changed
  tables and documents replace their prior content while retaining stable links.
- **RCM-central planning and fieldwork** — capture engagement context through
  an assistant interview, then generate and edit an Audit Planning Memorandum
  and Risk & Control Matrix. Structured planned tests, execution status,
  outcomes, limitations, observations, evidence requests, and working papers
  stay under their RCM row. Markdown templates and local methodology packs are
  configurable per workspace.
- **Data** — load multiple CSV/TSV/Excel files with inferred types and define
  named joins across them (left/inner/full/semi/anti).
- **Profile and Query** — automatic typed profiling plus interactive filters,
  group-by aggregations, split-by cross-tabs, sorting, server pagination,
  group-row drill-down, charts, and Excel exports.
- **Data Tests** — create durable exploratory or RCM-linked analytics,
  validation, and visible Polars definitions. Exploratory tests can be run and
  pinned but do not count as audit coverage or support formal findings. Runs preserve bounded result and exception artifacts,
  histories, hashes, table fingerprints, and semantic-validity checks. The
  analytics library includes 15 tests: Benford and last-two-digit analysis,
  duplicates, sequence gaps, reproducible sampling, period comparison, round
  numbers, outliers, threshold clustering, weekend postings, date-lag or
  backdating, stratification, completeness, negative/zero scan, and rare
  values. Results can be saved, exported, and pinned.
- **Dashboard** — pin reproducible queries or let the audit run curate four to
  six useful RCM-linked Data Test results using deterministic risk, exception,
  visualization, and management-relevance scoring.
- **Documents and evidence** — ingest PDF, TXT/Markdown, DOCX, and common image
  formats; preview extracted pages, explicitly confirm replacements, ask cited
  questions, and navigate immutable typed evidence anchors. AI-activity logs retain provider,
  model, page, source-hash, and artifact provenance without duplicating raw
  content into the ledger.
- **Document tests and working papers** — perform vouching/tracing, attribute
  testing, document/minutes review, and cited document Q&A. Comparisons remain
  explainable through exact, normalized, fuzzy-similarity, numeric-tolerance,
  and date-tolerance results. Seeded samples freeze key fields, per-item state
  is resumable, and linked RCM rows render evidence-based Markdown/HTML working
  papers covering all planned tests and execution artifacts.
- **Findings and reports** — disposition execution observations, promote a
  supported observation into an editable draft, or add a manual IIA-style
  finding with condition, criteria, cause,
  effect, recommendation, management response, and typed evidence. Generate an
  editable report from live planning-through-finding context, run advisory
  RCM-to-planned-test-to-execution-to-evidence traceability checks, copy
  Markdown/HTML, and reconcile regeneration side by side so auditor edits are
  never silently overwritten. Open material work produces a clearly labelled
  preliminary report rather than an unsupported final conclusion.
- **Natural-language assistant** — ask questions in plain English. A configured
  Groq, OpenRouter, Mistral, or local LM Studio model calls tools that discover
  schemas, run structured queries and analytics, or write visible, editable
  Polars. Tool results include compact, unmasked row previews with explicit
  truncation when a result exceeds the model-context limit.
- **Durable audit-assistant runs** — auto or permission-mode runs can build an
  RCM-centric plan, create and execute linked Data/Document Tests, roll up
  outcomes, pause for observation disposition, generate RCM working papers,
  curate a dashboard, and draft and quality-check the report. Mandatory output
  and completion stages remain deterministic even if model expansion fails.
  Runs support SSE replay, pause/resume/cancel, steering, editable approvals,
  restart recovery, and user-safe rerun reconciliation.

## Model context

The workbench is optimized for a locally hosted model:

- Model tools receive schemas, aggregate statistics, and bounded unmasked row
  previews. Entire large populations are never inserted into one prompt.
- Documents attached to assistant questions and planning workflows are included
  automatically within a shared character budget. Citations remain tied to
  immutable source hashes and exact included text.
- No per-engagement consent switch or automatic PII masking is applied. Use
  cloud backends only with synthetic or otherwise appropriate data.
- Deterministic matching and quality checks run locally. Model interpretation
  is presented separately and never replaces stored comparison evidence or
  clears deterministic report-quality failures.

## Current status and roadmap

- V1-V4, M1-M5, and the RCM-central workflow are implemented. See
  [`docs/agent-workflow-plan.md`](docs/agent-workflow-plan.md) and
  [`docs/full-audit-cycle-plan.md`](docs/full-audit-cycle-plan.md) and
  [`docs/rcm-central-workflow-plan.md`](docs/rcm-central-workflow-plan.md) for
  the architecture and acceptance detail. Legacy Audit Program data is retained
  for rollback and exposed through read-only compatibility APIs during the
  migration window; it is no longer an active planning or UI concept.
- The optional M6 stage covers a read-only traceability matrix, explainable
  risk/coverage heatmap, duplicate control/finding suggestions, reusable local
  procedure templates, and configured editorial/translation actions.
- Portable-zip distribution with embedded Python remains a separate packaging
  priority for locked-down corporate machines.

## Configuration

Configuration can live in a local `.env` file at the repository root. Copy
`.env.example` to `.env` and fill in the provider keys you use:

```dotenv
GROQ_API_KEY=your-key
OPENROUTER_API_KEY=your-key
MISTRAL_API_KEY=your-key
OPENCODE_API_KEY=your-key
CEREBRAS_API_KEY=your-key
```

The assistant is optional. Without a configured key, model-assisted actions
show a setup or deterministic-fallback state while local analysis, evidence,
findings, and editing continue to work. Provider/model choices are saved from
Assistant settings. OpenCode Zen defaults to `deepseek-v4-flash-free`, and
Cerebras defaults to `gpt-oss-120b`. LM Studio uses
`http://localhost:1234/v1` and defaults its dummy key to `lm-studio`. Cloud
requests default to 60 seconds and local LM Studio requests to 300 seconds.
The Documents analysis view also exposes a separate vision profile. Known
models use the built-in capability catalog; custom models must explicitly
declare `vision`. Deployments may override that profile with
`AGENT_VISION_PROVIDER`, `AGENT_VISION_MODEL`, and
`AGENT_VISION_CAPABILITIES=vision`.
Real environment variables override `.env`. The LLM transport and dotenv
loader use only the Python standard library.

## Stack

- **Backend** — Python 3.12, FastAPI, Polars (the only data engine),
  XlsxWriter/fastexcel, pypdf, Pillow, and pypdfium2.
- **Frontend** — Vue 3, TypeScript, Vite, PrimeVue 4, and Chart.js.
- **Storage** — local JSON artifacts plus workspace source/evidence files under
  `Workspaces/` by default; set `WORKBENCH_DATA` to override the root. SQLite
  holds the control plane (`workbench.db`) and each workspace's telemetry and
  event logs (`telemetry.db`).

## Development

Install backend dependencies:

```bash
uv venv .venv
uv pip install -r backend/requirements-dev.txt
```

Run the API on port 8000:

```bash
uv run --no-project uvicorn app.main:app --app-dir backend --reload
```

Run the frontend dev server on port 5173 (it proxies `/api` to port 8000):

```bash
cd frontend
npm install
npm run dev
```

Run the acceptance gates:

```bash
# 232 backend tests as of 2026-07-14
uv run --no-project pytest

# TypeScript/Vue gate and production bundle
cd frontend
npm run build
```

## Production build

```bash
cd frontend
npm run build
```

The FastAPI app serves `frontend/dist` automatically when it exists. Run the
backend and open <http://127.0.0.1:8000>.

# Audit Workbench

A local-first audit workbench spanning data analysis, engagement planning,
document evidence, fieldwork, findings, and report drafting. Structured data
is computed on your machine and raw rows never reach an LLM. Document content
can reach a configured model only after an explicit per-engagement opt-in;
every such disclosure is logged.

## Capabilities

- **Workspaces** — one per engagement. Each workspace is persistent and holds
  its source files, table/join definitions, planning artifacts, evidence,
  findings, report, and durable assistant runs.
- **Incremental audit-folder intake** — select a mixed local folder, compare it
  with prior imports, stage only new/changed files, review editable routing
  proposals, and import supported tables and documents idempotently. Changed
  tables retain stable links; changed documents create citation-safe versions.
- **Planning** — capture engagement context through an assistant interview,
  then generate and edit an Audit Planning Memorandum, Risk & Control Matrix,
  and linked audit program. Markdown templates and local methodology packs are
  configurable per workspace.
- **Data** — load multiple CSV/TSV/Excel files with inferred types and define
  named joins across them (left/inner/full/semi/anti).
- **Profile and Query** — automatic typed profiling plus interactive filters,
  group-by aggregations, split-by cross-tabs, sorting, server pagination,
  group-row drill-down, charts, and Excel exports.
- **Analytics** — 15 audit tests: Benford and last-two-digit analysis,
  duplicates, sequence gaps, reproducible sampling, period comparison, round
  numbers, outliers, threshold clustering, weekend postings, date-lag or
  backdating, stratification, completeness, negative/zero scan, and rare
  values. Results can be saved, exported, and pinned.
- **Dashboard** — pin queries, analytics, validation results, or assistant
  output as chart/table tiles. Tiles store reproducible specifications and
  recompute against the current source files.
- **Documents and evidence** — ingest PDF, TXT/Markdown, DOCX, and common image
  formats; preview extracted pages, track versions, ask cited questions, and
  navigate immutable typed evidence anchors. Disclosure and AI-activity logs
  retain provider, model, page, source-hash, and artifact provenance without
  duplicating raw content into the ledger.
- **Document tests and working papers** — perform vouching/tracing, attribute
  testing, document/minutes review, and cited document Q&A. Comparisons remain
  explainable through exact, normalized, fuzzy-similarity, numeric-tolerance,
  and date-tolerance results. Seeded samples freeze key fields, per-item state
  is resumable, and linked procedures render evidence-based Markdown/HTML
  working papers.
- **Findings and reports** — promote an assistant observation into an editable
  draft or add a manual IIA-style finding with condition, criteria, cause,
  effect, recommendation, management response, and typed evidence. Generate an
  editable report from live planning-through-finding context, run advisory
  traceability/quality checks, copy Markdown/HTML, and reconcile regeneration
  side by side so auditor edits are never silently overwritten.
- **Natural-language assistant** — ask questions in plain English. A configured
  Groq, OpenRouter, Mistral, or local LM Studio model calls tools that discover
  schemas, run structured queries and analytics, or write visible, editable
  Polars. Only metadata, aggregate statistics, and aggregated previews are
  returned to the model; raw structured rows stay local.
- **Durable audit-assistant runs** — auto or permission-mode runs can build
  joins, validation rules, analyses, and dashboards; conduct planning
  interviews; classify staged folder imports; and execute selected document
  tests. Runs support SSE replay, pause/resume/cancel, steering, editable
  approvals, restart recovery, and user-safe rerun reconciliation.

## Privacy model

Structured-data and document privacy are separate controls:

- Raw structured rows never leave the device. Model tools receive schemas,
  column names, aggregate statistics, and previews of aggregated results.
- Document pages and methodology excerpts are unavailable to models until
  document AI is explicitly enabled for that workspace. Every disclosure is
  recorded with its purpose, pages, and immutable source hash.
- Deterministic matching and quality checks run locally. Model interpretation
  is presented separately and never replaces stored comparison evidence or
  clears deterministic report-quality failures.

## Current status and roadmap

- V1-V4 and M1-M5 of the full audit-cycle extension are implemented. See
  [`docs/agent-workflow-plan.md`](docs/agent-workflow-plan.md) and
  [`docs/full-audit-cycle-plan.md`](docs/full-audit-cycle-plan.md) for the
  architecture and acceptance detail.
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
Real environment variables override `.env`. The LLM transport and dotenv
loader use only the Python standard library.

## Stack

- **Backend** — Python 3.12, FastAPI, Polars (the only data engine),
  XlsxWriter/fastexcel, and pypdf.
- **Frontend** — Vue 3, TypeScript, Vite, PrimeVue 4, and Chart.js.
- **Storage** — local JSON plus workspace source/evidence files under
  `Workspaces/` by default; set `WORKBENCH_DATA` to override the root.

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

# AGENTS.md

## 1. System Overview

**Purpose:** A **local-first data analysis workbench for auditors**. The unit of
work is a *workspace* (one per engagement): the auditor loads CSV/Excel files,
optionally joins them, then profiles, explores (filter/group/aggregate), and
runs canned audit analytics (Benford, duplicates, sequence gaps, sampling,
period comparison, round numbers). All computation happens on the user's
machine. This project is the ground-up successor to the "TT Rebate Checker"
validation platform (separate repo, same author); the EDA profiler was ported
from there.

**Tech stack:** Python 3.12 · FastAPI · **Polars (the only data engine — no
pandas, no DuckDB)** · XlsxWriter/fastexcel (Excel I/O) — frontend: Vue 3 +
TypeScript + Vite + **PrimeVue 4** (no Tailwind).

**Roadmap context (agreed 2026-07-06):** V2 adds charts + pin-to-dashboard
tiles. V3 (shipped) adds the natural-language **Assistant**: an LLM (**Groq
cloud API, configurable backend**) answers questions by *calling tools* that
run locally — structured queries, the analytics library, and an escape hatch
that runs **visible, editable Polars** (`run_python`). **Only metadata
(schema/column names/aggregate stats and previews of *aggregated* results) is
ever sent to the LLM — never raw data rows**; `assistant._frame_for_model` is
the choke point that withholds row-level detail. A portable-zip distribution
(embedded Python, mirroring the old platform's `build_portable.py`) is
planned because target users are on locked-down corporate PCs — this is why
the LLM transport uses only the standard library (no SDK dependency).

## 2. Architecture

```
backend/app/
├─ main.py                    ── app factory; /api routers; serves frontend/dist
│                                with SPA history fallback when it exists
├─ loader.py                  ── typed CSV/TSV/Excel reads; in-memory cache keyed
│                                by (path, size, mtime) — files can be 100MB+
├─ workspaces.py              ── Workspace model + registry. Storage:
│                                Workspaces/<id>/workspace.json + Data/ files.
│                                Joins are named derived tables (can chain).
├─ profiler.py                ── per-column + dataset profiling (typed-aware)
├─ explore.py                 ── declarative query engine: filters → group/agg →
│                                sort → paginate; also build_crosstab() for the
│                                split_by cross-tab (Excel pivot); frame_payload()
├─ analytics.py               ── ANALYTICS registry: 6 audit tests, each
│                                (df, params) -> AnalyticsResult; param metadata
│                                drives the SPA's dynamic forms; tests suggest a
│                                default viz (e.g. Benford → bar of obs vs exp)
├─ dashboard.py               ── tile computation: re-runs each tile's stored
│                                spec (query / analytics / python) against
│                                current frames; broken tiles degrade to cards
├─ llm.py                     ── LLM transport: OpenAI-compatible chat/tools
│                                over stdlib urllib (no dep), Groq by default;
│                                configured via .env or GROQ_API_KEY/MODEL/BASE_URL
├─ sandbox.py                 ── AST-guarded local Polars executor (no imports,
│                                no dunder/OS); powers run_python + python tiles
├─ assistant.py               ── NL agent: metadata-only context + tool loop
│                                (list_tables, describe_table, query_table,
│                                run_analytics, run_python); gates what the
│                                model sees so raw rows never leave the machine
└─ routes/
   ├─ workspace_routes.py     ── workspace/table/join CRUD
   ├─ analysis_routes.py      ── schema/preview/profile, query (+export),
   │                              analytics run (+export); exports re-run the
   │                              computation and stream xlsx (stateless)
   ├─ dashboard_routes.py     ── tiles CRUD (POST/PATCH/DELETE) + GET dashboard
   └─ assistant_routes.py     ── GET /assistant/status, POST /assistant (ask),
   │                              POST /run-python (execute an edited snippet)

frontend/src/
├─ api.ts                     ── fetch wrapper (ApiError, upload, xlsx download)
├─ types.ts                   ── mirrors backend payload shapes
├─ views/HomeView.vue         ── workspace cards + create/delete
├─ views/WorkspaceView.vue    ── tabs: Dashboard | Data | Query | Analytics |
│                                Assistant
└─ components/
   ├─ DashboardTab.vue        ── pinned tile grid: chart/table + verdict/stats,
   │                             rename/note/reorder/remove; remounts per visit
   ├─ DataTab.vue             ── upload, table list, preview dialog, remove
   ├─ JoinDialog.vue          ── join builder (schemas fetched per side)
   ├─ ProfileTab.vue          ── stat cards + expandable column profiles
   ├─ QueryTab.vue            ── Perspective-style query builder (was ExploreTab;
   │                             absorbed the old Pivot tab):
   │                             draggable Fields → Filters/Group by/Split by/
   │                             Aggregations/Order by zones on the right; live
   │                             (debounced) recompute. Flat query → lazy DataTable
   │                             (server page) with group-row drill-down; a Split
   │                             by field switches it to a cross-tab grid (grouped
   │                             headers + totals). Chart controls (bar/line/pie)
   │                             + Pin-to-dashboard ('query' tile, cross-tab too)
   ├─ AnalyticsTab.vue        ── test cards → dynamic param form → result → Pin
   ├─ AssistantTab.vue        ── NL chat: question → tool-step trace → answer +
   │                             artifacts (charts, editable+re-runnable Python)
   │                             → Pin; banner when the LLM isn't configured
   ├─ ChartView.vue           ── Chart.js renderer for a frame + VizSpec
   │                             (falls back to FrameTable for 'table' viz)
   ├─ PinDialog.vue           ── title + note prompt shared by both pin flows
   └─ FrameTable.vue          ── renders a {columns, dtypes, rows} payload
```

**Key conventions:**
- User-facing errors are `WorkspaceError`/`QueryError` (ValueError subclasses);
  `main.py` maps both to HTTP 400 with `{"detail": msg}`. Raise those, never
  bare HTTPException, from core modules.
- Everything is stateless per request: frames come from the loader cache;
  no session objects. Exports re-run the computation.
- API payload for tabular data is always `{columns, dtypes, rows}` (row
  arrays, JSON-safe values, dates as ISO strings) via `explore.frame_payload`.
- Analytics tests return `AnalyticsResult` (verdict ok/warn/fail/info, stat
  chips, summary frame, optional detail frame). Register new tests in
  `ANALYTICS` with param metadata (`kind`: column/columns/number/select) and
  the SPA form renders itself.
- `Workspaces/` is gitignored per-user data. `WORKBENCH_DATA` env var
  overrides its location.

## 3. Commands

```bash
# Backend deps (Python 3.12)
uv venv .venv
uv pip install -r backend/requirements-dev.txt

# Tests (69 tests: workspaces, explore incl. cross-tab, analytics, dashboard,
#        assistant, API)
cd backend && uv run --no-project pytest

# Enable the assistant (optional; unset == graceful "not configured" banner)
# Copy .env.example to .env and set GROQ_API_KEY (optional: model/base URL)

# Dev servers
uv run --no-project uvicorn app.main:app --app-dir backend --reload       # API :8000
cd frontend && npm run dev                                                # SPA :5173 (proxies /api)

# Production build (then run.bat serves everything on :8000)
cd frontend && npm run build
```

## 4. State & Next Steps

- V1 complete: workspaces, multi-file load, joins, profiling, explore,
  6 analytics tests, Excel exports, full backend test suite.
- V2 complete: Chart.js charts in Explore, pinned dashboard (spec-storing
  tiles, live recompute, per-tile error degradation), Dashboard tab is the
  landing tab when tiles exist.
- V3 complete: NL Assistant with a Groq tool-calling loop (list_tables,
  describe_table, query_table, run_analytics, run_python), AST-sandboxed
  local Polars, metadata-only guarantee (verified on real 604-row data: raw
  run_python results reach the browser in full but the model sees only
  shape/stats), and a new **python** tile kind so NL→Python results pin as
  live dashboard tiles.
- No linter configured; `npm run build` runs vue-tsc as the frontend type gate.
- Next: portable-zip build (embedded Python) for distribution.
- Gotcha: PrimeVue Select options ignore bare synthetic `.click()` — driving
  the UI programmatically needs the full pointerdown/mousedown/pointerup/
  mouseup/click sequence. The Claude preview tool's preview_click does not
  deliver events on this machine; use preview_eval instead.

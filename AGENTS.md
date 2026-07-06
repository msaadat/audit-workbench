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
tiles. V3 adds natural-language querying: an LLM (**Groq cloud API,
configurable backend**) generates **visible, editable Python** executed
locally; **only metadata (schema/column names/aggregate stats) may ever be
sent to the LLM — never raw data rows**. A portable-zip distribution
(embedded Python, mirroring the old platform's `build_portable.py`) is
planned because target users are on locked-down corporate PCs.

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
│                                sort → paginate; frame_payload() JSON serializer
├─ analytics.py               ── ANALYTICS registry: 6 audit tests, each
│                                (df, params) -> AnalyticsResult; param metadata
│                                drives the SPA's dynamic forms
└─ routes/
   ├─ workspace_routes.py     ── workspace/table/join CRUD
   └─ analysis_routes.py      ── schema/preview/profile, query (+export),
                                  analytics run (+export); exports re-run the
                                  computation and stream xlsx (stateless)

frontend/src/
├─ api.ts                     ── fetch wrapper (ApiError, upload, xlsx download)
├─ types.ts                   ── mirrors backend payload shapes
├─ views/HomeView.vue         ── workspace cards + create/delete
├─ views/WorkspaceView.vue    ── tabs: Data | Profile | Explore | Analytics
└─ components/
   ├─ DataTab.vue             ── upload, table list, preview dialog, remove
   ├─ JoinDialog.vue          ── join builder (schemas fetched per side)
   ├─ ProfileTab.vue          ── stat cards + expandable column profiles
   ├─ ExploreTab.vue          ── filter/group/agg builders; lazy DataTable
   │                             (server-side page+sort); group row click =
   │                             drill-down to underlying rows
   ├─ AnalyticsTab.vue        ── test cards → dynamic param form → result
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
python -m venv .venv
.venv\Scripts\pip install -r backend\requirements-dev.txt

# Tests (56 tests: workspaces, explore, analytics, API)
cd backend && ..\.venv\Scripts\python -m pytest

# Dev servers
.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --reload   # API :8000
cd frontend && npm run dev                                                # SPA :5173 (proxies /api)

# Production build (then run.bat serves everything on :8000)
cd frontend && npm run build
```

## 4. State & Next Steps

- V1 complete: workspaces, multi-file load, joins, profiling, explore,
  6 analytics tests, Excel exports, full backend test suite.
- No linter configured; `npm run build` runs vue-tsc as the frontend type gate.
- Next: V2 charts/pinned tiles, then V3 NL→Python (Groq, metadata-only),
  portable-zip build for distribution.

# Audit Workbench

A local-first data analysis workbench for auditors. Drop in an engagement's
CSV/Excel files and profile, explore, and test the data — everything runs on
your own machine; no data leaves it.

## Capabilities (V1)

- **Workspaces** — one per engagement. Holds the data files plus table/join
  definitions (`Workspaces/<id>/workspace.json`). Persistent and reopenable.
- **Data** — load multiple CSV/TSV/Excel files (types inferred), define named
  joins across them (left/inner/full/semi/anti).
- **Profile** — automatic per-column profiling: inferred type, blanks,
  cardinality, ranges, top values, duplicate-row count.
- **Explore** — interactive slicing: filters, group-by aggregations, sorting,
  server-side pagination, drill-down from a group to its rows, Excel export.
- **Analytics** — canned audit tests: Benford's law (MAD/chi-square),
  duplicate detection, sequence-gap analysis, reproducible sampling
  (random / interval / stratified), period-over-period comparison, and
  round-number analysis. Results export to Excel.

## Roadmap

- **V2** — charts and a pin-to-dashboard workspace view.
- **V3** — natural-language querying: an LLM (Groq cloud API, configurable)
  generates visible, editable Python that executes locally. **Only metadata
  (schema, column names, aggregate stats) is ever sent to the API — never
  raw rows.**
- **Later** — portable-zip distribution (embedded Python, no installs) for
  locked-down corporate machines; promote findings into validation rules.

## Stack

- **Backend** — Python 3.12, FastAPI, Polars (the only data engine).
- **Frontend** — Vue 3 + TypeScript + Vite + PrimeVue.

## Development

Backend (API on :8000):

```bash
python -m venv .venv
.venv\Scripts\pip install -r backend\requirements-dev.txt
.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --reload
```

Frontend (Vite dev server on :5173, proxies /api to :8000):

```bash
cd frontend
npm install
npm run dev
```

Tests:

```bash
cd backend
..\.venv\Scripts\python -m pytest
```

## Production build

```bash
cd frontend && npm run build
```

The FastAPI app serves `frontend/dist` automatically when it exists — run
just the backend and open http://127.0.0.1:8000.

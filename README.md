# Audit Workbench

A local-first data analysis workbench for auditors. Drop in an engagement's
CSV/Excel files and profile, explore, and test the data — everything runs on
your own machine; no data leaves it.

## Capabilities

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
- **Dashboard** — pin any Explore query, Analytics test, or Assistant result
  as a tile (bar/line/pie chart or table). Tiles store the *spec*, not the
  data: the dashboard re-runs everything against the current files on every
  load, so it stays live and every tile is reproducible. Tiles can be
  renamed, annotated, reordered, and removed.
- **Assistant** — ask questions in plain English. An LLM (Groq cloud API,
  configurable) answers by *calling tools* that run on your machine: it
  discovers the schema, runs structured queries and the analytics tests, and
  for anything bespoke writes **visible, editable Polars** you can tweak and
  re-run. **Only metadata — schema, column names, aggregate statistics, and
  previews of aggregated results — is ever sent to the model; raw data rows
  never leave the machine.** Any answer can be pinned to the dashboard.
  Without a key configured, the tab explains how to enable it and everything
  else keeps working.

## Roadmap

- **Later** — portable-zip distribution (embedded Python, no installs) for
  locked-down corporate machines; promote findings into validation rules.

## Configuration

Configuration can live in a local `.env` file at the repo root. Copy
`.env.example` to `.env` and fill in the values you need:

```dotenv
GROQ_API_KEY=your-key
GROQ_MODEL=llama-3.3-70b-versatile
GROQ_BASE_URL=https://api.groq.com/openai/v1
```

The assistant is optional; without `GROQ_API_KEY`, the tab shows a setup
message and the rest of the app keeps working. `GROQ_BASE_URL` accepts any
OpenAI-compatible endpoint (e.g. a self-hosted model), so the LLM backend is
configurable per deployment. Real environment variables override `.env` values.
The transport and dotenv loader both use only the Python standard library, so
there is no extra dependency to bundle.

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

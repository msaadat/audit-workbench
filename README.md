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
- **Assistant** — ask questions in plain English. An LLM (Groq, OpenRouter, or
  local LM Studio, configurable) answers by *calling tools* that run on your machine: it
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
# Groq (default)
GROQ_API_KEY=your-key
GROQ_MODEL=llama-3.3-70b-versatile

# Or OpenRouter
LLM_BACKEND=openrouter
OPENROUTER_API_KEY=your-key
OPENROUTER_MODEL=~openai/gpt-latest

# Or local LM Studio
LLM_BACKEND=lmstudio
LMSTUDIO_MODEL=
LMSTUDIO_BASE_URL=http://localhost:1234/v1
```

The assistant is optional; without a configured API key, the tab shows a setup
message and the rest of the app keeps working. Groq uses `GROQ_BASE_URL`
(`https://api.groq.com/openai/v1` by default). OpenRouter uses
`OPENROUTER_BASE_URL` (`https://openrouter.ai/api/v1` by default) and accepts
optional attribution headers via `OPENROUTER_APP_TITLE` and
`OPENROUTER_HTTP_REFERER`; `OPENROUTER_MODEL` defaults to
`~openai/gpt-latest`. LM Studio uses `LMSTUDIO_BASE_URL`
(`http://localhost:1234/v1` by default), optional `LMSTUDIO_MODEL`, and a local
dummy `LMSTUDIO_API_KEY` default of `lm-studio`; leave `LMSTUDIO_MODEL` blank
to use whichever model is loaded in LM Studio. LLM requests default to 60
seconds for cloud backends and 300 seconds for LM Studio; override with
`LLM_REQUEST_TIMEOUT` if your local model needs longer. Real environment
variables override `.env` values. The transport and dotenv loader both use only
the Python standard library, so there is no extra dependency to bundle.

## Stack

- **Backend** — Python 3.12, FastAPI, Polars (the only data engine).
- **Frontend** — Vue 3 + TypeScript + Vite + PrimeVue.

## Development

Backend (API on :8000):

```bash
uv venv .venv
uv pip install -r backend/requirements-dev.txt
uv run --no-project uvicorn app.main:app --app-dir backend --reload
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
uv run --no-project pytest
```

## Production build

```bash
cd frontend && npm run build
```

The FastAPI app serves `frontend/dist` automatically when it exists — run
just the backend and open http://127.0.0.1:8000.

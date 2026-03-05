# Design: FastAPI Session Analyzer

**Date:** 2026-03-05
**Scope:** Local single-user tool for uploading Claude Code `.jsonl` session logs, parsing them into DuckDB, and providing trajectory visualization, session reports, and SQL console.

## Architecture

```
claudexpt/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, routes, startup
│   ├── parser.py            # JSONL → condensed trajectory + report data
│   ├── db.py                # DuckDB session management (create/query)
│   └── templates/
│       ├── index.html        # Upload form + session list (Jinja2)
│       ├── trajectory.html   # Trajectory viewer (adapted from existing)
│       ├── report.html       # Session report (adapted from existing)
│       └── sql.html          # Interactive SQL console
├── static/                   # Shared CSS/JS assets
├── sessions/                 # Persisted DuckDB files (one .db per upload)
├── pyproject.toml
└── README.md
```

## User Flow

1. Visit `/` → upload form + list of previously uploaded sessions
2. Upload `.jsonl` via `POST /upload` → parser extracts session name, creates `sessions/{name}.db`
3. Redirect to `/session/{name}/trajectory`
4. Navigate between trajectory / report / sql views per session

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET /` | Landing page: upload + session list |
| `POST /upload` | Upload `.jsonl`, parse, persist, redirect |
| `GET /session/{name}/trajectory` | Trajectory viewer page |
| `GET /session/{name}/report` | Session report page |
| `GET /session/{name}/sql` | SQL console page |
| `GET /api/{name}/trajectory` | JSON: trajectory node array |
| `GET /api/{name}/report` | JSON: KPIs, tool usage, cost breakdown |
| `GET /api/{name}/query` | `?sql=...` → JSON rows from DuckDB |
| `DELETE /api/{name}` | Delete a session and its .db file |

## Parser Pipeline (`parser.py`)

Consolidates the ad-hoc scripts (`build_trajectory.py`, `build_graph_data.py`) into a single module:

1. **Read JSONL** — stream lines, parse JSON
2. **Insert raw rows** into DuckDB `raw` table
3. **Build trajectory** — extract uuid, parentUuid, type, timestamp, content items (text/tool_use/tool_result), tool progress fields → `traj` table
4. **Compute lanes** — git-graph lane assignment algorithm
5. **Compute report KPIs** — aggregate tokens, tool counts, cost, duration

## Frontend Changes

- **trajectory.html**: Replace static `<script src="trajectory_data.js">` with `fetch(/api/{name}/trajectory)`. All visualization JS stays the same.
- **report.html**: Fetch KPIs from `/api/{name}/report`, render with existing chart code.
- **sql.html**: New — textarea + submit → `/api/{name}/query` → render result table.
- **index.html**: New Jinja2 page — dropzone upload, session list with links, delete button.

## Dependencies

```
fastapi
uvicorn[standard]
duckdb
python-multipart
jinja2
```

No Node.js. No build step. Run with `uvicorn app.main:app`.

## Storage

One DuckDB file per session in `sessions/`. Session name derived from the JSONL data (`sessionId` field or filename). Files persist across restarts.

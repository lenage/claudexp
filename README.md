# claudexp

Local web app for analyzing Claude Code session logs. Upload `.jsonl` files to explore conversation trajectories as git-style graphs, view token/cost reports, and query raw data with SQL.

## Features

**Trajectory Viewer** — Git-graph style timeline with Canvas 2D rendering. Variable row heights based on time gaps between events. Playback controls, arrow key stepping, search. Click any node to load full untrimmed content from DuckDB.

**Session Report** — KPI cards (events, turns, tokens, cost), hourly activity timeline chart, event type breakdown, tool usage stats, token usage with per-category cost.

**SQL Console** — Run arbitrary SQL against the raw event data and processed trajectory tables.

## Quick Start (Docker)

```
docker compose up
```

Opens at [http://localhost:8765](http://localhost:8765). Session data persists in a Docker volume.

## Local Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run claudexpt
```

Opens at [http://127.0.0.1:8765](http://127.0.0.1:8765). Upload a Claude Code `.jsonl` session log from the home page.

Session logs are typically found at `~/.claude/projects/*/`.

## Project Structure

```
app/
  main.py          FastAPI routes and CLI entry point
  db.py            DuckDB session manager
  parser.py        JSONL parser, trajectory builder, report queries
  templates/
    index.html       Upload + session list
    trajectory.html  Git-graph trajectory viewer
    report.html      Session report with charts
    sql.html         SQL console
sessions/            Persisted DuckDB files (one per upload)
```

## Tech Stack

FastAPI, DuckDB, Jinja2, vanilla JS + Canvas 2D. No build step.

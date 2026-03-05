# FastAPI Session Analyzer — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Convert the static trajectory/report viewer into a FastAPI web app where users upload `.jsonl` session logs, which get parsed into DuckDB and served via API to the existing frontend.

**Architecture:** FastAPI monolith serving Jinja2 templates + JSON APIs. One DuckDB file per session persisted in `sessions/`. Parser consolidates the ad-hoc `build_trajectory.py` and `build_graph_data.py` into a single module. Frontend HTML adapted to fetch data from API instead of static JS files.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, DuckDB, Jinja2, python-multipart

---

### Task 1: Project scaffolding and dependencies

**Files:**
- Create: `pyproject.toml`
- Create: `app/__init__.py`
- Create: `app/main.py` (skeleton)

**Step 1: Create pyproject.toml**

```toml
[project]
name = "claudexpt"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "duckdb>=1.2",
    "python-multipart>=0.0.18",
    "jinja2>=3.1",
]

[project.scripts]
claudexpt = "app.main:cli"
```

**Step 2: Create app/__init__.py**

Empty file.

**Step 3: Create minimal app/main.py**

```python
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent.parent
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Claude Session Analyzer")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/")
async def index():
    return {"status": "ok"}


def cli():
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8765, reload=True)
```

**Step 4: Create directories**

```bash
mkdir -p app/templates static sessions
```

**Step 5: Install and verify**

```bash
pip install -e .
uvicorn app.main:app --port 8765 &
curl http://localhost:8765/
# Expected: {"status":"ok"}
kill %1
```

**Step 6: Commit**

```bash
git add pyproject.toml app/__init__.py app/main.py
git commit -m "feat: scaffold FastAPI project with dependencies"
```

---

### Task 2: DuckDB session manager (`app/db.py`)

**Files:**
- Create: `app/db.py`

**Step 1: Write db.py**

```python
import duckdb
from pathlib import Path

SESSIONS_DIR: Path  # set by main.py at startup


def init(sessions_dir: Path):
    global SESSIONS_DIR
    SESSIONS_DIR = sessions_dir
    SESSIONS_DIR.mkdir(exist_ok=True)


def db_path(name: str) -> Path:
    return SESSIONS_DIR / f"{name}.db"


def exists(name: str) -> bool:
    return db_path(name).is_file()


def connect(name: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path(name)), read_only=True)


def connect_rw(name: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path(name)))


def list_sessions() -> list[dict]:
    """Return list of {name, size_mb, created} for all .db files."""
    sessions = []
    for p in sorted(SESSIONS_DIR.glob("*.db")):
        sessions.append({
            "name": p.stem,
            "size_mb": round(p.stat().st_size / 1_048_576, 1),
        })
    return sessions


def delete_session(name: str):
    p = db_path(name)
    if p.is_file():
        p.unlink()


def query(name: str, sql: str) -> dict:
    """Execute SQL, return {columns: [...], rows: [...]}."""
    con = connect(name)
    try:
        result = con.execute(sql)
        columns = [desc[0] for desc in result.description]
        rows = [list(row) for row in result.fetchall()]
        return {"columns": columns, "rows": rows}
    finally:
        con.close()
```

**Step 2: Commit**

```bash
git add app/db.py
git commit -m "feat: add DuckDB session manager"
```

---

### Task 3: JSONL parser (`app/parser.py`)

This is the core — consolidates build_trajectory.py + build_graph_data.py into one module.

**Files:**
- Create: `app/parser.py`

**Step 1: Write parser.py**

```python
"""Parse Claude Code .jsonl session logs into DuckDB."""
import json
from pathlib import Path
from . import db


def parse_jsonl(name: str, jsonl_path: Path):
    """Parse a .jsonl file and create a DuckDB database with raw + traj tables."""
    lines = jsonl_path.read_text().strip().split("\n")
    rows = [json.loads(line) for line in lines if line.strip()]

    con = db.connect_rw(name)
    try:
        _insert_raw(con, rows)
        traj = _build_trajectory(rows)
        traj = _assign_lanes(traj)
        _insert_traj(con, traj)
    finally:
        con.close()


def _insert_raw(con, rows: list[dict]):
    """Insert raw JSONL rows into a 'raw' table."""
    con.execute("DROP TABLE IF EXISTS raw")
    con.execute("""
        CREATE TABLE raw (
            seq INTEGER,
            type VARCHAR,
            uuid VARCHAR,
            parent_uuid VARCHAR,
            session_id VARCHAR,
            timestamp VARCHAR,
            is_sidechain BOOLEAN,
            message JSON,
            raw JSON
        )
    """)
    for i, r in enumerate(rows):
        con.execute(
            "INSERT INTO raw VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                i + 1,
                r.get("type"),
                r.get("uuid"),
                r.get("parentUuid"),
                r.get("sessionId"),
                r.get("timestamp"),
                r.get("isSidechain", False),
                json.dumps(r.get("message")) if r.get("message") else None,
                json.dumps(r),
            ],
        )


def _build_trajectory(rows: list[dict]) -> list[dict]:
    """Convert raw rows into trajectory nodes with parsed content items."""
    nodes = []
    seq = 0
    for r in rows:
        rtype = r.get("type")
        if rtype not in ("user", "assistant", "progress"):
            continue
        seq += 1
        node = {
            "id": (r.get("uuid") or "")[:8],
            "fid": r.get("uuid"),
            "fpid": r.get("parentUuid"),
            "type": rtype,
            "ts": r.get("timestamp"),
            "seq": seq,
        }

        if rtype in ("user", "assistant"):
            node["items"] = _parse_content(r)
        elif rtype == "progress":
            msg = r.get("message") or {}
            # Progress events store tool info at top level or in message
            node["progressType"] = r.get("progressType") or msg.get("type")
            node["toolName"] = r.get("toolName") or msg.get("tool")
            node["toolStatus"] = r.get("toolStatus") or msg.get("status")
            tid = r.get("toolUseId") or msg.get("tool_use_id") or ""
            node["toolUseId"] = tid[:20] if tid else None

        nodes.append(node)
    return nodes


def _parse_content(row: dict) -> list[dict]:
    """Extract text/tool_use/tool_result items from message content."""
    msg = row.get("message") or {}
    content = msg.get("content")
    if not content:
        return []
    if isinstance(content, str):
        return [{"kind": "text", "preview": content[:120]}]
    if not isinstance(content, list):
        return []

    items = []
    for item in content:
        if isinstance(item, str):
            items.append({"kind": "text", "preview": item[:120]})
            continue
        if not isinstance(item, dict):
            continue

        itype = item.get("type")
        if itype == "text":
            items.append({"kind": "text", "preview": (item.get("text") or "")[:120]})
        elif itype == "tool_use":
            inp = item.get("input") or {}
            params = {}
            if isinstance(inp, dict):
                for k, v in inp.items():
                    sv = str(v)
                    params[k] = sv[:80] + ("..." if len(sv) > 80 else "")
            items.append({
                "kind": "tool_use",
                "toolId": (item.get("id") or "")[:20],
                "name": item.get("name", ""),
                "params": params,
            })
        elif itype == "tool_result":
            text = ""
            rc = item.get("content")
            if isinstance(rc, list):
                for c in rc:
                    if isinstance(c, dict) and c.get("text"):
                        text = c["text"][:100]
                        break
            elif isinstance(rc, str):
                text = rc[:100]
            items.append({
                "kind": "tool_result",
                "toolUseId": (item.get("tool_use_id") or "")[:20],
                "preview": text,
                "isError": item.get("is_error", False),
            })
    return items


def _assign_lanes(nodes: list[dict]) -> list[dict]:
    """Git-graph lane assignment. Adds 'lane' and 'pi' (parent index) to each node."""
    n = len(nodes)
    fid_to_idx = {}
    for i, nd in enumerate(nodes):
        if nd.get("fid"):
            fid_to_idx[nd["fid"]] = i

    children_map: dict[int, list[int]] = {}
    for i, nd in enumerate(nodes):
        fpid = nd.get("fpid")
        if fpid and fpid in fid_to_idx:
            pi = fid_to_idx[fpid]
            children_map.setdefault(pi, []).append(i)

    lane_of = [0] * n
    max_lane = 0
    active_lanes: set[int] = set()

    def get_free_lane(preferred: int = 0) -> int:
        nonlocal max_lane
        if preferred not in active_lanes:
            return preferred
        for delta in range(1, max_lane + 3):
            for candidate in [preferred + delta, preferred - delta]:
                if candidate >= 0 and candidate not in active_lanes:
                    return candidate
        max_lane += 1
        return max_lane

    roots = [i for i, nd in enumerate(nodes) if not nd.get("fpid") or nd["fpid"] not in fid_to_idx]
    for i in roots:
        lane_of[i] = 0
    active_lanes.add(0)

    for i in range(n):
        nd = nodes[i]
        fpid = nd.get("fpid")
        if fpid and fpid in fid_to_idx:
            pi = fid_to_idx[fpid]
            parent_lane = lane_of[pi]
            siblings = children_map.get(pi, [])
            if len(siblings) == 1:
                lane_of[i] = parent_lane
            else:
                idx_in_siblings = siblings.index(i)
                if idx_in_siblings == 0:
                    lane_of[i] = parent_lane
                else:
                    lane_of[i] = get_free_lane(parent_lane + idx_in_siblings)
                    active_lanes.add(lane_of[i])
        else:
            if i > 0:
                lane_of[i] = get_free_lane(0)
                active_lanes.add(lane_of[i])

        if i not in children_map:
            active_lanes.discard(lane_of[i])

    # Normalize lanes
    used = sorted(set(lane_of))
    remap = {l: idx for idx, l in enumerate(used)}
    lane_of = [remap[l] for l in lane_of]

    # Write back
    for i, nd in enumerate(nodes):
        nd["lane"] = lane_of[i]
        fpid = nd.get("fpid")
        if fpid and fpid in fid_to_idx:
            nd["pi"] = fid_to_idx[fpid]

    return nodes


def get_session_name(jsonl_path: Path) -> str:
    """Extract session name from first few lines of JSONL (look for sessionId)."""
    with open(jsonl_path) as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            sid = row.get("sessionId")
            if sid:
                return sid[:12]  # Short enough for URL
    return jsonl_path.stem


def get_report_data(name: str) -> dict:
    """Compute session report KPIs from raw table."""
    con = db.connect(name)
    try:
        # Basic counts
        total = con.execute("SELECT count(*) FROM raw").fetchone()[0]
        type_counts = dict(
            con.execute("SELECT type, count(*) FROM raw GROUP BY type ORDER BY count(*) DESC").fetchall()
        )

        # Time range
        ts = con.execute(
            "SELECT min(timestamp), max(timestamp) FROM raw WHERE timestamp IS NOT NULL"
        ).fetchone()
        ts_min, ts_max = ts[0], ts[1]

        # Token usage from assistant messages
        token_sql = """
            SELECT
                coalesce(sum(json_extract(message, '$.usage.input_tokens')::BIGINT), 0),
                coalesce(sum(json_extract(message, '$.usage.output_tokens')::BIGINT), 0),
                coalesce(sum(json_extract(message, '$.usage.cache_creation_input_tokens')::BIGINT), 0),
                coalesce(sum(json_extract(message, '$.usage.cache_read_input_tokens')::BIGINT), 0)
            FROM raw WHERE type = 'assistant' AND message IS NOT NULL
        """
        tok = con.execute(token_sql).fetchone()
        input_tok, output_tok, cache_create_tok, cache_read_tok = tok

        # Tool usage from progress events
        tool_sql = """
            SELECT
                json_extract_string(raw, '$.toolName') as tool,
                count(*) as cnt
            FROM raw
            WHERE type = 'progress'
              AND json_extract_string(raw, '$.toolStatus') = 'completed'
              AND json_extract_string(raw, '$.toolName') IS NOT NULL
            GROUP BY tool ORDER BY cnt DESC
        """
        tools = [{"name": r[0], "count": r[1]} for r in con.execute(tool_sql).fetchall()]

        # Cost estimate (Claude Opus 4.5 Bedrock pricing)
        cost = (
            input_tok * 5 / 1_000_000
            + output_tok * 25 / 1_000_000
            + cache_create_tok * 6.25 / 1_000_000
            + cache_read_tok * 0.50 / 1_000_000
        )

        return {
            "total_events": total,
            "type_counts": type_counts,
            "ts_min": ts_min,
            "ts_max": ts_max,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cache_creation_tokens": cache_create_tok,
            "cache_read_tokens": cache_read_tok,
            "total_tokens": input_tok + output_tok + cache_create_tok + cache_read_tok,
            "estimated_cost": round(cost, 2),
            "tools": tools,
        }
    finally:
        con.close()
```

**Step 2: Commit**

```bash
git add app/parser.py
git commit -m "feat: add JSONL parser with trajectory builder and lane assignment"
```

---

### Task 4: FastAPI routes (`app/main.py`)

**Files:**
- Modify: `app/main.py`

**Step 1: Write full main.py with all routes**

```python
import json
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .parser import get_report_data, get_session_name, parse_jsonl

BASE_DIR = Path(__file__).resolve().parent.parent
SESSIONS_DIR = BASE_DIR / "sessions"

app = FastAPI(title="Claude Session Analyzer")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def startup():
    db.init(SESSIONS_DIR)


# ── Pages ──


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    sessions = db.list_sessions()
    return templates.TemplateResponse("index.html", {"request": request, "sessions": sessions})


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    tmp = SESSIONS_DIR / f"_upload_{file.filename}"
    try:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(file.file, f)
        name = get_session_name(tmp)
        parse_jsonl(name, tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return RedirectResponse(url=f"/session/{name}/trajectory", status_code=303)


@app.get("/session/{name}/trajectory", response_class=HTMLResponse)
async def trajectory_page(request: Request, name: str):
    if not db.exists(name):
        return HTMLResponse("Session not found", status_code=404)
    return templates.TemplateResponse("trajectory.html", {"request": request, "name": name})


@app.get("/session/{name}/report", response_class=HTMLResponse)
async def report_page(request: Request, name: str):
    if not db.exists(name):
        return HTMLResponse("Session not found", status_code=404)
    return templates.TemplateResponse("report.html", {"request": request, "name": name})


@app.get("/session/{name}/sql", response_class=HTMLResponse)
async def sql_page(request: Request, name: str):
    if not db.exists(name):
        return HTMLResponse("Session not found", status_code=404)
    return templates.TemplateResponse("sql.html", {"request": request, "name": name})


# ── API ──


@app.get("/api/{name}/trajectory")
async def trajectory_data(name: str):
    if not db.exists(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    con = db.connect(name)
    try:
        rows = con.execute("SELECT * FROM traj ORDER BY seq").fetchall()
        cols = [d[0] for d in con.description]
        nodes = []
        for row in rows:
            node = dict(zip(cols, row))
            # Parse JSON string fields back
            for field in ("items",):
                if isinstance(node.get(field), str):
                    node[field] = json.loads(node[field])
            nodes.append(node)
        return nodes
    finally:
        con.close()


@app.get("/api/{name}/report")
async def report_data(name: str):
    if not db.exists(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    return get_report_data(name)


@app.get("/api/{name}/query")
async def query_data(name: str, sql: str):
    if not db.exists(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        return db.query(name, sql)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/{name}")
async def delete_session(name: str):
    db.delete_session(name)
    return {"deleted": name}


def cli():
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8765, reload=True)
```

**Step 2: Update parser.py _insert_traj to store nodes in DuckDB**

Add this function to `app/parser.py`:

```python
def _insert_traj(con, nodes: list[dict]):
    """Insert trajectory nodes into traj table."""
    con.execute("DROP TABLE IF EXISTS traj")
    con.execute("""
        CREATE TABLE traj (
            seq INTEGER,
            id VARCHAR,
            fid VARCHAR,
            fpid VARCHAR,
            type VARCHAR,
            ts VARCHAR,
            lane INTEGER,
            pi INTEGER,
            items JSON,
            toolName VARCHAR,
            toolStatus VARCHAR,
            toolUseId VARCHAR,
            progressType VARCHAR
        )
    """)
    for nd in nodes:
        con.execute(
            "INSERT INTO traj VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                nd.get("seq"),
                nd.get("id"),
                nd.get("fid"),
                nd.get("fpid"),
                nd.get("type"),
                nd.get("ts"),
                nd.get("lane"),
                nd.get("pi"),
                json.dumps(nd.get("items")) if nd.get("items") else None,
                nd.get("toolName"),
                nd.get("toolStatus"),
                nd.get("toolUseId"),
                nd.get("progressType"),
            ],
        )
```

**Step 3: Commit**

```bash
git add app/main.py app/parser.py
git commit -m "feat: add FastAPI routes and traj table insertion"
```

---

### Task 5: Landing page template (`app/templates/index.html`)

**Files:**
- Create: `app/templates/index.html`

**Step 1: Write index.html**

Minimal Jinja2 page with upload form and session list. Dark theme matching existing pages.

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Claude Session Analyzer</title>
<style>
  :root { --bg: #0a0a0f; --surface: #111118; --border: #1a1a2e; --text: #ccc; --muted: #555; --accent: #f59e0b; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: "SF Mono","Fira Code",monospace; display: flex; justify-content: center; padding: 60px 20px; }
  .wrap { max-width: 640px; width: 100%; }
  h1 { font-size: 20px; color: #fff; margin-bottom: 8px; }
  .sub { font-size: 12px; color: var(--muted); margin-bottom: 32px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 24px; margin-bottom: 24px; }
  h2 { font-size: 13px; color: #fff; margin-bottom: 16px; text-transform: uppercase; letter-spacing: 0.05em; }
  .drop { border: 2px dashed var(--border); border-radius: 8px; padding: 32px; text-align: center; cursor: pointer; transition: border-color 0.2s; }
  .drop:hover, .drop.over { border-color: var(--accent); }
  .drop input { display: none; }
  .drop p { font-size: 13px; color: var(--muted); }
  .drop .hint { font-size: 11px; color: #333; margin-top: 8px; }
  .sessions { list-style: none; }
  .sessions li { display: flex; align-items: center; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--border); }
  .sessions li:last-child { border: none; }
  .sname { font-size: 13px; color: #fff; }
  .ssize { font-size: 11px; color: var(--muted); margin-left: 12px; }
  .slinks { display: flex; gap: 10px; }
  .slinks a { font-size: 11px; color: var(--accent); text-decoration: none; }
  .slinks a:hover { text-decoration: underline; }
  .del-btn { background: none; border: none; color: #555; cursor: pointer; font-size: 11px; }
  .del-btn:hover { color: #f87171; }
  .empty { font-size: 12px; color: #333; text-align: center; padding: 16px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Claude Session Analyzer</h1>
  <p class="sub">Upload a Claude Code .jsonl session log to explore its trajectory, metrics, and data.</p>

  <div class="card">
    <h2>Upload Session</h2>
    <form id="upload-form" action="/upload" method="post" enctype="multipart/form-data">
      <label class="drop" id="dropzone">
        <input type="file" name="file" accept=".jsonl" required>
        <p>Drop .jsonl file here or click to browse</p>
        <p class="hint">Claude Code session logs (type: user/assistant/progress)</p>
      </label>
    </form>
  </div>

  <div class="card">
    <h2>Sessions</h2>
    {% if sessions %}
    <ul class="sessions">
      {% for s in sessions %}
      <li>
        <div>
          <span class="sname">{{ s.name }}</span>
          <span class="ssize">{{ s.size_mb }} MB</span>
        </div>
        <div class="slinks">
          <a href="/session/{{ s.name }}/trajectory">Trajectory</a>
          <a href="/session/{{ s.name }}/report">Report</a>
          <a href="/session/{{ s.name }}/sql">SQL</a>
          <button class="del-btn" onclick="del('{{ s.name }}')">Delete</button>
        </div>
      </li>
      {% endfor %}
    </ul>
    {% else %}
    <p class="empty">No sessions yet. Upload a .jsonl file to get started.</p>
    {% endif %}
  </div>
</div>
<script>
const dz = document.getElementById('dropzone');
const form = document.getElementById('upload-form');
const input = form.querySelector('input[type=file]');
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('over'); });
dz.addEventListener('dragleave', () => dz.classList.remove('over'));
dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('over'); input.files = e.dataTransfer.files; form.submit(); });
input.addEventListener('change', () => { if (input.files.length) form.submit(); });
async function del(name) {
  if (!confirm('Delete session ' + name + '?')) return;
  await fetch('/api/' + name, { method: 'DELETE' });
  location.reload();
}
</script>
</body>
</html>
```

**Step 2: Commit**

```bash
git add app/templates/index.html
git commit -m "feat: add landing page with upload and session list"
```

---

### Task 6: Adapt trajectory.html as Jinja template

**Files:**
- Create: `app/templates/trajectory.html` (adapted from `data/trajectory.html`)

**Step 1: Copy and adapt**

The key change: replace `<script src="trajectory_data.js">` with a fetch call, and inject session name via Jinja `{{ name }}`.

At the top, add nav links. Wrap the existing IIFE in an async loader:

```js
// Replace:
//   <script src="trajectory_data.js"></script>
//   <script>(function(){ const data = TRAJECTORY_DATA; ... })();</script>

// With:
//   <script>
//   (async function() {
//     const resp = await fetch('/api/{{ name }}/trajectory');
//     const TRAJECTORY_DATA = await resp.json();
//     ... rest of existing code unchanged ...
//   })();
//   </script>
```

Also update `<title>` to `Session: {{ name }}` and add a nav bar with links to Report and SQL.

**Step 2: Commit**

```bash
git add app/templates/trajectory.html
git commit -m "feat: adapt trajectory viewer as Jinja template with API fetch"
```

---

### Task 7: Adapt report.html as Jinja template

**Files:**
- Create: `app/templates/report.html` (adapted from `data/session-report.html`)

**Step 1: Copy and adapt**

Replace hardcoded data with a fetch to `/api/{{ name }}/report`. Render KPI cards, tool usage table, token usage, and cost from the JSON response. Add nav links.

**Step 2: Commit**

```bash
git add app/templates/report.html
git commit -m "feat: adapt session report as Jinja template with API fetch"
```

---

### Task 8: SQL console page (`app/templates/sql.html`)

**Files:**
- Create: `app/templates/sql.html`

**Step 1: Write sql.html**

Minimal page: textarea for SQL input, submit button, results rendered as a table. Same dark theme. Nav links to trajectory/report. Fetches `/api/{{ name }}/query?sql=...`.

**Step 2: Commit**

```bash
git add app/templates/sql.html
git commit -m "feat: add SQL console page"
```

---

### Task 9: End-to-end test with real data

**Step 1: Start the server**

```bash
uvicorn app.main:app --port 8765 --reload
```

**Step 2: Upload a4.jsonl via browser**

Open `http://localhost:8765/`, upload `data/a4.jsonl`, verify redirect to trajectory page.

**Step 3: Verify all pages**

- `/session/{name}/trajectory` — dots render, playback works, detail panel works
- `/session/{name}/report` — KPIs display correctly
- `/session/{name}/sql` — `SELECT count(*) FROM raw` returns 3504
- `/` — session appears in list with correct links

**Step 4: Verify persistence**

Restart server, verify session still shows in list and pages work.

**Step 5: Commit any fixes**

```bash
git add -A
git commit -m "fix: end-to-end fixes from integration testing"
```

---

### Task 10: Clean up old static files

**Step 1: Remove old data pipeline artifacts**

The following are no longer needed since the server handles everything:
- `data/trajectory_data.js` (generated JS data — now served via API)

Keep `data/a4.jsonl`, `data/a4.db`, `data/session-report.html`, `data/trajectory.html` as reference/archive.

**Step 2: Create static/ placeholder**

```bash
touch static/.gitkeep
```

**Step 3: Final commit**

```bash
git add -A
git commit -m "chore: clean up old static artifacts, finalize project structure"
```

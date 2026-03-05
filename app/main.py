import json
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .parser import get_report_data, get_session_id, parse_jsonl

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
        name = get_session_id(tmp)
        parse_jsonl(name, tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return JSONResponse({"url": f"/session/{name}/trajectory"})


@app.get("/session/{name}/trajectory", response_class=HTMLResponse)
async def trajectory_page(request: Request, name: str):
    if not db.exists(name):
        return HTMLResponse("Session not found", status_code=404)
    slug = db.get_slug(name)
    return templates.TemplateResponse("trajectory.html", {"request": request, "name": name, "slug": slug})


@app.get("/session/{name}/report", response_class=HTMLResponse)
async def report_page(request: Request, name: str):
    if not db.exists(name):
        return HTMLResponse("Session not found", status_code=404)
    slug = db.get_slug(name)
    return templates.TemplateResponse("report.html", {"request": request, "name": name, "slug": slug})


@app.get("/session/{name}/sql", response_class=HTMLResponse)
async def sql_page(request: Request, name: str):
    if not db.exists(name):
        return HTMLResponse("Session not found", status_code=404)
    slug = db.get_slug(name)
    return templates.TemplateResponse("sql.html", {"request": request, "name": name, "slug": slug})


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


@app.get("/api/{name}/event/{uuid}")
async def event_data(name: str, uuid: str):
    if not db.exists(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    con = db.connect(name)
    try:
        row = con.execute(
            "SELECT message, raw FROM raw WHERE uuid = ?", [uuid]
        ).fetchone()
        if not row:
            return JSONResponse({"error": "event not found"}, status_code=404)
        message = json.loads(row[0]) if row[0] else None
        raw = json.loads(row[1]) if row[1] else None
        return {"message": message, "raw": raw}
    finally:
        con.close()


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

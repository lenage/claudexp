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


def get_slug(name: str) -> str:
    """Read display slug from meta table, falling back to name."""
    p = db_path(name)
    if not p.is_file():
        return name
    con = duckdb.connect(str(p), read_only=True)
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'slug'").fetchone()
        return row[0] if row else name
    except Exception:
        return name
    finally:
        con.close()


def list_sessions() -> list[dict]:
    """Return list of {name, slug, size_mb} for all .db files."""
    sessions = []
    for p in sorted(SESSIONS_DIR.glob("*.db")):
        name = p.stem
        sessions.append({
            "name": name,
            "slug": get_slug(name),
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

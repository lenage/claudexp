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
        # Store metadata
        slug = _get_slug(rows) or name
        con.execute("DROP TABLE IF EXISTS meta")
        con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
        con.execute("INSERT INTO meta VALUES ('slug', ?)", [slug])
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
            data = r.get("data") or {}
            node["progressType"] = data.get("type")
            node["toolName"] = data.get("toolName") or data.get("hookName")
            node["toolStatus"] = data.get("status")
            tid = r.get("toolUseID") or ""
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


def get_session_id(jsonl_path: Path) -> str:
    """Extract sessionId from JSONL for use as db name / URL path."""
    with open(jsonl_path) as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            sid = row.get("sessionId")
            if sid:
                return sid[:12]
    return jsonl_path.stem


def _get_slug(rows: list[dict]) -> str | None:
    """Extract human-readable slug from rows."""
    for r in rows:
        slug = r.get("slug")
        if slug:
            return slug
    return None


def get_report_data(name: str) -> dict:
    """Compute session report KPIs from raw table."""
    con = db.connect(name)
    try:
        total = con.execute("SELECT count(*) FROM raw").fetchone()[0]
        type_counts = dict(
            con.execute("SELECT type, count(*) FROM raw GROUP BY type ORDER BY count(*) DESC").fetchall()
        )

        ts = con.execute(
            "SELECT min(timestamp), max(timestamp) FROM raw WHERE timestamp IS NOT NULL"
        ).fetchone()
        ts_min, ts_max = ts[0], ts[1]

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

        tool_sql = """
            SELECT
                json_extract_string(raw, '$.data.toolName') as tool,
                count(*) as cnt
            FROM raw
            WHERE type = 'progress'
              AND json_extract_string(raw, '$.data.status') = 'completed'
              AND json_extract_string(raw, '$.data.toolName') IS NOT NULL
            GROUP BY tool ORDER BY cnt DESC
        """
        tools = [{"name": r[0], "count": r[1]} for r in con.execute(tool_sql).fetchall()]

        hourly_sql = """
            WITH bounds AS (
                SELECT
                    date_trunc('hour', min(timestamp::TIMESTAMP)) as ts_start,
                    date_trunc('hour', max(timestamp::TIMESTAMP)) as ts_end
                FROM raw WHERE timestamp IS NOT NULL
            ),
            hours AS (
                SELECT generate_series AS hour
                FROM bounds, generate_series(bounds.ts_start, bounds.ts_end, INTERVAL '1 hour')
            ),
            counts AS (
                SELECT date_trunc('hour', timestamp::TIMESTAMP) as hour, count(*) as cnt
                FROM raw WHERE timestamp IS NOT NULL
                GROUP BY 1
            )
            SELECT h.hour::VARCHAR, coalesce(c.cnt, 0)
            FROM hours h LEFT JOIN counts c ON h.hour = c.hour
            ORDER BY h.hour
        """
        hourly = [{"hour": r[0], "count": r[1]} for r in con.execute(hourly_sql).fetchall()]

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
            "hourly": hourly,
        }
    finally:
        con.close()

"""Run Ovenlight queries (FTS5 text + filters) and group results by session."""
from __future__ import annotations

import os
import sqlite3
from collections import Counter, OrderedDict
from datetime import datetime
from typing import Dict, List, Optional

from . import db
from .query import Query, parse_query

MAX_HITS = 4000
SNIPPETS_PER_SESSION = 3


def session_dict(r: sqlite3.Row) -> dict:
    cwd = r["cwd"] or ""
    return {
        "id": r["id"],
        "title": db.display_title(r),
        "has_title": bool(r["title"]),
        "cwd": cwd,
        "project": os.path.basename(cwd.rstrip("/")) if cwd else (r["project_key"] or ""),
        "branch": r["git_branch"] or "",
        "started": r["started"],
        "ended": r["ended"],
        "n_msgs": r["n_msgs"],
        "model": r["model"] or "",
        "tok_in": r["tok_in"],
        "tok_out": r["tok_out"],
        "size": r["size"],
        "gone": bool(r["gone"]),
        "orphan": bool(cwd) and not r["cwd_exists"],
        "tags": [t for t in (r["tags"] or "").split(",") if t],
        "starred": bool(r["starred"]),
        "path": r["path"],
    }


def run(conn: sqlite3.Connection, qstr: str, limit: int = 50, offset: int = 0, order: str = "auto") -> dict:
    q: Query = parse_query(qstr)
    where_sql, where_params = q.sql_filters()
    out: dict = {"query": qstr, "warnings": list(q.warnings), "results": [], "total": 0, "facets": {}}

    if q.fts:
        sql = (
            "SELECT d.session_id AS sid, d.kind AS kind, d.ts AS ts, "
            "snippet(docs_fts, 0, '«', '»', '…', 14) AS snip, bm25(docs_fts) AS r "
            "FROM docs_fts JOIN doc d ON d.id = docs_fts.rowid "
            "JOIN sessions s ON s.id = d.session_id "
            "WHERE docs_fts MATCH ?"
        )
        params: list = [q.fts]
        if q.scopes:
            sql += " AND d.kind IN (%s)" % ",".join("?" * len(q.scopes))
            params.extend(sorted(q.scopes))
        if where_sql:
            sql += " AND " + where_sql
            params.extend(where_params)
        sql += " ORDER BY r LIMIT %d" % MAX_HITS
        try:
            hits = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            out["warnings"].append("Invalid query for the text index: %s" % exc)
            return out

        grouped: "OrderedDict[str, dict]" = OrderedDict()
        for h in hits:
            g = grouped.setdefault(h["sid"], {"rank": h["r"], "hits": 0, "snippets": []})
            g["hits"] += 1
            if len(g["snippets"]) < SNIPPETS_PER_SESSION:
                g["snippets"].append({"kind": h["kind"], "ts": h["ts"], "snip": h["snip"]})
        sids = list(grouped.keys())
        rows = _fetch_sessions(conn, sids)
        items = []
        for sid in sids:
            if sid not in rows:
                continue
            d = session_dict(rows[sid])
            d.update(hits=grouped[sid]["hits"], snippets=grouped[sid]["snippets"], rank=grouped[sid]["rank"])
            items.append(d)
        if order == "recent":
            items.sort(key=lambda x: x["ended"] or 0, reverse=True)
        out["total"] = len(items)
        out["facets"] = facets(items)
        out["results"] = items[offset:offset + limit]
        return out

    sql = "SELECT s.* FROM sessions s"
    if where_sql:
        sql += " WHERE " + where_sql
    sql += " ORDER BY s.ended DESC"
    rows = conn.execute(sql, where_params).fetchall()
    items = [dict(session_dict(r), hits=0, snippets=[], rank=0) for r in rows]
    out["total"] = len(items)
    out["facets"] = facets(items)
    out["results"] = items[offset:offset + limit]
    return out


def _fetch_sessions(conn: sqlite3.Connection, sids: List[str]) -> Dict[str, sqlite3.Row]:
    res: Dict[str, sqlite3.Row] = {}
    for i in range(0, len(sids), 500):
        chunk = sids[i:i + 500]
        for r in conn.execute("SELECT * FROM sessions WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            res[r["id"]] = r
    return res


def facets(items: List[dict]) -> dict:
    proj = Counter()
    months = Counter()
    for it in items:
        proj[it["cwd"] or it["project"] or "(unknown)"] += 1
        if it["ended"]:
            months[datetime.fromtimestamp(it["ended"]).strftime("%Y-%m")] += 1
    return {
        "projects": [{"name": k, "n": v} for k, v in proj.most_common(15)],
        "months": [{"name": k, "n": months[k]} for k in sorted(months, reverse=True)[:12]],
    }


def summary(conn: sqlite3.Connection) -> dict:
    tot = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(tok_in),0) ti, COALESCE(SUM(tok_out),0) tout, COALESCE(SUM(size),0) sz, "
        "SUM(gone) gone, SUM(CASE WHEN cwd IS NOT NULL AND cwd_exists=0 THEN 1 ELSE 0 END) orphan, "
        "MIN(started) first, MAX(ended) last FROM sessions"
    ).fetchone()
    by_proj = conn.execute(
        "SELECT COALESCE(cwd, project_key) p, COUNT(*) n, MAX(ended) last, COALESCE(SUM(tok_in+tok_out),0) tok "
        "FROM sessions GROUP BY p ORDER BY n DESC LIMIT 30"
    ).fetchall()
    return {
        "sessions": tot["n"] or 0,
        "tokens_in": tot["ti"], "tokens_out": tot["tout"], "bytes": tot["sz"],
        "gone": tot["gone"] or 0, "orphan": tot["orphan"] or 0,
        "first": tot["first"], "last": tot["last"],
        "projects": [{"name": r["p"], "n": r["n"], "last": r["last"], "tokens": r["tok"]} for r in by_proj],
        "last_index": db.get_meta(conn, "last_index"),
    }

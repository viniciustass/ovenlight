"""Scan ~/.claude/projects, index incrementally and back up transcripts."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from . import config, db, parse


def _is_transcript(p: Path) -> bool:
    # agent-*.jsonl are subagent transcripts from older versions
    return p.suffix == ".jsonl" and not p.name.startswith("agent-")


def index_file(conn, path: Path, force: bool = False, include_results: bool = False) -> str:
    """Index one file. Returns 'indexed', 'skipped' or raises."""
    st = path.stat()
    row = conn.execute("SELECT size, mtime FROM state WHERE path=?", (str(path),)).fetchone()
    if not force and row and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1e-6:
        return "skipped"
    # The ID is the file name: on resume Claude Code creates another file, but
    # copied entries may carry the old sessionId.
    sid = path.stem
    parsed = parse.parse_file(str(path), include_results=include_results)
    db.store_session(conn, sid, path.parent.name, str(path), st, parsed)
    conn.execute(
        "INSERT INTO state(path,size,mtime,session_id) VALUES(?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, session_id=excluded.session_id",
        (str(path), st.st_size, st.st_mtime, sid),
    )
    return "indexed"


def index_all(conn, force: bool = False, log: Optional[Callable[[str], None]] = None,
              include_results: Optional[bool] = None) -> dict:
    if include_results is None:
        include_results = os.environ.get("OVENLIGHT_INDEX_RESULTS") == "1"
    t0 = time.time()
    stats = {"scanned": 0, "indexed": 0, "skipped": 0, "errors": 0, "gone": 0, "error_samples": []}
    seen = set()
    pd = config.projects_dir()
    if pd.is_dir():
        pending = 0
        for proj in sorted(pd.iterdir()):
            if not proj.is_dir():
                continue
            for f in sorted(proj.glob("*.jsonl")):
                if not _is_transcript(f):
                    continue
                seen.add(str(f))
                stats["scanned"] += 1
                try:
                    r = index_file(conn, f, force=force, include_results=include_results)
                    stats[r] += 1
                    if r == "indexed":
                        pending += 1
                        if log:
                            log("indexed: %s/%s" % (proj.name, f.name))
                except Exception as exc:  # one bad file does not break the rest
                    stats["errors"] += 1
                    if len(stats["error_samples"]) < 10:
                        stats["error_samples"].append("%s: %s" % (f, exc))
                if pending >= 25:
                    conn.commit()
                    pending = 0
    # transcripts that disappeared (retention cleanup, rm): indexed history stays
    for r in conn.execute("SELECT path, session_id FROM state").fetchall():
        if r["path"] not in seen and not os.path.exists(r["path"]):
            conn.execute("UPDATE sessions SET gone=1 WHERE id=?", (r["session_id"],))
            stats["gone"] += 1
    stats["seconds"] = round(time.time() - t0, 2)
    db.set_meta(conn, "last_index", str(time.time()))
    db.set_meta(conn, "last_index_stats", json.dumps(stats))
    conn.commit()
    return stats


def backup_all(log: Optional[Callable[[str], None]] = None) -> dict:
    """Copy new/changed transcripts to ~/.ovenlight/backup/projects (never deletes)."""
    res = {"copied": 0, "unchanged": 0, "errors": 0}
    pd = config.projects_dir()
    if not pd.is_dir():
        return res
    for proj in pd.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            if not _is_transcript(f):
                continue
            dst = config.backup_dir() / proj.name / f.name
            try:
                st = f.stat()
                if dst.exists() and dst.stat().st_size == st.st_size and dst.stat().st_mtime >= st.st_mtime:
                    res["unchanged"] += 1
                    continue
                config.secure_mkdir(dst.parent)
                shutil.copy2(f, dst)
                config.secure_file(dst)
                res["copied"] += 1
                if log:
                    log("backup: %s" % dst)
            except OSError:
                res["errors"] += 1
    return res


def restore(conn, sid_prefix: str) -> str:
    """Restore a removed transcript from the Ovenlight backup."""
    row = db.find_session(conn, sid_prefix)
    src = config.backup_dir() / (row["project_key"] or "") / (row["id"] + ".jsonl")
    if not src.exists():
        raise FileNotFoundError("No backup of this transcript at %s" % src)
    dst = Path(row["path"])
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    index_file(conn, dst, force=True)
    conn.commit()
    return str(dst)

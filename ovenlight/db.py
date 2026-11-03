"""Local database (SQLite + FTS5). Transcripts remain the source of truth."""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,
  tool TEXT NOT NULL DEFAULT 'claude',
  project_key TEXT,
  cwd TEXT,
  git_branch TEXT,
  title TEXT,
  first_prompt TEXT,
  started REAL,
  ended REAL,
  n_msgs INTEGER DEFAULT 0,
  n_prompts INTEGER DEFAULT 0,
  model TEXT,
  tok_in INTEGER DEFAULT 0,
  tok_out INTEGER DEFAULT 0,
  tok_cache INTEGER DEFAULT 0,
  size INTEGER DEFAULT 0,
  path TEXT,
  mtime REAL,
  cwd_exists INTEGER DEFAULT 1,
  gone INTEGER DEFAULT 0,
  version TEXT,
  tags TEXT DEFAULT '',
  starred INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_ended ON sessions(ended);
CREATE INDEX IF NOT EXISTS sessions_cwd ON sessions(cwd);

CREATE TABLE IF NOT EXISTS files(session_id TEXT, path TEXT);
CREATE INDEX IF NOT EXISTS files_sid ON files(session_id);
CREATE INDEX IF NOT EXISTS files_path ON files(path);

CREATE TABLE IF NOT EXISTS tools(session_id TEXT, name TEXT, n INTEGER);
CREATE INDEX IF NOT EXISTS tools_sid ON tools(session_id);

CREATE TABLE IF NOT EXISTS doc(
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  kind TEXT,
  ts REAL,
  text TEXT
);
CREATE INDEX IF NOT EXISTS doc_sid ON doc(session_id);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
  text, content='doc', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS doc_ai AFTER INSERT ON doc BEGIN
  INSERT INTO docs_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS doc_ad AFTER DELETE ON doc BEGIN
  INSERT INTO docs_fts(docs_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS state(path TEXT PRIMARY KEY, size INTEGER, mtime REAL, session_id TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def has_fts5() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.close()
        return True
    except sqlite3.Error:
        return False


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or config.db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    if path is None or str(path) != ":memory:":
        base = str(path or config.db_path())
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(base + suffix):
                config.secure_file(base + suffix)
    return conn


def set_meta(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def get_meta(conn: sqlite3.Connection, k: str, default: Optional[str] = None) -> Optional[str]:
    r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def store_session(conn: sqlite3.Connection, sid: str, project_key: str, path: str, st: os.stat_result, parsed: dict) -> None:
    """Insert/update a session. User tags and stars are preserved."""
    m = parsed["meta"]
    cwd = m["cwd"]
    cwd_exists = 1 if (cwd and os.path.isdir(cwd)) else 0
    conn.execute("DELETE FROM doc WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM files WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM tools WHERE session_id=?", (sid,))
    conn.execute(
        """INSERT INTO sessions(id,tool,project_key,cwd,git_branch,title,first_prompt,started,ended,
              n_msgs,n_prompts,model,tok_in,tok_out,tok_cache,size,path,mtime,cwd_exists,gone,version)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
           ON CONFLICT(id) DO UPDATE SET
              project_key=excluded.project_key, cwd=excluded.cwd, git_branch=excluded.git_branch,
              title=excluded.title, first_prompt=excluded.first_prompt, started=excluded.started,
              ended=excluded.ended, n_msgs=excluded.n_msgs, n_prompts=excluded.n_prompts,
              model=excluded.model, tok_in=excluded.tok_in, tok_out=excluded.tok_out,
              tok_cache=excluded.tok_cache, size=excluded.size, path=excluded.path,
              mtime=excluded.mtime, cwd_exists=excluded.cwd_exists, gone=0, version=excluded.version""",
        (
            sid, "claude", project_key, cwd, m["branch"],
            m["custom_title"] or m["title"], m["first_prompt"],
            m["started"] or st.st_mtime, m["ended"] or st.st_mtime,
            parsed["n_msgs"], parsed["n_prompts"], m["model"],
            parsed["tok_in"], parsed["tok_out"], parsed["tok_cache"],
            st.st_size, path, st.st_mtime, cwd_exists, m["version"],
        ),
    )
    conn.executemany(
        "INSERT INTO doc(session_id,kind,ts,text) VALUES(?,?,?,?)",
        [(sid, k, ts, text) for (k, ts, text) in parsed["docs"]],
    )
    conn.executemany("INSERT INTO files(session_id,path) VALUES(?,?)", [(sid, p) for p in sorted(parsed["files"])])
    conn.executemany("INSERT INTO tools(session_id,name,n) VALUES(?,?,?)", [(sid, k, v) for k, v in parsed["tools"].items()])


def display_title(row) -> str:
    t = row["title"] or row["first_prompt"] or "(untitled)"
    return " ".join(str(t).split())


def find_session(conn: sqlite3.Connection, prefix: str):
    """Resolve a full ID or a unique prefix."""
    rows = conn.execute("SELECT * FROM sessions WHERE id LIKE ? ORDER BY ended DESC LIMIT 6", (prefix + "%",)).fetchall()
    if not rows:
        raise LookupError("No session with ID %r. Run `ovenlight index` if it is recent." % prefix)
    exact = [r for r in rows if r["id"] == prefix]
    if exact:
        return exact[0]
    if len(rows) > 1:
        raise LookupError("Prefix %r is ambiguous (%d sessions). Use more characters." % (prefix, len(rows)))
    return rows[0]


def now() -> float:
    return time.time()

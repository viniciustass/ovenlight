"""Ovenlight advanced search syntax.

Example:
    webhook stripe project:myapp branch:feat/* after:7d in:bash -docker "error 500"

Free text becomes an FTS5 query; `key:value` becomes an SQL filter.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import List, Optional, Set, Tuple

KEYS = {
    "project": "project", "p": "project",
    "branch": "branch", "b": "branch",
    "after": "after", "since": "after",
    "before": "before", "until": "before",
    "model": "model",
    "file": "file",
    "in": "in",
    "used": "used", "tool": "used",
    "source": "source",
    "tag": "tag",
    "msgs": "msgs", "tokens": "tokens", "size": "size",
    "orphan": "orphan", "deleted": "deleted", "starred": "starred",
}

SCOPES = {
    "prompt": "prompt", "prompts": "prompt",
    "reply": "reply", "replies": "reply",
    "bash": "bash", "command": "bash", "commands": "bash",
    "error": "error", "errors": "error",
    "file": "file", "files": "file",
    "tool": "tool", "tools": "tool",
    "output": "output", "outputs": "output",
}

TOKEN_RE = re.compile(r'(-?)(?:([^\s:"]+):)?(?:"([^"]*)"|(\S+))')
YES = {"yes", "y", "true", "1"}


class Query:
    def __init__(self) -> None:
        self.fts: Optional[str] = None
        self.scopes: Set[str] = set()
        self.where: List[Tuple[str, list]] = []
        self.warnings: List[str] = []
        self.text_terms: List[str] = []

    def sql_filters(self) -> Tuple[str, list]:
        parts, params = [], []
        for sql, p in self.where:
            parts.append("(" + sql + ")")
            params.extend(p)
        return (" AND ".join(parts), params)


def parse_when(s: str) -> Optional[float]:
    s = s.strip().lower()
    now = time.time()
    if s == "today":
        d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()
    if s == "yesterday":
        d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp() - 86400
    m = re.fullmatch(r"(\d+)\s*([hdwm])", s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        secs = {"h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400}[u]
        return now - n * secs
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def _like(v: str, wrap: bool) -> str:
    v = v.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("*", "%")
    if wrap and "%" not in v:
        v = "%" + v + "%"
    return v


def _num(v: str, units: bool = False) -> Optional[Tuple[str, float]]:
    m = re.fullmatch(r"(>=|<=|>|<|=)?\s*([\d.]+)\s*([kmg]b?)?", v.strip().lower())
    if not m:
        return None
    op = m.group(1) or "="
    n = float(m.group(2))
    if units and m.group(3):
        n *= {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(3)[0]]
    return op, n


def _fts_term(t: str) -> Optional[str]:
    prefix = t.endswith("*")
    core = t.rstrip("*")
    if not re.search(r"\w", core):
        return None
    core = core.replace('"', '""')
    return '"%s"%s' % (core, "*" if prefix else "")


def parse_query(text: str) -> Query:
    q = Query()
    pos: List[str] = []   # positive terms and OR operators
    neg: List[str] = []

    for m in TOKEN_RE.finditer(text or ""):
        neg_flag, key, quoted, word = m.group(1), m.group(2), m.group(3), m.group(4)
        val = quoted if quoted is not None else (word or "")
        canon = KEYS.get(key.lower()) if key else None

        if key and canon is None:
            # "http://x" or "a:b" is not a filter, it's text
            val = key + ":" + val
            key = None

        if canon:
            _add_filter(q, canon, val)
            continue

        if not neg_flag and quoted is None and val == "OR":
            if pos and pos[-1] != "OR":
                pos.append("OR")
            continue

        if quoted is not None:
            # exact phrase: quoted as a whole, no prefix
            term = '"%s"' % val.replace('"', '""') if re.search(r"\w", val) else None
        else:
            term = _fts_term(val)
        if term is None:
            continue
        q.text_terms.append(val)
        (neg if neg_flag else pos).append(term)

    while pos and pos[-1] == "OR":
        pos.pop()
    if pos:
        expr = "(" + " ".join(pos) + ")"
        if neg:
            expr += " NOT (" + " OR ".join(neg) + ")"
        q.fts = expr
    elif neg:
        q.warnings.append("Only exclusion terms (-word): include at least one positive term.")
    if q.scopes and not q.fts:
        q.warnings.append("`in:` only works together with search text; it was ignored.")
    return q


def _add_filter(q: Query, key: str, val: str) -> None:
    val = val.strip()
    if not val:
        return
    esc = " ESCAPE '\\'"
    if key == "project":
        like = _like(val, True)
        q.where.append(("lower(s.cwd) LIKE ?%s OR lower(s.project_key) LIKE ?%s" % (esc, esc), [like, like]))
    elif key == "branch":
        q.where.append(("lower(s.git_branch) LIKE ?" + esc, [_like(val, False)]))
    elif key in ("after", "before"):
        t = parse_when(val)
        if t is None:
            q.warnings.append("Unrecognized date in %s:%s (use 2026-09-01, 7d, 2w, yesterday)." % (key, val))
            return
        if key == "after":
            q.where.append(("s.ended >= ?", [t]))
        else:
            q.where.append(("s.started <= ?", [t]))
    elif key == "model":
        q.where.append(("lower(s.model) LIKE ?" + esc, [_like(val, True)]))
    elif key == "file":
        q.where.append(("EXISTS (SELECT 1 FROM files f WHERE f.session_id=s.id AND lower(f.path) LIKE ?%s)" % esc, [_like(val, True)]))
    elif key == "used":
        q.where.append(("EXISTS (SELECT 1 FROM tools t WHERE t.session_id=s.id AND lower(t.name) = ?)", [val.lower()]))
    elif key == "source":
        q.where.append(("s.tool = ?", [val.lower()]))
    elif key == "tag":
        q.where.append(("(',' || lower(s.tags) || ',') LIKE ?" + esc, ["%," + _like(val, False).strip("%") + ",%"]))
    elif key in ("msgs", "tokens", "size"):
        n = _num(val, units=(key == "size"))
        if not n:
            q.warnings.append("Unrecognized number in %s:%s (e.g. >20, <=5, >1mb)." % (key, val))
            return
        col = {"msgs": "s.n_msgs", "tokens": "(s.tok_in + s.tok_out)", "size": "s.size"}[key]
        q.where.append(("%s %s ?" % (col, n[0]), [n[1]]))
    elif key == "in":
        sc = SCOPES.get(val.lower())
        if sc:
            q.scopes.add(sc)
        else:
            q.warnings.append("Unknown scope in:%s (use prompt, reply, bash, error, file, tool, output)." % val)
    elif key in ("orphan", "deleted", "starred"):
        want = val.lower() in YES
        col = {"orphan": "(s.cwd IS NOT NULL AND s.cwd_exists = 0)", "deleted": "s.gone = 1", "starred": "s.starred = 1"}[key]
        q.where.append((col if want else "NOT " + col, []))


HELP = """Search syntax

  free text          words (implicit AND); "exact phrase"; prefix deplo*; -exclude; OR
  project:x          folder/project contains x            (alias: p:)
  branch:feat/*      git branch (* is a wildcard)          (alias: b:)
  after:7d           activity after (2026-09-01, 12h, 7d, 2w, 3m, yesterday, today)
  before:2026-09-01  session started before
  model:opus         model contains
  file:auth.py       sessions that read/edited the file
  used:Bash          sessions where the tool was used (Bash, Edit, Task, mcp__...)
  in:bash            where to search: prompt, reply, bash, error, file, tool, output (repeatable)
  msgs:>20           message count   tokens:>100000   size:>1mb
  tag:x              Ovenlight tag   starred:yes   orphan:yes (source folder no longer exists)
  deleted:yes        transcript removed by Claude Code cleanup (history kept by Ovenlight)

Example:  webhook stripe project:shop after:2w in:bash -docker
"""

"""Ovenlight paths and constants."""
from __future__ import annotations

import os
import re
from pathlib import Path

VERSION = "0.1.0"


def home() -> Path:
    return Path(os.path.expanduser("~"))


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (home() / ".claude"))


def projects_dir() -> Path:
    return claude_dir() / "projects"


def secure_file(p) -> None:
    """Make the file owner-only (0600). Ovenlight data contains conversation text."""
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def secure_mkdir(p: Path) -> Path:
    """Create the folder (and intermediate ones inside ~/.ovenlight) with mode 0700."""
    p = Path(p)
    root = Path(os.environ.get("OVENLIGHT_HOME") or (home() / ".ovenlight"))
    p.mkdir(parents=True, exist_ok=True)
    cur = p
    while True:
        try:
            os.chmod(cur, 0o700)
        except OSError:
            pass
        if cur == root or root not in cur.parents:
            break
        cur = cur.parent
    return p


def ovenlight_dir() -> Path:
    p = Path(os.environ.get("OVENLIGHT_HOME") or (home() / ".ovenlight"))
    if not p.is_dir():
        secure_mkdir(p)
    elif (p.stat().st_mode & 0o077) != 0:
        secure_mkdir(p)  # fix older installs created with 0755
    return p


def db_path() -> Path:
    return ovenlight_dir() / "ovenlight.db"


def backup_dir() -> Path:
    return ovenlight_dir() / "backup" / "projects"


def encode_cwd(cwd: str) -> str:
    """Claude Code project folder name: path with non-alphanumerics replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


# Markers that tie a process to the Claude Code session that launched Ovenlight. Passing them on would make a
# claude started by Ovenlight think it is a child session (it then stops saving transcripts) and would leak the
# parent's messaging token. User configuration (ANTHROPIC_*, CLAUDE_CODE_USE_BEDROCK, ...) is kept.
SESSION_MARKERS = (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SSE_PORT", "CLAUDE_PID",
)


def clean_env(extra=None) -> dict:
    """os.environ without the parent Claude Code session markers."""
    env = {k: v for k, v in os.environ.items() if k not in SESSION_MARKERS}
    env.update(extra or {})
    return env


def audit(event: str, **fields) -> None:
    """Append one JSON line to ~/.ovenlight/audit.log (0600): every command Ovenlight runs or sends."""
    import json
    import time
    path = ovenlight_dir() / "audit.log"
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
    rec.update(fields)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        secure_file(path)
    except OSError:
        pass

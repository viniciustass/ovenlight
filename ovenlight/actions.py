"""Reopen sessions and open terminal windows on macOS and Linux."""
from __future__ import annotations

import os
import random
import shlex
import shutil
import string
import subprocess
import sys
from typing import List, Optional, Tuple

from . import config, db, live


class ActionError(Exception):
    pass


def plan_reopen(row, bg: bool = False, fork: bool = False, here: bool = False) -> dict:
    """Build the reopen command. Does not run anything."""
    if row["gone"]:
        raise ActionError(
            "This session's transcript was deleted by Claude Code cleanup. "
            "If Ovenlight has a backup: `ovenlight restore %s`." % row["id"][:8]
        )
    cwd, warnings = session_cwd(row, here)
    argv = ["claude", "--resume", row["id"]]
    if fork:
        argv.append("--fork-session")
    if bg:
        argv.append("--bg")
    return {"cwd": cwd, "argv": argv, "shell": "cd %s && %s" % (shlex.quote(cwd), " ".join(shlex.quote(a) for a in argv)),
            "warnings": warnings}


def session_cwd(row, here: bool = False) -> Tuple[str, List[str]]:
    """Folder to work in for a session: its original cwd, or home (or the current folder) if it is gone."""
    warnings: List[str] = []
    cwd = row["cwd"]
    if here or not cwd or not os.path.isdir(cwd):
        if cwd and not os.path.isdir(cwd):
            warnings.append("The original folder (%s) no longer exists; using %s instead." % (cwd, "the current folder" if here else "your home folder"))
        cwd = os.getcwd() if here else str(config.home())
    return cwd, warnings


def plan_shell(row, here: bool = False, claude: bool = True) -> dict:
    """A shell in the session folder that starts a new `claude` (or a plain shell with claude=False).
    Works even if the transcript was deleted."""
    cwd, warnings = session_cwd(row, here)
    cmd = "cd %s" % shlex.quote(cwd) + (" && claude" if claude else "")
    return {"cwd": cwd, "argv": ["claude"] if claude else [], "shell": cmd, "warnings": warnings}


def conflicts(sid: str, conn=None) -> List[dict]:
    """Live processes that seem to be using this session right now."""
    try:
        return [i for i in live.live_sessions(conn)["items"] if i.get("session_id") == sid]
    except Exception:
        return []


# ---------------------------------------------------------------- terminals

def _as_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def open_terminal(shell_cmd: str) -> Tuple[bool, str]:
    """Open a terminal window running shell_cmd. Returns (ok, message)."""
    custom = os.environ.get("OVENLIGHT_TERMINAL_CMD")
    if custom:  # e.g. OVENLIGHT_TERMINAL_CMD='wezterm start -- bash -lc {cmd}'
        full = custom.replace("{cmd}", shlex.quote(shell_cmd + "; exec ${SHELL:-bash}"))
        subprocess.Popen(full, shell=True, start_new_session=True)
        return True, "Opened with OVENLIGHT_TERMINAL_CMD."

    if sys.platform == "darwin":
        if os.environ.get("OVENLIGHT_TERMINAL", "").lower() == "iterm":
            script = ('tell application "iTerm"\n activate\n set w to (create window with default profile)\n'
                      ' tell current session of w to write text "%s"\nend tell' % _as_escape(shell_cmd))
        else:
            script = 'tell application "Terminal"\n activate\n do script "%s"\nend tell' % _as_escape(shell_cmd)
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if r.returncode != 0:
            return False, "osascript failed: %s" % r.stderr.strip()
        return True, "Opened in %s." % ("iTerm" if "iTerm" in script else "Terminal")

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False, "No graphical session (DISPLAY/WAYLAND_DISPLAY). Run the command manually."
    inner = shell_cmd + "; exec ${SHELL:-bash}"
    candidates = [
        ("gnome-terminal", ["gnome-terminal", "--", "bash", "-lc", inner]),
        ("kgx", ["kgx", "-e", "bash", "-lc", inner]),
        ("konsole", ["konsole", "-e", "bash", "-lc", inner]),
        ("xfce4-terminal", ["xfce4-terminal", "-e", "bash -lc %s" % shlex.quote(inner)]),
        ("kitty", ["kitty", "bash", "-lc", inner]),
        ("alacritty", ["alacritty", "-e", "bash", "-lc", inner]),
        ("wezterm", ["wezterm", "start", "--", "bash", "-lc", inner]),
        ("foot", ["foot", "bash", "-lc", inner]),
        ("x-terminal-emulator", ["x-terminal-emulator", "-e", "bash", "-lc", inner]),
        ("xterm", ["xterm", "-e", "bash", "-lc", inner]),
    ]
    for exe, argv in candidates:
        if shutil.which(exe):
            subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "Opened in %s." % exe
    return False, "No known terminal emulator found. Set OVENLIGHT_TERMINAL_CMD."


# ---------------------------------------------------------------- managed tmux

def _new_name() -> str:
    return "ovenlight-" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(5))


def run_managed(cwd: str, claude_args: Optional[List[str]] = None) -> str:
    """Start `claude` inside a ovenlight-* tmux session (level B: prompts can be sent later)."""
    if not live.tmux_ok():
        raise ActionError("tmux is not installed. macOS: `brew install tmux`; Debian/Ubuntu: `sudo apt install tmux`.")
    if not os.path.isdir(cwd):
        raise ActionError("Folder %s does not exist." % cwd)
    name = _new_name()
    cmd = " ".join(shlex.quote(a) for a in ["claude"] + list(claude_args or []))
    r = subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", cwd, cmd], capture_output=True, text=True,
                       env=config.clean_env(), stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise ActionError("tmux failed to start: %s" % r.stderr.strip())
    return name


def reopen(row, mode: str = "terminal", bg: bool = False, fork: bool = False, here: bool = False,
           claude: bool = True) -> dict:
    """mode: terminal (new window) | managed (tmux + new window) | shell (new window, no claude) | print."""
    if mode == "shell":
        plan = plan_shell(row, here=here, claude=claude)
        ok, msg = open_terminal(plan["shell"])
        return {"command": plan["shell"], "warnings": plan["warnings"], "opened": ok, "message": msg}
    plan = plan_reopen(row, bg=bg, fork=fork, here=here)
    res = {"command": plan["shell"], "warnings": plan["warnings"], "opened": False, "message": ""}
    if mode == "print" or bg:
        if bg:
            r = subprocess.run(plan["argv"], cwd=plan["cwd"], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL, env=config.clean_env())
            res["opened"] = r.returncode == 0
            res["message"] = (r.stdout or r.stderr).strip()[:300] or "Sent to the background."
        return res
    if mode == "managed":
        name = run_managed(plan["cwd"], plan["argv"][1:])
        res["tmux"] = name
        ok, msg = open_terminal("tmux attach -t %s" % name)
        res["opened"], res["message"] = ok, ("Managed session %s. " % name) + msg
        return res
    ok, msg = open_terminal(plan["shell"])
    res["opened"], res["message"] = ok, msg
    return res

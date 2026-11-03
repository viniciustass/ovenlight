"""Live sessions.

Three control levels (see README):
  A. background     -> `claude agents --json` (Claude Code agent view)
  B. managed        -> started by `ovenlight run` inside a `ovenlight-*` tmux session (full control)
  C. plain terminal -> detected by process; read-only (Claude Code has no API to inject text)
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config, parse, term

WORKING_SECONDS = 15


class LiveError(Exception):
    pass


def _run(cmd: List[str], timeout: float = 8.0, input: Optional[str] = None, env: Optional[dict] = None,
         cwd: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        if env is not None:
            kw["env"] = env
        if cwd and os.path.isdir(cwd):
            kw["cwd"] = cwd
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "%s not found on PATH" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


# ---------------------------------------------------------------- processes

def _is_claude(argv: List[str]) -> bool:
    if not argv:
        return False
    exe = os.path.basename(argv[0])
    if exe == "claude" or "/claude/versions/" in argv[0]:
        return True
    if exe in ("node", "bun", "deno") and len(argv) > 1:
        a = argv[1]
        return os.path.basename(a) == "claude" or "claude-code" in a
    return False


# Claude Code's own background machinery (observed in 2.1.317): the agent-view daemon, pty hosts and
# pre-warmed "spare" processes. They are not sessions; a spare that gets claimed shows up in
# `claude agents --json` with its pid, so it is still listed (as background) from there.
INTERNAL_SUBCOMMANDS = {"daemon", "bg-pty-host", "bg-spare"}
# Short-lived utility invocations (e.g. the `claude agents --json` Ovenlight itself runs every few seconds).
# `attach` is a viewer of a background session, which is already listed from the agent view.
UTILITY_SUBCOMMANDS = {"agents", "logs", "attach", "stop", "kill", "rm", "respawn", "mcp", "config", "doctor",
                       "update", "install", "setup-token", "plugin", "migrate-installer", "--version", "-v",
                       "--help", "-h"}


def _is_internal(argv: List[str]) -> bool:
    args = argv[1:]
    if args and (args[0] in INTERNAL_SUBCOMMANDS or args[0] in UTILITY_SUBCOMMANDS):
        return True
    return any(a in ("--bg-spare", "--bg-pty-host") for a in args)


def skips_permissions(argv: List[str]) -> bool:
    """True when claude was started in bypass mode. Claude Code holds messages from sessions in a different
    permission class (e.g. Ovenlight's courier) until the user approves them in that session's terminal."""
    args = argv[1:]
    if "--dangerously-skip-permissions" in args or "--permission-mode=bypassPermissions" in args:
        return True
    return any(a == "--permission-mode" and i + 1 < len(args) and args[i + 1] == "bypassPermissions"
               for i, a in enumerate(args))


def _ps_table() -> List[dict]:
    rc, out, _ = _run(["ps", "-axo", "pid=,ppid=,etime=,stat=,tty=,command="])
    rows = []
    if rc != 0:
        return rows
    for line in out.splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) < 6:
            continue
        try:
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "etime": parts[2],
                         "stat": parts[3], "tty": parts[4], "cmd": parts[5]})
        except ValueError:
            continue
    return rows


def _cwds(pids: List[int]) -> Dict[int, str]:
    res: Dict[int, str] = {}
    if not pids:
        return res
    if sys.platform.startswith("linux"):
        for p in pids:
            try:
                res[p] = os.readlink("/proc/%d/cwd" % p)
            except OSError:
                pass
    else:  # macOS and other BSDs
        rc, out, _ = _run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(str(p) for p in pids)])
        cur = None
        for l in out.splitlines():
            if l.startswith("p"):
                try:
                    cur = int(l[1:])
                except ValueError:
                    cur = None
            elif l.startswith("n") and cur is not None:
                res[cur] = l[1:]
    return res


def _transcripts_for_cwd(cwd: str) -> List[Tuple[str, float, int]]:
    cands = {cwd, os.path.realpath(cwd)}
    best: List[Tuple[str, float, int]] = []
    for c in cands:
        d = config.projects_dir() / config.encode_cwd(c)
        if d.is_dir():
            for f in d.glob("*.jsonl"):
                if f.name.startswith("agent-"):
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                best.append((str(f), st.st_mtime, st.st_size))
    best.sort(key=lambda x: x[1], reverse=True)
    return best


# ---------------------------------------------------------------- tmux

def tmux_ok() -> bool:
    return shutil.which("tmux") is not None


def managed_panes() -> Dict[str, dict]:
    """tmux sessions created by Ovenlight (ovenlight- prefix)."""
    if not tmux_ok():
        return {}
    rc, out, _ = _run(["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}\t#{pane_current_path}"])
    res: Dict[str, dict] = {}
    if rc != 0:
        return res
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) == 3 and p[0].startswith("ovenlight-"):
            try:
                res[p[0]] = {"name": p[0], "pane_pid": int(p[1]), "cwd": p[2]}
            except ValueError:
                pass
    return res


def _has_ancestor(pid: int, target: int, ppid: Dict[int, int]) -> bool:
    seen = 0
    while pid and seen < 40:
        if pid == target:
            return True
        pid = ppid.get(pid, 0)
        seen += 1
    return False


# ---------------------------------------------------------------- listing

# Real schema observed in Claude Code 2.1.317 (`claude agents --json`): list of objects
#   {"pid": int, "cwd": str, "kind": "interactive", "startedAt": ms, "sessionId": uuid,
#    "name": str, "status": "busy" | "idle"}
# The list includes INTERACTIVE sessions (plain terminal, level C), not only background ones.
# Sessions whose kind is in NON_BG_KINDS cannot receive prompts; any other kind (or none)
# is treated as background.
NON_BG_KINDS = {"interactive", "foreground", "headless", "print", "sdk"}
STATUS_MAP = {"busy": "working", "working": "working", "running": "working",
              "idle": "idle", "needs_input": "needs input", "needs-input": "needs input",
              "waiting": "needs input", "completed": "completed", "done": "completed",
              "failed": "failed", "error": "failed", "stopped": "stopped",
              "permission": "needs input", "awaiting_input": "needs input"}


def background_sessions() -> Tuple[List[dict], Optional[str]]:
    """Sessions listed by `claude agents --json` (interactive and background). Tolerant to schema changes."""
    if not shutil.which("claude"):
        return [], "`claude` not found on PATH."
    rc, out, err = _run(["claude", "agents", "--json"], timeout=12)
    if rc != 0:
        msg = (err or out).strip().splitlines()
        return [], "agent view unavailable: %s" % (msg[0][:200] if msg else "exit code %d" % rc)
    try:
        data = json.loads(out)
    except ValueError:
        return [], "`claude agents --json` did not return JSON."
    if isinstance(data, dict):
        data = data.get("sessions") or data.get("agents") or data.get("items") or []
    items = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        # Two ids (2.1.317): "id" is the background *job* id that `claude logs/stop/attach` take,
        # "sessionId" is the conversation (transcript, --resume). Interactive rows may have only sessionId.
        sid = next((str(d[k]) for k in ("sessionId", "session_id") if d.get(k)), None)
        job = next((str(d[k]) for k in ("id", "short_id", "shortId", "jobId", "job_id") if d.get(k)), None)
        sid = sid or job
        job = job or sid
        if not sid:
            continue
        agent_kind = str(d.get("kind") or d.get("type") or "").lower()
        is_bg = agent_kind not in NON_BG_KINDS
        # "status" is the run state (busy/idle...); "state" is extra detail (e.g. "blocked") shown alongside
        raw_status = str(d.get("status") or "")
        raw_state = str(d.get("state") or "")
        main = raw_status or raw_state or "unknown"
        label = STATUS_MAP.get(main.lower(), main)
        if raw_status and raw_state and raw_state.lower() != raw_status.lower():
            label += " (%s)" % raw_state
        try:
            pid = int(d["pid"]) if d.get("pid") is not None else None
        except (TypeError, ValueError):
            pid = None
        started = d.get("startedAt") or d.get("started_at")
        if isinstance(started, (int, float)) and started > 1e11:  # milliseconds
            started = started / 1000.0
        items.append({
            "key": ("bg:" + job) if is_bg else ("sid:" + sid),
            "kind": "bg" if is_bg else "proc",
            "agent_kind": agent_kind or None,
            "id": job,
            "session_id": sid,
            "pid": pid,
            "title": str(next((d[k] for k in ("name", "title", "display_name", "displayName") if d.get(k)), sid)),
            "cwd": str(next((d[k] for k in ("cwd", "directory", "dir", "project", "path") if d.get(k)), "")),
            "state": label,
            "state_estimated": False,
            "started": started if isinstance(started, (int, float)) else None,
            "last_activity": None,
            "confidence": "high",
            "summary": str(next((d[k] for k in ("summary", "last_output", "lastOutput", "result", "prompt") if d.get(k)), ""))[:300],
            "can_send": is_bg,
            "managed": None,
            "raw": d,
        })
    return items, None


def _transcript_path(sid: str, cwd: str) -> Optional[Tuple[str, float]]:
    for c in {cwd, os.path.realpath(cwd)} if cwd else ():
        f = config.projects_dir() / config.encode_cwd(c) / (sid + ".jsonl")
        try:
            return str(f), f.stat().st_mtime
        except OSError:
            continue
    return None


def _merge_agents(items: List[dict], agents: List[dict], conn=None) -> List[dict]:
    """Merge the `claude agents --json` list with process detection, without duplicates.

    - Interactive entry with a known pid: fixes the process item (exact session, real state)
      and is dropped; still no sending (level C) unless Ovenlight manages it.
    - Background entry: stays as `bg:` (sending allowed) and hides the matching process line.
    """
    by_pid = {i["pid"]: i for i in items if i.get("pid") and i.get("kind") in ("proc", "managed", "headless", "embedded")}
    by_sid: Dict[str, dict] = {}
    for i in items:
        if i.get("session_id") and i.get("kind") in ("proc", "headless", "embedded"):
            by_sid.setdefault(i["session_id"], i)
    hidden = set()
    out_agents = []
    for a in agents:
        proc = by_pid.get(a["pid"]) if a.get("pid") else None
        if proc is None and a["kind"] == "bg":
            proc = by_sid.get(a["session_id"])
        if a["kind"] != "bg" and proc is not None:
            # interactive: the process is already listed; use the exact Claude Code data
            sid = a["session_id"]
            proc["session_id"] = sid
            proc["confidence"] = "high"
            proc["state"] = a["state"]
            proc["state_estimated"] = False
            tp = _transcript_path(sid, proc.get("cwd") or a["cwd"])
            if tp:
                proc["transcript"], proc["last_activity"] = tp
            title = ""
            if conn is not None:
                r = conn.execute("SELECT title, first_prompt FROM sessions WHERE id=?", (sid,)).fetchone()
                if r:
                    title = r["title"] or r["first_prompt"] or ""
            proc["title"] = title or a["title"]
            proc["agent_name"] = a["title"]
            proc["can_message"] = bool(a["title"]) and not proc.get("can_send")
            proc["raw"] = a["raw"]
            continue
        if proc is not None:
            hidden.add(id(proc))
            a["last_activity"] = proc.get("last_activity")
            a["transcript"] = proc.get("transcript")
        else:
            tp = _transcript_path(a["session_id"], a["cwd"])
            if tp:
                a["transcript"], a["last_activity"] = tp
        # prefer the conversation title; the agent-view name is often just the short id
        a["agent_name"] = a["title"]
        title = ""
        if conn is not None:
            r = conn.execute("SELECT title, first_prompt FROM sessions WHERE id=?", (a["session_id"],)).fetchone()
            if r:
                title = r["title"] or r["first_prompt"] or ""
        if not title and a.get("transcript"):  # not indexed yet
            qi = parse.quick_info(a["transcript"], max_lines=2000)
            title = qi["title"] or qi["first_prompt"] or ""
        a["title"] = title or a["title"]
        if a["kind"] != "bg":
            # interactive with no matching process (e.g. ps/lsof failed): show read-only
            a["can_send"] = False
        out_agents.append(a)
    return out_agents + [i for i in items if id(i) not in hidden]


def _state_from_age(age: float) -> str:
    return "working" if age < WORKING_SECONDS else "idle"


def live_sessions(conn=None) -> dict:
    """All live sessions, from every level."""
    now = time.time()
    table = _ps_table()
    ppid = {r["pid"]: r["ppid"] for r in table}
    me = os.getpid()
    procs = []
    for r in table:
        if r["pid"] == me:
            continue
        try:
            argv = shlex.split(r["cmd"])
        except ValueError:
            argv = r["cmd"].split()
        if _is_claude(argv) and not _is_internal(argv):
            r["argv"] = argv
            procs.append(r)
    cwds = _cwds([p["pid"] for p in procs])
    panes = managed_panes()

    by_cwd: Dict[str, List[dict]] = {}
    for p in procs:
        p["cwd"] = cwds.get(p["pid"], "")
        by_cwd.setdefault(p["cwd"], []).append(p)

    embedded = term.embedded_pids()
    items: List[dict] = []
    for cwd, plist in by_cwd.items():
        cands = _transcripts_for_cwd(cwd) if cwd else []
        for idx, p in enumerate(plist):
            headless = any(a in ("-p", "--print") for a in p["argv"][1:])
            mode = "headless" if headless else "interactive"
            managed = next((n for n, pane in panes.items() if _has_ancestor(p["pid"], pane["pane_pid"], ppid)), None)
            sess = cands[idx] if idx < len(cands) else None
            confidence = "none" if not sess else ("high" if len(plist) == 1 else "low")
            sid = Path(sess[0]).stem if sess else None
            title = ""
            if sess:
                info = parse.quick_info(sess[0])
                title = info["first_prompt"] or ""
                age = now - sess[1]
            else:
                age = 1e9
            if conn is not None and sid:
                r = conn.execute("SELECT title, first_prompt FROM sessions WHERE id=?", (sid,)).fetchone()
                if r:
                    title = r["title"] or r["first_prompt"] or title
            host = next((ep for ep in embedded if _has_ancestor(p["pid"], ep, ppid)), None)
            items.append({
                "key": ("tmux:" + managed) if managed else ("term:%d" % host) if host else ("pid:%d" % p["pid"]),
                "kind": "managed" if managed else "embedded" if host else ("headless" if headless else "proc"),
                "pid": p["pid"], "tty": p["tty"], "uptime": p["etime"], "mode": mode,
                "cwd": cwd, "session_id": sid, "transcript": sess[0] if sess else None,
                "title": title or "(no prompt yet)",
                "state": _state_from_age(age) if sess else "unknown",
                "state_estimated": True,
                "last_activity": sess[1] if sess else None,
                "confidence": confidence,
                "managed": managed,
                "can_send": bool(managed or host),
                "skip_perms": skips_permissions(p["argv"]),
            })
    # Ovenlight tmux panes with no claude process (e.g. claude exited, shell remained)
    listed = {i["managed"] for i in items if i.get("managed")}
    for name, pane in panes.items():
        if name not in listed:
            items.append({"key": "tmux:" + name, "kind": "managed", "pid": pane["pane_pid"], "tty": "", "uptime": "",
                          "mode": "interactive", "cwd": pane["cwd"], "session_id": None, "transcript": None,
                          "title": "(no claude running in this tmux)", "state": "ended", "state_estimated": False,
                          "last_activity": None, "confidence": "none", "managed": name, "can_send": True})
    agents, bg_err = background_sessions()
    items = _merge_agents(items, agents, conn)
    # Ovenlight's own children (`claude -p --fork-session` from "ask a fork") are not sessions of their own:
    # their answer is shown in the pane that asked. Browser terminals (term:) and tmux stay.
    items = [i for i in items
             if not (i.get("pid") and i["key"].split(":", 1)[0] in ("pid", "sid", "bg")
                     and i["pid"] != me and _has_ancestor(i["pid"], me, ppid))]
    items.sort(key=lambda i: (i.get("last_activity") or 0), reverse=True)
    return {"items": items, "background_error": bg_err, "tmux": tmux_ok(), "at": now}


# ---------------------------------------------------------------- live reading

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[@-_]")


def _strip_ansi(text: str) -> str:
    # cursor-forward codes (ESC[nG / ESC[nC) separate words in TUI output: turn them into spaces
    text = re.sub(r"\x1b\[\d*[GC]", " ", text)
    text = re.sub(r"\x1b\[\d*B", "\n", text)
    return re.sub(r"[ \t]+\n", "\n", _ANSI.sub("", text))


def tail_for(item_key: str, conn=None, n: int = 60, items: Optional[List[dict]] = None,
             max_bytes: int = 600_000) -> dict:
    """Latest messages of a live session. `items` (from live_sessions) avoids re-scanning processes."""
    kind, _, ref = item_key.partition(":")
    path = None
    if kind in ("bg", "pid", "tmux", "sid"):
        if items is None and kind != "sid":
            items = live_sessions(conn)["items"]
        for it in items or []:
            if it["key"] == item_key:
                path = it.get("transcript")
                break
    if path and not os.path.exists(path):
        path = None
    if not path:
        if kind == "bg":  # `claude logs` is raw TUI output; only a fallback
            rc, out, err = _run(["claude", "logs", ref], timeout=10)
            if rc != 0:
                raise LiveError("`claude logs %s` failed: %s" % (ref, (err or out).strip()[:200]))
            return {"format": "text", "text": _strip_ansi(out)[-20000:]}
        if kind == "tmux":
            rc, out, _ = _run(["tmux", "capture-pane", "-p", "-t", ref, "-S", "-80"])
            return {"format": "text", "text": out}
        if kind == "sid" and conn is not None:
            r = conn.execute("SELECT path FROM sessions WHERE id=?", (ref,)).fetchone()
            path = r["path"] if r else None
    if not path or not os.path.exists(path):
        raise LiveError("Could not find this session's transcript.")
    msgs = parse.read_messages(path, include_results=False, max_bytes=max_bytes)
    # the tail can be all tool output (e.g. base64 screenshots): read further back until there is text
    while len(msgs) < min(n, 3) and max_bytes < 32_000_000 and os.path.getsize(path) > max_bytes:
        max_bytes *= 8
        msgs = parse.read_messages(path, include_results=False, max_bytes=max_bytes)
    return {"format": "messages", "messages": msgs[-n:]}


def tails_for(keys: List[str], conn=None, n: int = 30) -> Dict[str, dict]:
    """Tails of several live sessions with a single process scan (for the grid view)."""
    items = live_sessions(conn)["items"]
    out: Dict[str, dict] = {}
    for k in keys[:16]:
        try:
            out[k] = tail_for(k, conn, n=n, items=items, max_bytes=250_000)
        except LiveError as exc:
            out[k] = {"error": str(exc)}
    return out


# ---------------------------------------------------------------- send / stop

def _tmux_send(name: str, text: str) -> None:
    if not tmux_ok():
        raise LiveError("tmux is not installed.")
    rc, _, err = _run(["tmux", "has-session", "-t", name])
    if rc != 0:
        raise LiveError("tmux session %s no longer exists." % name)
    buf = "ovenlight-send"
    rc, _, err = _run(["tmux", "load-buffer", "-b", buf, "-"], input=text)
    if rc != 0:
        raise LiveError("tmux load-buffer failed: %s" % err.strip())
    rc, _, err = _run(["tmux", "paste-buffer", "-p", "-d", "-b", buf, "-t", name])
    if rc != 0:
        raise LiveError("tmux paste-buffer failed: %s" % err.strip())
    time.sleep(0.25)
    _run(["tmux", "send-keys", "-t", name, "Enter"])


def _send_background(sid: str, text: str, timeout: float = 15.0) -> str:
    """EXPERIMENTAL (not tested against the real Claude Code).

    Per the docs, `claude --resume <session> "prompt"` delivers the prompt to a background session
    and then attaches to it in the terminal. With redirected stdout it sends nothing, so we run it
    inside a pty, wait for the confirmation and close the client (the session keeps running).
    """
    import fcntl
    import pty
    import select
    import struct
    import termios

    if not shutil.which("claude"):
        raise LiveError("`claude` not found on PATH.")
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    except OSError:
        pass
    proc = subprocess.Popen(["claude", "--resume", sid, text], stdin=slave, stdout=slave, stderr=slave,
                            close_fds=True, start_new_session=True, env=config.clean_env())
    os.close(slave)
    buf = b""
    ok = False
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.4)
            if r:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                if b"Sent your prompt" in buf:
                    ok = True
                    time.sleep(0.6)
                    break
            if proc.poll() is not None and not r:
                break
    finally:
        try:
            os.killpg(proc.pid, signal.SIGHUP)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master)
    text_out = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", buf).decode("utf-8", "replace").strip()
    if not ok:
        raise LiveError("Claude Code did not confirm the send. Output: %s" % (text_out[-300:] or "(empty)"))
    return text_out[-300:]


def send(item_key: str, text: str) -> str:
    kind, _, ref = item_key.partition(":")
    if not text.strip():
        raise LiveError("Empty message.")
    if kind == "term":
        try:
            term.send_to(int(ref), text)
        except (KeyError, ValueError):
            raise LiveError("That browser terminal is no longer open.")
        except OSError as exc:
            raise LiveError("Could not write to the terminal: %s" % exc)
        return "Sent to the browser terminal."
    if kind == "tmux":
        _tmux_send(ref, text)
        return "Sent to tmux session %s." % ref
    if kind == "bg":
        agents, _ = background_sessions()
        match = next((a for a in agents if ref in (a["id"], a["session_id"]) or a["session_id"].startswith(ref)), None)
        if match is not None and match["kind"] != "bg":
            raise LiveError(
                "Claude Code reports this session as interactive (%s), not background. "
                "Type /bg inside it to move it to the background, or use `ovenlight run`." % (match["agent_kind"] or "?"))
        return _send_background(match["session_id"] if match else ref, text)
    if kind in ("pid", "sid"):
        it = next((i for i in live_sessions()["items"] if i["key"] == item_key), None)
        if it is not None and it.get("agent_name"):
            msg = send_to_chat(it["agent_name"], text)
            if it.get("skip_perms"):
                msg += (" It runs with skip-permissions, so Claude Code holds messages from other sessions: "
                        "approve it in that session's terminal (\"Deliver this message to Claude\").")
            return msg
    raise LiveError(
        "Claude Code does not list this session for session messaging, and it runs in a plain terminal, "
        "so nothing can type into it. Type /bg inside it, or start your sessions with `ovenlight run`."
    )


# ---------------------------------------------------------------- session messaging (courier)
# Claude Code (2.1.317) lets local sessions message each other, but only from inside a session (SendMessage tool);
# there is no CLI for it. So Ovenlight starts a tiny headless courier: cheap model, no session saved, allowed to use
# nothing but the messaging tools, whose only job is to deliver the user's text to the target chat.
COURIER_MODEL = os.environ.get("OVENLIGHT_COURIER_MODEL", "claude-haiku-5-5")
COURIER_NOTE = ("[Sent by the user from the Ovenlight web UI, relayed by a short-lived courier that has already exited. "
                "Do not reply to the sender; treat this as the user's next message and answer here in this chat.]")


def courier_prompt(name: str, text: str) -> str:
    return ("You are a message courier. Use the SendMessage tool to send the text between <msg> tags to the local "
            "Claude Code session named \"%s\", exactly as written, nothing added or removed. Then reply with exactly "
            "DELIVERED, or FAILED: <reason>.\n\n<msg>\n%s\n\n%s\n</msg>" % (name, COURIER_NOTE, text))


def send_to_chat(name: str, text: str, timeout: float = 120.0) -> str:
    """Deliver `text` to the interactive session `name` (its agent-view name) through Claude Code session messaging."""
    if not shutil.which("claude"):
        raise LiveError("`claude` not found on PATH.")
    cmd = ["claude", "-p", "--model", COURIER_MODEL, "--output-format", "json", "--no-session-persistence",
           "--allowedTools=SendMessage,ListAgents,ToolSearch", courier_prompt(name, text)]
    rc, out, err = _run(cmd, timeout=timeout, env=config.clean_env(), cwd=str(config.ovenlight_dir()))
    if rc == 124:
        raise LiveError("The courier timed out; the message may not have been delivered.")
    try:
        data = json.loads(out)
    except ValueError:
        raise LiveError("Courier failed: %s" % ((err or out).strip()[:300] or "no output"))
    result = str(data.get("result") or "").strip()
    if rc != 0 or not result.startswith("DELIVERED"):
        raise LiveError("Could not deliver to %s: %s" % (name, (result or err or "unknown error")[:300]))
    cost = data.get("total_cost_usd")
    return "Delivered to %s%s." % (name, " (courier cost $%.4f)" % cost if isinstance(cost, (int, float)) else "")


def stop(item_key: str) -> str:
    kind, _, ref = item_key.partition(":")
    if kind == "tmux":
        rc, _, err = _run(["tmux", "kill-session", "-t", ref])
        if rc != 0:
            raise LiveError("Failed: %s" % err.strip())
        return "tmux session %s ended." % ref
    if kind == "bg":
        rc, out, err = _run(["claude", "stop", ref], timeout=15)
        if rc != 0:
            raise LiveError("`claude stop %s` failed: %s" % (ref, (err or out).strip()[:200]))
        return "Stopped: %s" % ref
    raise LiveError("Only background or Ovenlight-managed sessions can be stopped. Close the session's terminal.")


def ask_headless(sid: str, text: str, fork: bool = True, timeout: float = 300.0, cwd: Optional[str] = None) -> dict:
    """Ask a session via `claude -p --resume` (on a copy/fork by default).

    Returns {"answer", "session_id"}; session_id is the fork's id, so a follow-up can resume the same fork
    (fork=False) instead of starting a new copy every time."""
    if not shutil.which("claude"):
        raise LiveError("`claude` not found on PATH.")
    cmd = ["claude", "-p", "--resume", sid, "--output-format", "json"]  # run with clean_env below
    if fork:
        cmd.append("--fork-session")
    cmd.append(text)
    rc, out, err = _run(cmd, timeout=timeout, env=config.clean_env(), cwd=cwd)
    if rc != 0:
        raise LiveError("claude -p failed: %s" % ((err or out).strip()[:300]))
    try:
        data = json.loads(out)
    except ValueError:
        return {"answer": out, "session_id": None}
    if not isinstance(data, dict):
        return {"answer": out, "session_id": None}
    return {"answer": str(data.get("result", "")), "session_id": data.get("session_id")}

"""Ovenlight command line."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from datetime import datetime
from typing import List, Optional

from . import actions, config, db, indexer, live, parse, query, search

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(code: str, s: str) -> str:
    return "\033[%sm%s\033[0m" % (code, s) if USE_COLOR else s


def dim(s): return c("2", s)
def bold(s): return c("1", s)
def amber(s): return c("33", s)
def red(s): return c("31", s)


def fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def short_path(p: str, n: int = 2) -> str:
    parts = [x for x in (p or "").split("/") if x]
    return "/".join(parts[-n:]) if parts else "(no folder)"


def hl(snip: str) -> str:
    return snip.replace("«", c("1;33", "")).replace("»", c("0", "")) if USE_COLOR else snip.replace("«", "[").replace("»", "]")


def print_session_line(s: dict) -> None:
    flags = []
    if s.get("starred"): flags.append("★")
    if s.get("gone"): flags.append(red("deleted"))
    if s.get("orphan"): flags.append("orphan")
    flags += s.get("tags", [])
    print("%s  %s  %-22s %-16s %4d  %s %s" % (
        amber(s["id"][:8]), dim(fmt_ts(s["ended"])), short_path(s["cwd"])[:22], (s["branch"] or "")[:16],
        s["n_msgs"], bold(s["title"][:80]), dim(" ".join(flags))))


# ---------------------------------------------------------------- commands

def cmd_index(a) -> int:
    conn = db.connect()
    if not db.has_fts5():
        print("Your SQLite has no FTS5; Ovenlight requires it.", file=sys.stderr)
        return 1
    log = (lambda m: print(dim(m))) if a.verbose else None
    st = indexer.index_all(conn, force=a.force, log=log, include_results=a.outputs or None)
    print("%d files: %d indexed, %d unchanged, %d with errors, %d transcripts gone (%.1fs)" % (
        st["scanned"], st["indexed"], st["skipped"], st["errors"], st["gone"], st["seconds"]))
    for e in st["error_samples"]:
        print(red("  error: " + e))
    if a.backup:
        b = indexer.backup_all()
        print("backup: %d copied, %d already up to date, %d errors" % (b["copied"], b["unchanged"], b["errors"]))
    return 0


def _ensure_index(conn) -> None:
    if not conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
        print(dim("First run: indexing…"), file=sys.stderr)
        indexer.index_all(conn)


def cmd_search(a) -> int:
    conn = db.connect()
    _ensure_index(conn)
    r = search.run(conn, " ".join(a.query), limit=a.n, order=a.order)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0
    for w in r["warnings"]:
        print(amber("warning: " + w), file=sys.stderr)
    for s in r["results"]:
        print_session_line(s)
        for sn in s["snippets"]:
            print("      %s %s" % (dim(sn["kind"] + ":"), hl(sn["snip"].replace("\n", " "))))
    print(dim("%d sessions%s" % (r["total"], "" if len(r["results"]) == r["total"] else " (showing %d)" % len(r["results"]))))
    return 0


def cmd_list(a) -> int:
    conn = db.connect()
    _ensure_index(conn)
    qs = " ".join(a.filters)
    r = search.run(conn, qs, limit=a.n)
    for s in r["results"]:
        print_session_line(s)
    print(dim("%d sessions" % r["total"]))
    return 0


def cmd_show(a) -> int:
    conn = db.connect()
    row = db.find_session(conn, a.id)
    s = search.session_dict(row)
    print(bold(s["title"]))
    print(dim("id:       " + s["id"]))
    print(dim("folder:   %s%s" % (s["cwd"] or "—", "  (no longer exists)" if s["orphan"] else "")))
    print(dim("branch:   %s   model: %s" % (s["branch"] or "—", s["model"] or "—")))
    print(dim("period:   %s to %s   msgs: %d   tokens: %d/%d" % (fmt_ts(s["started"]), fmt_ts(s["ended"]), s["n_msgs"], s["tok_in"], s["tok_out"])))
    print(dim("file:     %s%s" % (s["path"], "  (DELETED)" if s["gone"] else "")))
    if not s["path"] or not os.path.exists(s["path"]):
        print(amber("\nTranscript unavailable. Try `ovenlight restore %s`." % s["id"][:8]))
        return 0
    msgs = parse.read_messages(s["path"], include_results=a.outputs)
    if not a.full:
        msgs = msgs[-a.tail:]
    print()
    for m in msgs:
        k = m["kind"]
        txt = m["text"] if a.full or k in ("prompt", "reply") else m["text"][:200].replace("\n", " ")
        if k == "prompt":
            print(c("1;33", "you    ") + txt)
        elif k == "reply":
            print(c("1;36", "claude ") + txt)
        else:
            print(dim("  %s %s" % (k, txt)))
        print()
    return 0


def cmd_open(a) -> int:
    conn = db.connect()
    row = db.find_session(conn, a.id)
    if a.shell:
        plan = actions.plan_shell(row, here=a.here, claude=not a.no_claude)
        for w in plan["warnings"]:
            print(amber("warning: " + w), file=sys.stderr)
        if a.print:
            print(plan["shell"])
            return 0
        if a.this_terminal:
            os.chdir(plan["cwd"])
            sh = os.environ.get("SHELL") or "/bin/bash"
            if a.no_claude:
                os.execvp(sh, [sh, "-l"])
            os.execvp(sh, [sh, "-l", "-c", 'claude; exec "${SHELL:-/bin/bash}" -l'])
        config.audit("open", session=row["id"], mode="shell", claude=not a.no_claude, via="cli")
        r = actions.reopen(row, mode="shell", here=a.here, claude=not a.no_claude)
        print(r["message"] or "Done.")
        if not r["opened"]:
            print("Run it manually:  " + r["command"])
        return 0 if r["opened"] else 1
    plan = actions.plan_reopen(row, bg=a.bg, fork=a.fork, here=a.here)
    for w in plan["warnings"]:
        print(amber("warning: " + w), file=sys.stderr)
    if a.print:
        print(plan["shell"])
        return 0
    cf = actions.conflicts(row["id"], conn)
    if cf and not a.force:
        print(red("This session seems to be open right now (pid %s). Opening it again mixes the transcripts." % cf[0].get("pid", "?")))
        print("Use --fork for a copy, or --force to open it anyway.")
        return 2
    config.audit("open", session=row["id"], mode="managed" if a.managed else "terminal", via="cli")
    if a.managed:
        r = actions.reopen(row, mode="managed", bg=a.bg, fork=a.fork, here=a.here)
    elif a.this_terminal:
        os.chdir(plan["cwd"])
        os.execvp(plan["argv"][0], plan["argv"])
    else:
        r = actions.reopen(row, mode="terminal", bg=a.bg, fork=a.fork, here=a.here)
    print(r["message"] or "Done.")
    if not r["opened"]:
        print("Run it manually:  " + r["command"])
    return 0 if r["opened"] else 1


def _state_color(st: str) -> str:
    s = st.lower()
    if "work" in s or "run" in s or "busy" in s: return amber("● " + st)
    if "input" in s or "need" in s or "wait" in s: return red("● " + st)
    return dim("● " + st)


def cmd_live(a) -> int:
    conn = db.connect()
    while True:
        r = live.live_sessions(conn)
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
            return 0
        if a.follow:
            print("\033[2J\033[H", end="")
        kinds = {"bg": "background", "managed": "managed", "proc": "terminal", "headless": "headless",
                 "embedded": "browser"}
        for it in r["items"]:
            ts = fmt_ts(it["last_activity"]) if it.get("last_activity") else "—"
            print("%-14s %-16s %-14s %-24s %s" % (it["key"][:14], _state_color(it["state"]), kinds.get(it["kind"], it["kind"]),
                                                  short_path(it["cwd"])[:24], bold(it["title"][:60])))
            if it.get("session_id"):
                print(dim("               session %s  activity %s  match: %s" % (it["session_id"][:8], ts, it["confidence"])))
        if not r["items"]:
            print(dim("No sessions running."))
        if r["background_error"]:
            print(dim("agent view: " + r["background_error"]))
        if not a.follow:
            return 0
        try:
            time.sleep(2)
        except KeyboardInterrupt:
            return 0


def cmd_send(a) -> int:
    key = a.target
    conn = db.connect()
    if ":" not in key:  # accepts pid, tmux name or short id
        items = live.live_sessions(conn)["items"]
        hit = [i for i in items if key in (i.get("managed"), str(i.get("pid")), i.get("id"), (i.get("session_id") or "")[:8])]
        if not hit:
            print(red("No live session found for %r. See `ovenlight live`." % key))
            return 1
        key = hit[0]["key"]
    try:
        config.audit("send", key=key, text=" ".join(a.text), via="cli")
        print(live.send(key, " ".join(a.text)))
    except live.LiveError as exc:
        print(red(str(exc)))
        return 1
    return 0


def cmd_ask(a) -> int:
    conn = db.connect()
    row = db.find_session(conn, a.id)
    try:
        r = live.ask_headless(row["id"], " ".join(a.text), fork=not a.no_fork, cwd=row["cwd"])
        print(r["answer"])
        if r.get("session_id") and not a.no_fork:
            print(dim("\nfork: %s  (continue with: claude --resume %s)" % (r["session_id"], r["session_id"])))
    except live.LiveError as exc:
        print(red(str(exc)))
        return 1
    return 0


def cmd_stop(a) -> int:
    key = a.target if ":" in a.target else "tmux:" + a.target if a.target.startswith("ovenlight-") else "bg:" + a.target
    try:
        print(live.stop(key))
    except live.LiveError as exc:
        print(red(str(exc)))
        return 1
    return 0


def cmd_run(a) -> int:
    cwd = os.path.abspath(a.folder)
    try:
        name = actions.run_managed(cwd, a.claude_args)
    except actions.ActionError as exc:
        print(red(str(exc)))
        return 1
    print("Managed session: %s (Ovenlight can send prompts to it)" % name)
    if a.detached:
        print("To attach: tmux attach -t %s" % name)
        return 0
    os.execvp("tmux", ["tmux", "attach", "-t", name])
    return 0


def cmd_web(a) -> int:
    from . import web
    web.serve(port=a.port, auto_index=not a.no_auto, open_browser=a.open, terminal=not a.no_terminal)
    return 0


def cmd_stats(a) -> int:
    conn = db.connect()
    _ensure_index(conn)
    s = search.summary(conn)
    print(bold("%d sessions" % s["sessions"]) + dim("  (%s to %s)" % (fmt_ts(s["first"]), fmt_ts(s["last"]))))
    print("tokens: %s in, %s out   transcripts: %.1f MB" % (format(s["tokens_in"], ","), format(s["tokens_out"], ","), s["bytes"] / 1e6))
    print("deleted transcripts (only in Ovenlight): %d   missing source folders: %d" % (s["gone"], s["orphan"]))
    print()
    for p in s["projects"]:
        print("%4d  %s  %s" % (p["n"], dim(fmt_ts(p["last"])), p["name"]))
    return 0


def cmd_tag(a) -> int:
    conn = db.connect()
    row = db.find_session(conn, a.id)
    tags = [t for t in (row["tags"] or "").split(",") if t]
    t = a.tag.strip().lower().replace(",", "")
    if a.remove and t in tags:
        tags.remove(t)
    elif not a.remove and t not in tags:
        tags.append(t)
    conn.execute("UPDATE sessions SET tags=? WHERE id=?", (",".join(tags), row["id"]))
    conn.commit()
    print("tags: " + (", ".join(tags) or "(none)"))
    return 0


def cmd_star(a) -> int:
    conn = db.connect()
    row = db.find_session(conn, a.id)
    new = 0 if row["starred"] else 1
    conn.execute("UPDATE sessions SET starred=? WHERE id=?", (new, row["id"]))
    conn.commit()
    print("starred" if new else "unstarred")
    return 0


def cmd_backup(a) -> int:
    r = indexer.backup_all(log=print if a.verbose else None)
    print("backup in %s: %d copied, %d up to date, %d errors" % (config.backup_dir(), r["copied"], r["unchanged"], r["errors"]))
    return 0


def cmd_restore(a) -> int:
    conn = db.connect()
    try:
        print("Restored to " + indexer.restore(conn, a.id))
    except (FileNotFoundError, LookupError) as exc:
        print(red(str(exc)))
        return 1
    return 0


HOOK_SNIPPET = {
    "hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "ovenlight hook"}]}]}
}


def cmd_hook(a) -> int:
    if a.snippet:
        print(json.dumps(HOOK_SNIPPET, indent=2))
        print("\nPaste into ~/.claude/settings.json (merging with what is already there) and check the format against the hooks documentation.", file=sys.stderr)
        return 0
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    tp = data.get("transcript_path")
    if not tp or not os.path.exists(tp):
        return 0
    from pathlib import Path
    conn = db.connect()
    try:
        indexer.index_file(conn, Path(tp), force=True)
        conn.commit()
        import shutil
        dst = config.backup_dir() / Path(tp).parent.name / Path(tp).name
        config.secure_mkdir(dst.parent)
        shutil.copy2(tp, dst)
        config.secure_file(dst)
    except Exception as exc:
        print("ovenlight hook: %s" % exc, file=sys.stderr)
    return 0


def cmd_doctor(a) -> int:
    import platform
    import shutil
    ok = lambda b: c("32", "ok") if b else red("failed")
    print("Ovenlight %s, Python %s, %s" % (config.VERSION, platform.python_version(), sys.platform))
    print("SQLite FTS5:          %s" % ok(db.has_fts5()))
    pd = config.projects_dir()
    n = len([f for p in pd.iterdir() if p.is_dir() for f in p.glob("*.jsonl")]) if pd.is_dir() else 0
    print("Transcripts:          %s  (%d files in %s)" % (ok(pd.is_dir()), n, pd))
    cl = shutil.which("claude")
    if cl:
        rc, out, _ = live._run(["claude", "--version"])
        print("claude on PATH:       ok  (%s)" % (out.strip() or cl))
    else:
        print("claude on PATH:       %s  (open/send/agent view need it)" % red("not found"))
    print("tmux (level B):       %s" % (c("32", "ok") if live.tmux_ok() else amber("missing — `ovenlight run` unavailable")))
    print("ps / lsof:            %s / %s" % (ok(bool(shutil.which("ps"))), ok(bool(shutil.which("lsof")) or sys.platform.startswith("linux"))))
    conn = db.connect()
    raw = db.get_meta(conn, "last_index_stats")
    if raw:
        st = json.loads(raw)
        print("Last index run:       %d files, %d errors" % (st["scanned"], st["errors"]))
        for e in st.get("error_samples", [])[:5]:
            print(red("   " + e))
    # format health sample
    bad = lines = 0
    types = {}
    if pd.is_dir():
        files = sorted((f for p in pd.iterdir() if p.is_dir() for f in p.glob("*.jsonl")), key=lambda f: f.stat().st_mtime, reverse=True)[:20]
        for f in files:
            res = parse.parse_file(str(f))
            bad += res["stats"]["bad"]
            lines += res["stats"]["lines"]
            for k, v in res["stats"]["types"].items():
                types[k] = types.get(k, 0) + v
        print("Sample (20 most recent): %d lines, %d unreadable" % (lines, bad))
        if types:
            print("   entry types seen: " + ", ".join("%s=%d" % kv for kv in sorted(types.items(), key=lambda x: -x[1])[:10]))
    return 0


def cmd_search_help(a) -> int:
    print(query.HELP)
    return 0


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ovenlight", description="Ovenlight: find, follow and reopen your Claude Code sessions.",
                                epilog="Independent project, not affiliated with or endorsed by Anthropic. Claude and Claude Code are trademarks of Anthropic.")
    p.add_argument("--version", action="version", version="ovenlight " + config.VERSION)
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    def add(name, fn, help, aliases=()):
        sp = sub.add_parser(name, help=help, description=help, aliases=list(aliases))
        sp.set_defaults(fn=fn)
        return sp

    sp = add("index", cmd_index, "index transcripts (incremental)")
    sp.add_argument("--force", action="store_true", help="reindex everything")
    sp.add_argument("--backup", action="store_true", help="also copy transcripts to ~/.ovenlight/backup")
    sp.add_argument("--outputs", action="store_true", help="also index tool output (much larger database)")
    sp.add_argument("-v", "--verbose", action="store_true")

    sp = add("search", cmd_search, "advanced search (see `ovenlight search-help`)", ["s"])
    sp.add_argument("query", nargs="+")
    sp.add_argument("-n", type=int, default=20, help="max results (default 20)")
    sp.add_argument("--order", choices=["auto", "recent"], default="auto")
    sp.add_argument("--json", action="store_true")

    sp = add("list", cmd_list, "list sessions (accepts the same filters as search)", ["ls"])
    sp.add_argument("filters", nargs="*", help="e.g. project:shop after:7d")
    sp.add_argument("-n", type=int, default=30, help="max results (default 30)")

    sp = add("show", cmd_show, "show a session")
    sp.add_argument("id")
    sp.add_argument("--tail", type=int, default=30, help="last N messages")
    sp.add_argument("--full", action="store_true", help="show the whole conversation")
    sp.add_argument("--outputs", action="store_true", help="include tool output")

    sp = add("open", cmd_open, "reopen a session in a new terminal window", ["resume"])
    sp.add_argument("id")
    sp.add_argument("--bg", action="store_true", help="resume in the background")
    sp.add_argument("--fork", action="store_true", help="resume a copy (does not mix transcripts)")
    sp.add_argument("--managed", action="store_true", help="open inside a Ovenlight tmux (lets you send prompts later)")
    sp.add_argument("--here", action="store_true", help="use the current folder instead of the original one")
    sp.add_argument("--this-terminal", action="store_true", help="run in this terminal instead of opening a new window")
    sp.add_argument("--shell", action="store_true", help="open a shell in the session folder that starts a new claude")
    sp.add_argument("--no-claude", action="store_true", help="with --shell: just the shell, without starting claude")
    sp.add_argument("--print", action="store_true", help="only print the command")
    sp.add_argument("--force", action="store_true", help="ignore the session-already-open warning")

    sp = add("live", cmd_live, "sessions running now", ["ps"])
    sp.add_argument("--follow", "-f", action="store_true", help="refresh every 2 s")
    sp.add_argument("--json", action="store_true")

    sp = add("send", cmd_send, "send a prompt to a live session (background or managed)")
    sp.add_argument("target", help="key (bg:ID, tmux:NAME), tmux name, pid or short id")
    sp.add_argument("text", nargs="+")

    sp = add("ask", cmd_ask, "ask a session a question (claude -p --resume, on a fork)")
    sp.add_argument("id")
    sp.add_argument("text", nargs="+")
    sp.add_argument("--no-fork", action="store_true", help="write the answer into the original session")

    sp = add("stop", cmd_stop, "stop a background or managed session")
    sp.add_argument("target")

    sp = add("run", cmd_run, "start claude inside a Ovenlight tmux (level B); claude args go after --")
    sp.add_argument("folder", nargs="?", default=".")
    sp.add_argument("--detached", action="store_true", help="create it but do not attach")

    sp = add("web", cmd_web, "local web interface", ["serve"])
    sp.add_argument("--port", type=int, default=7777)
    sp.add_argument("--open", action="store_true", help="open the browser")
    sp.add_argument("--no-auto", action="store_true", help="no automatic reindex every 60 s")
    sp.add_argument("--no-terminal", action="store_true", help="disable the embedded browser terminal")

    add("stats", cmd_stats, "totals and projects")
    sp = add("tag", cmd_tag, "add/remove a tag")
    sp.add_argument("id"); sp.add_argument("tag"); sp.add_argument("--remove", action="store_true")
    sp = add("star", cmd_star, "toggle starred")
    sp.add_argument("id")
    sp = add("backup", cmd_backup, "copy transcripts to ~/.ovenlight/backup")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp = add("restore", cmd_restore, "restore a deleted transcript from the backup")
    sp.add_argument("id")
    sp = add("hook", cmd_hook, "SessionEnd hook entry point (--snippet prints the JSON)")
    sp.add_argument("--snippet", action="store_true")
    add("doctor", cmd_doctor, "check the environment")
    add("search-help", cmd_search_help, "show the search syntax")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    p = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: List[str] = []
    if argv and argv[0] == "run" and "--" in argv:
        i = argv.index("--")  # everything after -- goes to claude
        argv, extra = argv[:i], argv[i + 1:]
    a = p.parse_args(argv)
    a.claude_args = extra
    if not getattr(a, "fn", None):
        p.print_help()
        return 0
    try:
        return a.fn(a) or 0
    except LookupError as exc:
        print(red(str(exc)), file=sys.stderr)
        return 1
    except actions.ActionError as exc:
        print(red(str(exc)), file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

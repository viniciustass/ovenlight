"""Ovenlight local web interface. Listens on 127.0.0.1 only and requires a token."""
from __future__ import annotations

import json
import os
import pkgutil
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import actions, config, db, indexer, live, parse, search, term

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _build_id() -> bytes:
    """Hash of the UI and static files: static URLs carry it (?b=...) so browsers never keep a stale copy."""
    import hashlib
    h = hashlib.sha1()
    for name in ("ui.html", "static/favicon.svg", "static/xterm.js", "static/xterm.css", "static/addon-fit.js"):
        h.update(pkgutil.get_data("ovenlight", name) or b"")
    return h.hexdigest()[:12].encode()


BUILD = _build_id()
STATIC = {"xterm.js": "application/javascript", "addon-fit.js": "application/javascript", "xterm.css": "text/css",
          "favicon.svg": "image/svg+xml"}


def load_token() -> str:
    p = config.ovenlight_dir() / "token"
    if p.exists():
        t = p.read_text().strip()
        if t:
            return t
    t = secrets.token_urlsafe(24)
    p.write_text(t)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return t


class Handler(BaseHTTPRequestHandler):
    server_version = "Ovenlight"
    token = ""
    terminal = True  # embedded terminal (ovenlight web --no-terminal turns it off)
    forks: dict = {}  # fork session id -> cwd, for forks this server created (only those can be continued)

    def log_message(self, fmt, *args):  # quiet
        pass

    # ---- util
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        return name in ALLOWED_HOSTS

    def _cookie_token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ovenlight_t":
                return v
        return ""

    def _authed(self) -> bool:
        t = self._cookie_token() or self.headers.get("X-Ovenlight-Token") or ""
        return secrets.compare_digest(t, self.token)

    def _origin_ok(self, required: bool = False) -> bool:
        """Origin must be this very server: local host AND same port (cookies are not port-scoped,
        so another app on localhost:3000 would otherwise ride on Ovenlight's cookie)."""
        origin = self.headers.get("Origin")
        if not origin:
            return not required
        u = urlparse(origin)
        try:
            port = u.port or (443 if u.scheme == "https" else 80)
        except ValueError:
            return False
        return (u.hostname or "") in ("127.0.0.1", "localhost", "::1") and port == self.server.server_address[1]

    # ---- GET
    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "Host not allowed"}, 403)
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            t = (qs.get("t") or [""])[0]
            if t and secrets.compare_digest(t, self.token):
                self.send_response(302)
                self.send_header("Set-Cookie", "ovenlight_t=%s; HttpOnly; SameSite=Strict; Path=/" % self.token)
                self.send_header("Location", "/")
                self.end_headers()
                return
            if not self._authed():
                body = ("<meta charset=utf-8><body style='font:16px system-ui;padding:3em'>"
                        "<h3>Ovenlight</h3><p>Open the full address printed in the terminal "
                        "(with <code>?t=…</code>) once to authorize this browser.</p>").encode()
                self.send_response(401)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            html = (pkgutil.get_data("ovenlight", "ui.html") or b"ui.html missing").replace(b"__BUILD__", BUILD)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
            return
        if not self._authed():
            return self._json({"error": "Unauthorized"}, 401)
        if u.path.startswith("/static/"):
            return self._static(u.path[len("/static/"):])
        if u.path == "/api/term":
            return self._term(qs)
        try:
            self._api_get(u.path, qs)
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": str(exc)}, 500)

    def _api_get(self, path, qs):
        a = lambda k, d="": (qs.get(k) or [d])[0]
        conn = db.connect()
        try:
            if path == "/api/search":
                return self._json(search.run(conn, a("q"), limit=int(a("limit", "60")), offset=int(a("offset", "0")),
                                             order=a("order", "auto")))
            if path == "/api/config":
                return self._json({"terminal": bool(self.terminal), "version": config.VERSION})
            if path == "/api/stats":
                return self._json(search.summary(conn))
            if path == "/api/live":
                return self._json(live.live_sessions(conn))
            if path == "/api/tail":
                try:
                    return self._json(live.tail_for(a("key"), conn, int(a("n", "60"))))
                except live.LiveError as exc:
                    return self._json({"error": str(exc)}, 404)
            if path == "/api/tails":
                keys = [k for k in a("keys").split(",") if k][:16]
                return self._json(live.tails_for(keys, conn, n=max(1, min(80, int(a("n", "30"))))))
            if path == "/api/session":
                return self._json(self._session_detail(conn, a("id")))
        finally:
            conn.close()
        self._json({"error": "unknown route"}, 404)

    def _static(self, name):
        ctype = STATIC.get(name)
        data = pkgutil.get_data("ovenlight", "static/" + name) if ctype else None
        if not data:
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")  # URLs carry the build hash
        self.end_headers()
        self.wfile.write(data)

    def _term_target(self, conn, sid, mode, force):
        """Validate a terminal request. Returns (row, cwd) or raises _Refuse."""
        if mode not in ("claude", "shell", "bare"):
            raise _Refuse(400, "Unknown terminal mode.")
        if sid in Handler.forks:  # a fork created here may not be indexed yet
            indexer.index_all(conn)
        row = db.find_session(conn, sid)
        if mode == "claude":
            if row["gone"]:
                raise _Refuse(410, "This session's transcript was deleted. Restore it first: ovenlight restore %s" % row["id"][:8])
            if not force and actions.conflicts(row["id"], conn):
                raise _Refuse(409, "This session seems to be open right now. Running it here too mixes the transcripts.")
        cwd, _ = actions.session_cwd(row)
        return row, cwd

    def _term(self, qs):
        a = lambda k, d="": (qs.get(k) or [d])[0]
        if not self.terminal:
            return self._json({"error": "The embedded terminal is disabled (ovenlight web --no-terminal)."}, 403)
        if not self._origin_ok(required=True):
            return self._json({"error": "Origin not allowed"}, 403)
        key = self.headers.get("Sec-WebSocket-Key")
        if (self.headers.get("Upgrade") or "").lower() != "websocket" or not key:
            return self._json({"error": "WebSocket upgrade required"}, 400)
        mode = a("mode", "claude")
        conn = db.connect()
        try:
            row, cwd = self._term_target(conn, a("id"), mode, a("force") == "1")
        except _Refuse as r:
            return self._json({"error": r.msg}, r.status)
        except LookupError as exc:
            return self._json({"error": str(exc)}, 404)
        finally:
            conn.close()
        argv = term.program(mode, row["id"])
        config.audit("terminal", mode=mode, session=row["id"], cwd=cwd, argv=argv)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", term.accept_key(key))
        self.end_headers()
        self.close_connection = True
        try:
            cols, rows = int(a("cols", "80")), int(a("rows", "24"))
        except ValueError:
            cols, rows = 80, 24
        term.serve_ws(term.WS(self.rfile, self.wfile, self.connection), argv, cwd, cols, rows)

    def _session_detail(self, conn, sid):
        row = db.find_session(conn, sid)
        d = search.session_dict(row)
        msgs = []
        if row["path"] and os.path.exists(row["path"]):
            msgs = parse.read_messages(row["path"], include_results=False)
        cut = None
        if len(msgs) > 160:
            cut = len(msgs) - 160
            msgs = msgs[:40] + msgs[-120:]
        for m in msgs:
            m["text"] = m["text"][:2000]
        d["messages"] = msgs
        d["omitted"] = cut
        d["files"] = [r["path"] for r in conn.execute("SELECT path FROM files WHERE session_id=? ORDER BY path", (row["id"],))]
        d["tools"] = [{"name": r["name"], "n": r["n"]} for r in conn.execute(
            "SELECT name, n FROM tools WHERE session_id=? ORDER BY n DESC", (row["id"],))]
        return d

    # ---- POST
    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "Host not allowed"}, 403)
        if not self._authed() or self.headers.get("X-Ovenlight") != "1" or not self._origin_ok():
            return self._json({"error": "Unauthorized"}, 401)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return self._json({"error": "invalid JSON"}, 400)
        path = urlparse(self.path).path
        conn = db.connect()
        try:
            return self._api_post(conn, path, body)
        except (live.LiveError, actions.ActionError, LookupError) as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            traceback.print_exc()
            return self._json({"error": str(exc)}, 500)
        finally:
            conn.close()

    def _api_post(self, conn, path, b):
        if path == "/api/index":
            return self._json(indexer.index_all(conn, force=bool(b.get("force"))))
        if path == "/api/reopen":
            row = db.find_session(conn, b.get("id", ""))
            mode = b.get("mode", "terminal")
            if mode != "print":
                config.audit("open", session=row["id"], mode=mode, bg=bool(b.get("bg")), fork=bool(b.get("fork")))
            if mode == "shell":
                return self._json(actions.reopen(row, mode="shell", claude=b.get("claude", True) is not False))
            if not b.get("force"):
                c = actions.conflicts(row["id"], conn)
                if c:
                    return self._json({"conflict": True, "items": c,
                                       "error": "This session seems to be open right now. Opening it again mixes the transcripts."}, 409)
            return self._json(actions.reopen(row, mode=b.get("mode", "terminal"), bg=bool(b.get("bg")),
                                             fork=bool(b.get("fork"))))
        if path == "/api/term/check":
            if not self.terminal:
                return self._json({"error": "The embedded terminal is disabled (ovenlight web --no-terminal)."}, 403)
            try:
                row, cwd = self._term_target(conn, b.get("id", ""), b.get("mode", "claude"), bool(b.get("force")))
            except _Refuse as r:
                return self._json({"error": r.msg, "conflict": r.status == 409}, r.status)
            return self._json({"ok": True, "cwd": cwd, "id": row["id"]})
        if path == "/api/send":
            config.audit("send", key=b.get("key", ""), text=b.get("text", ""))
            return self._json({"message": live.send(b.get("key", ""), b.get("text", ""))})
        if path == "/api/stop":
            config.audit("stop", key=b.get("key", ""))
            return self._json({"message": live.stop(b.get("key", ""))})
        if path == "/api/ask":
            text = b.get("text", "")
            cont = b.get("continue_fork")
            if cont:  # follow-up in a fork this server created: same copy, no new fork
                if cont not in Handler.forks:
                    return self._json({"error": "Unknown fork; start a new one."}, 400)
                sid, cwd, fork = cont, Handler.forks[cont], False
            else:
                row = db.find_session(conn, b.get("id", ""))
                sid, cwd, fork = row["id"], row["cwd"], not b.get("no_fork")
            config.audit("ask", session=sid, text=text, fork=fork, continued=bool(cont))
            res = live.ask_headless(sid, text, fork=fork, cwd=cwd)
            if fork and res.get("session_id"):
                Handler.forks[res["session_id"]] = cwd
            return self._json(res)
        if path == "/api/tag":
            row = db.find_session(conn, b.get("id", ""))
            tags = [t for t in (row["tags"] or "").split(",") if t]
            t = str(b.get("tag", "")).strip().lower().replace(",", "")
            if t:
                if b.get("on", True) and t not in tags:
                    tags.append(t)
                if not b.get("on", True) and t in tags:
                    tags.remove(t)
                conn.execute("UPDATE sessions SET tags=? WHERE id=?", (",".join(tags), row["id"]))
                conn.commit()
            return self._json({"tags": tags})
        if path == "/api/star":
            row = db.find_session(conn, b.get("id", ""))
            conn.execute("UPDATE sessions SET starred=? WHERE id=?", (1 if b.get("on", True) else 0, row["id"]))
            conn.commit()
            return self._json({"starred": bool(b.get("on", True))})
        self._json({"error": "unknown route"}, 404)


class _Refuse(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _auto_index(stop: threading.Event, every: int = 60) -> None:
    while not stop.wait(every):
        try:
            conn = db.connect()
            try:
                indexer.index_all(conn)
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()


def make_server(port: int = 7777, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    Handler.token = load_token()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def serve(port: int = 7777, auto_index: bool = True, open_browser: bool = False, terminal: bool = True) -> None:
    srv = make_server(port)
    Handler.terminal = terminal
    url = "http://127.0.0.1:%d/?t=%s" % (srv.server_address[1], Handler.token)
    print("Ovenlight running at %s" % url)
    print("(listening on 127.0.0.1 only; Ctrl+C to quit)")
    stop = threading.Event()
    conn = db.connect()  # the first index always runs; --no-auto only disables the periodic one
    try:
        if not conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
            print("First run: indexing your sessions…")
            print(indexer.index_all(conn))
    finally:
        conn.close()
    if auto_index:
        threading.Thread(target=_auto_index, args=(stop,), daemon=True).start()
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    import signal

    def _term(*_):  # `kill <pid>` also ends browser terminals cleanly (same path as Ctrl+C)
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _term)
    except ValueError:  # not in the main thread (tests)
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        term.kill_all()
        srv.server_close()

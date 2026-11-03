import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ovenlight import actions, config, db, indexer, live, parse, query, search  # noqa: E402


def ts(offset_days=0, hh=10):
    t = time.time() - offset_days * 86400
    return time.strftime("%Y-%m-%dT", time.gmtime(t)) + "%02d:00:00.000Z" % hh


def write_session(proj_dir: Path, sid: str, cwd: str, branch: str, entries):
    proj_dir.mkdir(parents=True, exist_ok=True)
    p = proj_dir / (sid + ".jsonl")
    with open(p, "w") as f:
        for e in entries:
            if isinstance(e, dict):
                e.setdefault("cwd", cwd)
                e.setdefault("gitBranch", branch)
                e.setdefault("sessionId", sid)
                e.setdefault("version", "2.1.0")
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return p


def user(text, t):
    return {"type": "user", "timestamp": t, "message": {"role": "user", "content": text}}


def asst(blocks, t, mid, usage=(10, 5, 100), model="claude-opus-x"):
    return {"type": "assistant", "timestamp": t,
            "message": {"id": mid, "role": "assistant", "model": model,
                        "content": blocks,
                        "usage": {"input_tokens": usage[0], "output_tokens": usage[1], "cache_read_input_tokens": usage[2]}}}


def tool_result(text, t, is_error=False):
    return {"type": "user", "timestamp": t, "message": {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": text, "is_error": is_error}]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_env = dict(os.environ)
        os.environ["HOME"] = self.tmp
        os.environ["OVENLIGHT_HOME"] = os.path.join(self.tmp, ".ovenlight")
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.projects = Path(self.tmp) / ".claude" / "projects"
        self.shop = os.path.join(self.tmp, "work", "shop")
        os.makedirs(self.shop)
        self.gone_dir = "/tmp/folder-that-does-not-exist-ovenlight/blog"
        self.make_data()
        self.conn = db.connect()
        indexer.index_all(self.conn)

    def tearDown(self):
        self.conn.close()
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_data(self):
        pj = self.projects / config.encode_cwd(self.shop)
        # session 1: Stripe webhook, today
        write_session(pj, "aaaaaaaa-1111-4000-8000-000000000001", self.shop, "feat/stripe", [
            {"type": "summary", "summary": "Fix Stripe webhook", "leafUuid": "z"},
            user("I need to fix the Stripe webhook that returns error 500", ts(0, 9)),
            asst([{"type": "text", "text": "I will check the webhook handler in the café module."},
                  {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": self.shop + "/src/webhook.py"}}], ts(0, 9), "m1"),
            asst([{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "docker compose up -d", "description": "start containers"}}], ts(0, 9), "m1"),
            tool_result("Error: connection refused", ts(0, 9), is_error=True),
            asst([{"type": "tool_use", "id": "t3", "name": "Edit", "input": {"file_path": self.shop + "/src/webhook.py", "old_string": "a", "new_string": "b"}}], ts(0, 10), "m2", usage=(20, 8, 50)),
        ])
        # session 2: login, 40 days ago, another branch
        write_session(pj, "bbbbbbbb-2222-4000-8000-000000000002", self.shop, "main", [
            user("Implement login with OAuth and sessions", ts(40, 14)),
            asst([{"type": "text", "text": "We will use the authorization code flow."}], ts(40, 14), "m3"),
            {"type": "user", "timestamp": ts(40, 15), "message": {"role": "user", "content": "<command-name>/clear</command-name>"}},
        ])
        # session 3: source folder is gone
        pj2 = self.projects / config.encode_cwd(self.gone_dir)
        write_session(pj2, "cccccccc-3333-4000-8000-000000000003", self.gone_dir, "main", [
            user("Write a blog post about the café SQLite setup", ts(10, 8)),
            asst([{"type": "text", "text": "Draft of the post is ready."}], ts(10, 8), "m4"),
            "corrupted-line-becomes-string",  # written as a JSON string, not a dict
        ])
        # file with a broken line, and a subagent file (must be ignored)
        with (pj2 / "cccccccc-3333-4000-8000-000000000003.jsonl").open("a") as fh:
            fh.write("{broken\n")
        (pj2 / "agent-xyz.jsonl").write_text(json.dumps(user("ignore", ts(0))) + "\n")


class TestParse(Base):
    def test_extract_and_meta(self):
        p = self.projects / config.encode_cwd(self.shop) / "aaaaaaaa-1111-4000-8000-000000000001.jsonl"
        r = parse.parse_file(str(p))
        self.assertEqual(r["meta"]["cwd"], self.shop)
        self.assertEqual(r["meta"]["branch"], "feat/stripe")
        self.assertEqual(r["meta"]["title"], "Fix Stripe webhook")
        self.assertIn(self.shop + "/src/webhook.py", r["files"])
        self.assertEqual(r["tools"]["Bash"], 1)
        # m1 appears in 2 entries: counted once, last usage wins
        self.assertEqual(r["n_msgs"], 1 + 2)
        self.assertEqual(r["tok_out"], 5 + 8)
        kinds = {d[0] for d in r["docs"]}
        self.assertTrue({"prompt", "reply", "bash", "file", "error"} <= kinds)

    def test_system_prompts_and_bad_lines_ignored(self):
        p = self.projects / config.encode_cwd(self.shop) / "bbbbbbbb-2222-4000-8000-000000000002.jsonl"
        r = parse.parse_file(str(p))
        self.assertEqual(r["n_prompts"], 1)  # <command-name> does not count
        p3 = self.projects / config.encode_cwd(self.gone_dir) / "cccccccc-3333-4000-8000-000000000003.jsonl"
        r3 = parse.parse_file(str(p3))
        self.assertGreaterEqual(r3["stats"]["bad"], 2)


class TestIndex(Base):
    def test_counts_and_subagent_skipped(self):
        n = self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(n, 3)
        ids = [r["id"] for r in self.conn.execute("SELECT id FROM sessions")]
        self.assertFalse(any(i.startswith("agent-") for i in ids))

    def test_incremental_and_idempotent(self):
        st = indexer.index_all(self.conn)
        self.assertEqual(st["indexed"], 0)
        self.assertEqual(st["skipped"], 3)
        docs = self.conn.execute("SELECT COUNT(*) FROM doc").fetchone()[0]
        indexer.index_all(self.conn, force=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM doc").fetchone()[0], docs)

    def test_gone_after_transcript_deleted_keeps_history(self):
        p = self.projects / config.encode_cwd(self.shop) / "bbbbbbbb-2222-4000-8000-000000000002.jsonl"
        indexer.backup_all()
        p.unlink()
        st = indexer.index_all(self.conn)
        self.assertEqual(st["gone"], 1)
        r = search.run(self.conn, "OAuth deleted:yes")
        self.assertEqual(r["total"], 1)  # text is still searchable
        self.assertTrue(r["results"][0]["gone"])
        with self.assertRaises(actions.ActionError):
            actions.plan_reopen(db.find_session(self.conn, "bbbbbbbb"))
        indexer.restore(self.conn, "bbbbbbbb")
        self.assertTrue(p.exists())
        self.assertEqual(search.run(self.conn, "OAuth deleted:yes")["total"], 0)

    def test_tags_survive_reindex(self):
        self.conn.execute("UPDATE sessions SET tags='important', starred=1 WHERE id LIKE 'aaaaaaaa%'")
        self.conn.commit()
        indexer.index_all(self.conn, force=True)
        r = self.conn.execute("SELECT tags, starred FROM sessions WHERE id LIKE 'aaaaaaaa%'").fetchone()
        self.assertEqual((r["tags"], r["starred"]), ("important", 1))


class TestSearch(Base):
    def ids(self, q):
        return [r["id"][:8] for r in search.run(self.conn, q)["results"]]

    def test_basic_and_accents(self):
        self.assertEqual(self.ids("webhook stripe"), ["aaaaaaaa"])
        self.assertEqual(set(self.ids("cafe")), {"aaaaaaaa", "cccccccc"})  # "cafe" finds "café"

    def test_phrase_prefix_negation_or(self):
        self.assertEqual(self.ids('"error 500"'), ["aaaaaaaa"])
        self.assertEqual(set(self.ids("webh*")), {"aaaaaaaa"})
        self.assertEqual(set(self.ids("cafe -sqlite")), {"aaaaaaaa"})
        self.assertEqual(set(self.ids("oauth OR sqlite")), {"bbbbbbbb", "cccccccc"})

    def test_scopes(self):
        self.assertEqual(self.ids("docker in:bash"), ["aaaaaaaa"])
        self.assertEqual(self.ids("docker in:prompt"), [])
        self.assertEqual(self.ids("refused in:error"), ["aaaaaaaa"])
        self.assertEqual(self.ids("webhook.py in:file"), ["aaaaaaaa"])

    def test_filters(self):
        self.assertEqual(set(self.ids("project:shop")), {"aaaaaaaa", "bbbbbbbb"})
        self.assertEqual(self.ids("branch:feat/*"), ["aaaaaaaa"])
        self.assertEqual(self.ids("branch:main project:shop"), ["bbbbbbbb"])
        self.assertEqual(self.ids("after:7d"), ["aaaaaaaa"])
        self.assertEqual(set(self.ids("before:30d")), {"bbbbbbbb"})
        self.assertEqual(self.ids("file:webhook.py"), ["aaaaaaaa"])
        self.assertEqual(self.ids("used:bash"), ["aaaaaaaa"])
        self.assertEqual(self.ids("orphan:yes"), ["cccccccc"])
        self.assertEqual(self.ids("msgs:>2"), ["aaaaaaaa"])
        self.assertEqual(set(self.ids("model:opus")), {"aaaaaaaa", "cccccccc", "bbbbbbbb"})
        self.assertEqual(self.ids("webhook after:7d project:shop in:prompt"), ["aaaaaaaa"])

    def test_filter_with_text_combined_and_snippets(self):
        r = search.run(self.conn, "stripe project:shop")
        self.assertEqual(r["total"], 1)
        self.assertTrue(any("«" in s["snip"] for s in r["results"][0]["snippets"]))
        self.assertTrue(r["facets"]["projects"])

    def test_warnings(self):
        self.assertTrue(search.run(self.conn, "after:xyz")["warnings"])
        self.assertTrue(search.run(self.conn, "-docker")["warnings"])
        self.assertTrue(search.run(self.conn, "in:bash")["warnings"])
        # FTS special characters do not break
        r = search.run(self.conn, 'foo" AND (bar')
        self.assertIsInstance(r["results"], list)
        self.assertEqual(search.run(self.conn, "http://example.com")["results"], [])

    def test_tag_and_star_filters(self):
        self.conn.execute("UPDATE sessions SET tags='alpha,beta', starred=1 WHERE id LIKE 'bbbbbbbb%'")
        self.conn.commit()
        self.assertEqual(self.ids("tag:beta"), ["bbbbbbbb"])
        self.assertEqual(self.ids("tag:alph"), [])
        self.assertEqual(self.ids("starred:yes"), ["bbbbbbbb"])

    def test_summary(self):
        s = search.summary(self.conn)
        self.assertEqual(s["sessions"], 3)
        self.assertEqual(s["orphan"], 1)


class TestQueryParser(unittest.TestCase):
    def test_parse_when(self):
        self.assertIsNotNone(query.parse_when("2026-09-01"))
        self.assertIsNotNone(query.parse_when("12h"))
        self.assertIsNotNone(query.parse_when("yesterday"))
        self.assertIsNone(query.parse_when("banana"))

    def test_fts_expression(self):
        q = query.parse_query('a "b c" d* -e OR f')
        self.assertIn('"a"', q.fts)
        self.assertIn('"b c"', q.fts)
        self.assertIn('"d"*', q.fts)
        self.assertIn("NOT", q.fts)
        self.assertIn("OR", q.fts)


class TestActions(Base):
    def test_plan_reopen(self):
        row = db.find_session(self.conn, "aaaaaaaa")
        p = actions.plan_reopen(row)
        self.assertEqual(p["cwd"], self.shop)
        self.assertEqual(p["argv"][:3], ["claude", "--resume", row["id"]])
        self.assertIn("cd ", p["shell"])
        self.assertIn("--bg", actions.plan_reopen(row, bg=True)["argv"])
        self.assertIn("--fork-session", actions.plan_reopen(row, fork=True)["argv"])

    def test_plan_reopen_orphan_warns(self):
        row = db.find_session(self.conn, "cccccccc")
        p = actions.plan_reopen(row)
        self.assertTrue(p["warnings"])
        self.assertEqual(p["cwd"], str(config.home()))

    def test_ambiguous_prefix(self):
        with self.assertRaises(LookupError):
            db.find_session(self.conn, "zzz")


class TestLive(Base):
    def test_live_runs_and_degrades_without_claude(self):
        r = live.live_sessions(self.conn)
        self.assertIn("items", r)
        self.assertIsInstance(r["items"], list)

    def test_is_claude(self):
        self.assertTrue(live._is_claude(["claude", "--resume", "x"]))
        self.assertTrue(live._is_claude(["/usr/local/bin/claude"]))
        self.assertTrue(live._is_claude(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]))
        self.assertTrue(live._is_claude(["/home/u/.local/share/claude/versions/2.1.99"]))
        self.assertFalse(live._is_claude(["vim", "claude.txt"]))
        self.assertFalse(live._is_claude(["python3", "-m", "ovenlight"]))

    def test_internal_claude_processes_are_not_sessions(self):
        for argv in (["/Users/u/.local/bin/claude", "daemon", "run", "--origin", "transient"],
                     ["claude", "bg-pty-host", "--bg-pty-host", "/tmp/cc-daemon-501/x/spare/a.pty.sock", "200", "50"],
                     ["claude", "bg-spare", "--bg-spare", "/tmp/cc-daemon-501/x/spare/a.claim.sock"]):
            self.assertTrue(live._is_claude(argv), argv)
            self.assertTrue(live._is_internal(argv), argv)
        for argv in (["claude", "agents", "--json"], ["claude", "logs", "abc"], ["claude", "--version"], ["claude", "attach", "abc"]):
            self.assertTrue(live._is_internal(argv), argv)
        self.assertFalse(live._is_internal(["claude", "--dangerously-skip-permissions", "-c"]))
        self.assertFalse(live._is_internal(["claude", "--resume", "abc"]))

    def test_send_to_plain_process_is_refused_with_guidance(self):
        with self.assertRaises(live.LiveError) as cm:
            live.send("pid:123", "hi")
        self.assertIn("/bg", str(cm.exception))

    def test_tails_batch(self):
        r = live.tails_for(["sid:aaaaaaaa-1111-4000-8000-000000000001", "pid:999999"], self.conn, n=5)
        self.assertEqual(r["sid:aaaaaaaa-1111-4000-8000-000000000001"]["format"], "messages")
        self.assertLessEqual(len(r["sid:aaaaaaaa-1111-4000-8000-000000000001"]["messages"]), 5)
        self.assertIn("error", r["pid:999999"])

    def test_tail_by_sid(self):
        r = live.tail_for("sid:aaaaaaaa-1111-4000-8000-000000000001", self.conn)
        self.assertEqual(r["format"], "messages")
        self.assertTrue(any(m["kind"] == "prompt" for m in r["messages"]))

    @unittest.skipUnless(shutil.which("tmux"), "tmux missing")
    def test_tmux_managed_roundtrip(self):
        # uses `cat` instead of claude to check sending and capture
        import subprocess
        name = "ovenlight-test1"
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", self.tmp, "cat"], check=True)
        try:
            live.send("tmux:" + name, "hello world\nsecond line")
            time.sleep(0.6)
            out = subprocess.run(["tmux", "capture-pane", "-p", "-t", name], capture_output=True, text=True).stdout
            self.assertIn("hello world", out)
            self.assertIn(name, live.managed_panes())
        finally:
            live.stop("tmux:" + name)


class TestWeb(Base):
    def setUp(self):
        super().setUp()
        from ovenlight import web
        self.web = web
        self.srv = web.make_server(0)
        self.port = self.srv.server_address[1]
        self.token = web.Handler.token
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": host or "127.0.0.1:%d" % self.port}
        h.update(headers or {})
        data = json.dumps(body) if body is not None else None
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        return r.status, raw, r

    def auth(self):
        return {"X-Ovenlight-Token": self.token, "X-Ovenlight": "1"}

    def test_auth_and_host_rules(self):
        self.assertEqual(self.req("GET", "/api/search?q=stripe")[0], 401)
        self.assertEqual(self.req("GET", "/")[0], 401)
        s, _, r = self.req("GET", "/?t=" + self.token)
        self.assertEqual(s, 302)
        self.assertIn("HttpOnly", r.getheader("Set-Cookie"))
        self.assertEqual(self.req("GET", "/api/search?q=x", headers=self.auth(), host="evil.com")[0], 403)
        # POST sem X-Ovenlight ou com Origin externo é recusado
        self.assertEqual(self.req("POST", "/api/index", {}, headers={"X-Ovenlight-Token": self.token})[0], 401)
        self.assertEqual(self.req("POST", "/api/index", {}, headers=dict(self.auth(), Origin="http://evil.com"))[0], 401)

    def test_ui_and_search_and_detail(self):
        s, body, _ = self.req("GET", "/", headers={"Cookie": "ovenlight_t=" + self.token})
        self.assertEqual(s, 200)
        self.assertIn(b"<title>Ovenlight</title>", body)
        self.assertNotIn(b"__BUILD__", body)  # static URLs carry the build hash
        self.assertIn(b"/static/favicon.svg?b=" + self.web.BUILD, body)
        s, body, _ = self.req("GET", "/api/search?q=stripe", headers=self.auth())
        d = json.loads(body)
        self.assertEqual(d["total"], 1)
        sid = d["results"][0]["id"]
        s, body, _ = self.req("GET", "/api/session?id=" + sid[:8], headers=self.auth())
        det = json.loads(body)
        self.assertTrue(det["messages"])
        self.assertTrue(det["files"])
        self.assertEqual(self.req("GET", "/api/stats", headers=self.auth())[0], 200)
        self.assertEqual(self.req("GET", "/api/live", headers=self.auth())[0], 200)

    def test_post_actions(self):
        h = self.auth()
        s, body, _ = self.req("POST", "/api/reopen", {"id": "aaaaaaaa", "mode": "print", "force": True}, headers=h)
        self.assertEqual(s, 200)
        self.assertIn("claude --resume", json.loads(body)["command"])
        s, body, _ = self.req("POST", "/api/tag", {"id": "bbbbbbbb", "tag": "Review"}, headers=h)
        self.assertEqual(json.loads(body)["tags"], ["review"])
        s, body, _ = self.req("POST", "/api/star", {"id": "bbbbbbbb", "on": True}, headers=h)
        self.assertTrue(json.loads(body)["starred"])
        s, body, _ = self.req("POST", "/api/send", {"key": "pid:1", "text": "hi"}, headers=h)
        self.assertEqual(s, 400)
        s, body, _ = self.req("POST", "/api/index", {}, headers=h)
        self.assertEqual(s, 200)


class TestCLI(Base):
    def run_cli(self, *args):
        import io
        from contextlib import redirect_stdout
        from ovenlight import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(args))
        return rc, buf.getvalue()

    def test_commands(self):
        rc, out = self.run_cli("search", "stripe", "project:shop")
        self.assertEqual(rc, 0)
        self.assertIn("aaaaaaaa", out)
        rc, out = self.run_cli("list")
        self.assertIn("3 sessions", out)
        rc, out = self.run_cli("show", "aaaaaaaa")
        self.assertIn("webhook", out)
        rc, out = self.run_cli("open", "aaaaaaaa", "--print")
        self.assertIn("claude --resume aaaaaaaa", out)
        rc, out = self.run_cli("stats")
        self.assertIn("3 sessions", out)
        rc, out = self.run_cli("tag", "aaaaaaaa", "x")
        self.assertIn("x", out)
        rc, out = self.run_cli("hook", "--snippet")
        self.assertIn("SessionEnd", out)
        rc, out = self.run_cli("doctor")
        self.assertEqual(rc, 0)
        rc, out = self.run_cli("search", "--json", "oauth")
        self.assertEqual(json.loads(out)["total"], 1)


class TestRegressions(Base):
    def test_run_never_inherits_stdin(self):
        # `cat` without inherited stdin exits at once (it used to hang until the timeout)
        t0 = time.time()
        rc, out, _ = live._run(["cat"], timeout=5)
        self.assertEqual(rc, 0)
        self.assertLess(time.time() - t0, 2)
        rc, out, _ = live._run(["cat"], timeout=5, input="hi")
        self.assertEqual(out, "hi")

    def test_run_flags_after_folder_and_double_dash(self):
        from unittest import mock
        from ovenlight import cli
        captured = {}

        def fake_run(cwd, claude_args=None):
            captured["cwd"], captured["args"] = cwd, claude_args
            return "ovenlight-test"

        with mock.patch.object(actions, "run_managed", fake_run), mock.patch("os.execvp") as ex:
            rc = cli.main(["run", self.tmp, "--detached", "--", "--model", "opus"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["args"], ["--model", "opus"])
        ex.assert_not_called()  # --detached must not attach tmux
        with mock.patch.object(actions, "run_managed", fake_run), mock.patch("os.execvp") as ex:
            cli.main(["run", self.tmp])
        ex.assert_called_once()
        self.assertEqual(captured["args"], [])


class TestAgentsMerge(Base):
    """Real `claude agents --json` (2.1.317) lists interactive sessions; they must not become `bg:`."""

    REAL = [{"pid": 4242, "cwd": "/w/shop", "kind": "interactive", "startedAt": 1793661205032,
             "sessionId": "aaaaaaaa-1111-4000-8000-000000000001", "name": "shop-b7", "status": "busy"}]

    def _fake(self, payload):
        from unittest import mock
        out = json.dumps(payload)
        return mock.patch.object(live, "_run", lambda cmd, timeout=8.0, input=None: (0, out, "")), \
            mock.patch.object(live.shutil, "which", lambda name: "/usr/bin/" + name)

    def test_interactive_entries_are_not_background(self):
        a, b = self._fake(self.REAL)
        with a, b:
            items, err = live.background_sessions()
        self.assertIsNone(err)
        self.assertEqual(items[0]["kind"], "proc")
        self.assertFalse(items[0]["can_send"])
        self.assertEqual(items[0]["state"], "working")
        self.assertEqual(items[0]["started"], 1793661205.032)

    def test_merge_enriches_process_without_duplicate(self):
        a, b = self._fake(self.REAL)
        with a, b:
            agents, _ = live.background_sessions()
        proc = {"key": "pid:4242", "kind": "proc", "pid": 4242, "cwd": "/w/shop", "session_id": "other",
                "confidence": "low", "state": "idle", "state_estimated": True, "title": "x",
                "can_send": False, "managed": None, "last_activity": None}
        out = live._merge_agents([proc], agents, self.conn)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["key"], "pid:4242")
        self.assertEqual(out[0]["session_id"], "aaaaaaaa-1111-4000-8000-000000000001")
        self.assertEqual(out[0]["state"], "working")
        self.assertFalse(out[0]["state_estimated"])
        self.assertFalse(out[0]["can_send"])

    def test_browser_terminal_is_merged_not_duplicated(self):
        a, b = self._fake(self.REAL)
        with a, b:
            agents, _ = live.background_sessions()
        proc = {"key": "term:1111", "kind": "embedded", "pid": 4242, "cwd": "/w/shop", "session_id": None,
                "confidence": "none", "state": "unknown", "state_estimated": True, "title": "x",
                "can_send": True, "managed": None, "last_activity": None}
        out = live._merge_agents([proc], agents, self.conn)
        self.assertEqual([i["key"] for i in out], ["term:1111"])
        self.assertTrue(out[0]["can_send"])
        self.assertEqual(out[0]["session_id"], "aaaaaaaa-1111-4000-8000-000000000001")
        self.assertEqual(out[0]["state"], "working")

    def test_background_entry_hides_process_line(self):
        bg = [dict(self.REAL[0], kind="background", status="idle", name="aaaaaaaa")]
        a, b = self._fake(bg)
        with a, b:
            agents, _ = live.background_sessions()
        proc = {"key": "pid:4242", "kind": "proc", "pid": 4242, "cwd": "/w/shop", "session_id": None,
                "can_send": False, "managed": None, "last_activity": 123.0, "transcript": "/t.jsonl"}
        out = live._merge_agents([proc], agents, self.conn)
        self.assertEqual([i["key"] for i in out], ["bg:aaaaaaaa-1111-4000-8000-000000000001"])
        self.assertTrue(out[0]["can_send"])
        self.assertEqual(out[0]["last_activity"], 123.0)
        # the agent-view name ("aaaaaaaa", the short id) gives way to the conversation title
        self.assertEqual(out[0]["title"], "Fix Stripe webhook")

    def test_background_job_id_vs_session_id(self):
        # real 2.1.317 shape: "id" is the job (for logs/stop), "sessionId" the conversation
        real_bg = [{"pid": 87934, "id": "aaaaaaaa", "cwd": self.shop, "kind": "background", "startedAt": 1793710989566,
                    "sessionId": "aaaaaaaa-1111-4000-8000-000000000001", "name": "aaaaaaaa", "status": "idle", "state": "blocked"}]
        a, b = self._fake(real_bg)
        with a, b:
            agents, _ = live.background_sessions()
        it = agents[0]
        self.assertEqual(it["key"], "bg:aaaaaaaa")
        self.assertEqual(it["session_id"], "aaaaaaaa-1111-4000-8000-000000000001")
        self.assertEqual(it["state"], "idle (blocked)")
        out = live._merge_agents([], agents, self.conn)
        self.assertEqual(out[0]["title"], "Fix Stripe webhook")
        self.assertTrue(out[0]["transcript"].endswith("aaaaaaaa-1111-4000-8000-000000000001.jsonl"))

    def test_strip_ansi_tui_output(self):
        raw = "\x1b[38;2;255;255;255m\u23fa\x1b[3G\x1b[39mCriei\x1b[9Gas\x1b[12Gduas\x1b[2Bok"
        self.assertEqual(live._strip_ansi(raw), "\u23fa Criei as duas\nok")

    def test_message_plain_terminal_chat_via_courier(self):
        from unittest import mock
        seen = {}

        def fake_run(cmd, timeout=8.0, input=None, env=None, cwd=None):
            seen["cmd"], seen["env"] = cmd, env
            return 0, json.dumps({"result": "DELIVERED", "total_cost_usd": 0.0067}), ""

        items = [{"key": "pid:4242", "kind": "proc", "agent_name": "shop-b7", "can_send": False}]
        with mock.patch.object(live, "live_sessions", lambda conn=None: {"items": items}), \
                mock.patch.object(live, "_run", fake_run), mock.patch.object(live.shutil, "which", lambda n: "/x/claude"):
            msg = live.send("pid:4242", "run the tests")
        self.assertIn("Delivered to shop-b7", msg)
        cmd = seen["cmd"]
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--allowedTools=SendMessage,ListAgents,ToolSearch", cmd)
        self.assertIn('"shop-b7"', cmd[-1])
        self.assertIn("run the tests", cmd[-1])
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", seen["env"])

    def test_skip_permissions_detection_and_held_notice(self):
        from unittest import mock
        self.assertTrue(live.skips_permissions(["claude", "--dangerously-skip-permissions", "-c"]))
        self.assertTrue(live.skips_permissions(["claude", "--permission-mode", "bypassPermissions"]))
        self.assertTrue(live.skips_permissions(["claude", "--permission-mode=bypassPermissions"]))
        self.assertFalse(live.skips_permissions(["claude", "--permission-mode", "plan"]))
        self.assertFalse(live.skips_permissions(["claude"]))
        items = [{"key": "pid:4242", "kind": "proc", "agent_name": "shop-b7", "can_send": False, "skip_perms": True}]
        with mock.patch.object(live, "live_sessions", lambda conn=None: {"items": items}), \
                mock.patch.object(live, "_run", lambda *a, **k: (0, json.dumps({"result": "DELIVERED"}), "")), \
                mock.patch.object(live.shutil, "which", lambda n: "/x/claude"):
            msg = live.send("pid:4242", "hi")
        self.assertIn("approve it in that session's terminal", msg)

    def test_courier_failure_is_reported(self):
        from unittest import mock
        items = [{"key": "pid:4242", "kind": "proc", "agent_name": "shop-b7", "can_send": False}]
        with mock.patch.object(live, "live_sessions", lambda conn=None: {"items": items}), \
                mock.patch.object(live, "_run", lambda *a, **k: (0, json.dumps({"result": "FAILED: no such session"}), "")), \
                mock.patch.object(live.shutil, "which", lambda n: "/x/claude"):
            with self.assertRaises(live.LiveError) as cm:
                live.send("pid:4242", "hi")
        self.assertIn("no such session", str(cm.exception))

    def test_send_bg_refused_when_session_is_interactive(self):
        a, b = self._fake(self.REAL)
        with a, b:
            with self.assertRaises(live.LiveError) as cm:
                live.send("bg:aaaaaaaa", "hi")
        self.assertIn("/bg", str(cm.exception))


class TestPermissions(Base):
    def test_private_files_and_dirs(self):
        home = config.ovenlight_dir()
        self.assertEqual(os.stat(home).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(config.db_path()).st_mode & 0o777, 0o600)
        indexer.backup_all()
        files = list(config.backup_dir().rglob("*.jsonl"))
        self.assertTrue(files)
        for f in files:
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        d = files[0].parent
        while d != home:
            self.assertEqual(d.stat().st_mode & 0o777, 0o700, d)
            d = d.parent

    def test_existing_open_dir_is_tightened(self):
        os.chmod(config.ovenlight_dir(), 0o755)
        self.assertEqual(os.stat(config.ovenlight_dir()).st_mode & 0o777, 0o700)


class TestShellAndTerminal(Base):
    """`open --shell` and the embedded browser terminal (pty over WebSocket)."""

    def test_plan_shell_and_cli_print(self):
        row = db.find_session(self.conn, "aaaaaaaa")
        p = actions.plan_shell(row)
        self.assertEqual(p["cwd"], self.shop)
        self.assertEqual(p["shell"], "cd " + self.shop + " && claude")
        self.assertEqual(actions.plan_shell(row, claude=False)["shell"], "cd " + self.shop)
        orphan = actions.plan_shell(db.find_session(self.conn, "cccccccc"))
        self.assertEqual(orphan["cwd"], str(config.home()))
        self.assertTrue(orphan["warnings"])
        import io
        from contextlib import redirect_stdout
        from ovenlight import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["open", "aaaaaaaa", "--shell", "--print"])
            cli.main(["open", "aaaaaaaa", "--shell", "--no-claude", "--print"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().split("\n")[:2], ["cd " + self.shop + " && claude", "cd " + self.shop])

    def test_send_to_browser_terminal(self):
        # live.send("term:<pid>") types into a pty Ovenlight owns: bracketed paste + Enter
        import pty, threading as th
        from ovenlight import term
        master, slave = pty.openpty()
        term._masters[4242], term._write_locks[4242] = master, th.Lock()
        try:
            msg = live.send("term:4242", "two\nlines")
            self.assertIn("browser terminal", msg)
            import select
            data = b""
            while select.select([slave], [], [], 0.5)[0]:
                data += os.read(slave, 4096)
            self.assertIn(b"\x1b[200~two", data)
            self.assertIn(b"\x1b[201~", data)
        finally:
            term._masters.pop(4242, None); term._write_locks.pop(4242, None)
            os.close(master); os.close(slave)
        with self.assertRaises(live.LiveError):
            live.send("term:999999", "hi")

    def test_ws_frame_roundtrip(self):
        from ovenlight import term
        import io
        payload = json.dumps({"t": "i", "d": "olá"}).encode()
        frame = term.encode_frame(payload, opcode=1, mask=b"\x01\x02\x03\x04")
        ws = term.WS(io.BytesIO(frame), io.BytesIO())
        self.assertEqual(ws.recv(), (1, payload))
        # unmasked client frames are refused (RFC 6455)
        ws2 = term.WS(io.BytesIO(term.encode_frame(b"x", opcode=1)), io.BytesIO())
        self.assertIsNone(ws2.recv())
        self.assertEqual(term.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


class TestWebTerminal(Base):
    def setUp(self):
        super().setUp()
        from ovenlight import web
        self.web = web
        self.srv = web.make_server(0)
        self.port = self.srv.server_address[1]
        self.token = web.Handler.token
        web.Handler.terminal = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        from ovenlight import term
        term.kill_all()
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def handshake(self, query, origin="self", token=True):
        import base64
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        org = "http://127.0.0.1:%d" % self.port if origin == "self" else origin
        lines = ["GET /api/term?%s HTTP/1.1" % query, "Host: 127.0.0.1:%d" % self.port,
                 "Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Key: " + base64.b64encode(os.urandom(16)).decode(), "Sec-WebSocket-Version: 13"]
        if org:
            lines.append("Origin: " + org)
        if token:
            lines.append("Cookie: ovenlight_t=" + self.token)
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(1)
            if not chunk:
                break
            head += chunk
        return s, int(head.split(b" ")[1])

    def read_until(self, s, needle, timeout=10):
        from ovenlight import term
        import io
        s.settimeout(timeout)
        buf, out = b"", b""
        deadline = time.time() + timeout
        while needle not in out and time.time() < deadline:
            try:
                buf += s.recv(65536)
            except OSError:
                break
            while len(buf) >= 2:
                ln = buf[1] & 0x7F
                hl = 2 + (2 if ln == 126 else 8 if ln == 127 else 0)
                if len(buf) < hl:
                    break
                if ln == 126:
                    ln = int.from_bytes(buf[2:4], "big")
                elif ln == 127:
                    ln = int.from_bytes(buf[2:10], "big")
                if len(buf) < hl + ln:
                    break
                out += buf[hl:hl + ln]
                buf = buf[hl + ln:]
        return out

    def test_shell_roundtrip_and_exit(self):
        from ovenlight import term
        os.environ["SHELL"] = "/bin/sh"
        s, status = self.handshake("id=aaaaaaaa&mode=bare&cols=100&rows=30")
        self.assertEqual(status, 101)
        msg = json.dumps({"t": "i", "d": "pwd; echo ovenlight-$((40+2))\n"}).encode()
        s.sendall(term.encode_frame(msg, opcode=1, mask=os.urandom(4)))
        out = self.read_until(s, b"ovenlight-42")
        self.assertIn(b"ovenlight-42", out)
        self.assertIn(os.path.realpath(self.shop).encode(), out.replace(b"\r\n", b""))
        s.sendall(term.encode_frame(json.dumps({"t": "i", "d": "exit 3\n"}).encode(), opcode=1, mask=os.urandom(4)))
        out = self.read_until(s, b'"exit"')
        self.assertIn(b'"code": 3', out)
        s.close()
        log = (Path(os.environ["OVENLIGHT_HOME"]) / "audit.log").read_text()
        self.assertIn('"event": "terminal"', log)

    def test_parent_session_markers_are_not_inherited(self):
        from ovenlight import term
        os.environ.update({"SHELL": "/bin/sh", "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
                           "CLAUDE_CODE_USE_BEDROCK": "1"})
        s, status = self.handshake("id=aaaaaaaa&mode=bare")
        self.assertEqual(status, 101)
        cmd = 'echo "child=[${CLAUDE_CODE_CHILD_SESSION}] tok=[${CLAUDE_CODE_MESSAGING_TOKEN}] bedrock=[${CLAUDE_CODE_USE_BEDROCK}]"\n'
        s.sendall(term.encode_frame(json.dumps({"t": "i", "d": cmd}).encode(), opcode=1, mask=os.urandom(4)))
        out = self.read_until(s, b"bedrock=[1]")
        s.close()
        self.assertIn(b"child=[] tok=[] bedrock=[1]", out)

    def test_terminal_security(self):
        # no token
        s, st = self.handshake("id=aaaaaaaa&mode=shell", token=False); s.close()
        self.assertEqual(st, 401)
        # no Origin, foreign Origin, and another app on the same host but a different port
        for org in (None, "http://evil.com", "http://127.0.0.1:3000"):
            s, st = self.handshake("id=aaaaaaaa&mode=shell", origin=org); s.close()
            self.assertEqual(st, 403, org)
        # arbitrary programs are not accepted
        s, st = self.handshake("id=aaaaaaaa&mode=bash%20-c%20id"); s.close()
        self.assertEqual(st, 400)
        # deleted transcript cannot be resumed
        self.conn.execute("UPDATE sessions SET gone=1 WHERE id LIKE 'bbbbbbbb%'"); self.conn.commit()
        s, st = self.handshake("id=bbbbbbbb&mode=claude"); s.close()
        self.assertEqual(st, 410)
        # disabled with --no-terminal
        self.web.Handler.terminal = False
        s, st = self.handshake("id=aaaaaaaa&mode=shell"); s.close()
        self.assertEqual(st, 403)

    def test_static_and_config_and_post_origin_port(self):
        h = {"X-Ovenlight-Token": self.token, "X-Ovenlight": "1"}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/static/xterm.js", headers=dict(h, Host="127.0.0.1:%d" % self.port))
        r = c.getresponse(); body = r.read()
        self.assertEqual(r.status, 200)
        self.assertIn(b"Terminal", body)
        c.request("GET", "/static/../web.py", headers=dict(h, Host="127.0.0.1:%d" % self.port))
        r = c.getresponse(); r.read()
        self.assertEqual(r.status, 404)
        c.request("GET", "/api/config", headers=dict(h, Host="127.0.0.1:%d" % self.port))
        r = c.getresponse()
        self.assertTrue(json.loads(r.read())["terminal"])
        # POST from another local port is refused (cookies are not port-scoped)
        c.request("POST", "/api/index", body="{}", headers=dict(h, Host="127.0.0.1:%d" % self.port, Origin="http://localhost:3000"))
        r = c.getresponse(); r.read()
        self.assertEqual(r.status, 401)
        c.request("POST", "/api/index", body="{}", headers=dict(h, Host="127.0.0.1:%d" % self.port, Origin="http://localhost:%d" % self.port))
        r = c.getresponse(); r.read()
        self.assertEqual(r.status, 200)

    def test_ask_fork_thread_continues_same_copy(self):
        from unittest import mock
        h = {"X-Ovenlight-Token": self.token, "X-Ovenlight": "1", "Host": "127.0.0.1:%d" % self.port}
        calls = []

        def fake_ask(sid, text, fork=True, timeout=300.0, cwd=None):
            calls.append((sid, fork, cwd))
            return {"answer": "ok " + text, "session_id": "fork-1" if fork else sid}

        def post(body):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("POST", "/api/ask", body=json.dumps(body), headers=h)
            r = c.getresponse()
            return r.status, json.loads(r.read())

        with mock.patch.object(live, "ask_headless", fake_ask):
            st, d = post({"id": "aaaaaaaa", "text": "first"})
            self.assertEqual((st, d["session_id"]), (200, "fork-1"))
            st, d = post({"continue_fork": "fork-1", "text": "second"})
            self.assertEqual(st, 200)
            st, _ = post({"continue_fork": "not-ours", "text": "x"})
            self.assertEqual(st, 400)
        self.assertEqual(calls[0][:2], ("aaaaaaaa-1111-4000-8000-000000000001", True))
        self.assertEqual(calls[0][2], self.shop)          # runs in the session folder
        self.assertEqual(calls[1][:2], ("fork-1", False))  # follow-up resumes the same fork

    def test_reopen_shell_mode_skips_conflicts(self):
        from unittest import mock
        h = {"X-Ovenlight-Token": self.token, "X-Ovenlight": "1", "Host": "127.0.0.1:%d" % self.port}
        with mock.patch.object(actions, "open_terminal", lambda cmd: (True, "opened: " + cmd)) as _:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("POST", "/api/reopen", body=json.dumps({"id": "aaaaaaaa", "mode": "shell"}), headers=h)
            r = c.getresponse(); d = json.loads(r.read())
        self.assertEqual(r.status, 200)
        self.assertEqual(d["command"], "cd " + self.shop + " && claude")
        self.assertTrue(d["opened"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

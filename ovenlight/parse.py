"""Tolerant reader for Claude Code JSONL transcripts.

The format is internal to Claude Code and changes between versions, so everything here uses
.get() and ignores what it does not recognize (counting what was skipped for `ovenlight doctor`).
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead"}
MAX_DOC = 20000
SYSTEM_PREFIXES = ("<command-", "<local-command", "Caveat:", "<system-reminder", "<user-prompt-submit-hook")


def to_epoch(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        return v / 1000.0 if v > 1e12 else v
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _blocks(content: Any) -> Iterable[dict]:
    if isinstance(content, str):
        yield {"type": "text", "text": content}
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                yield {"type": "text", "text": b}
            elif isinstance(b, dict):
                yield b


def _result_text(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(str(b.get("text", "")))
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(out)
    return ""


def _compact_input(inp: Any) -> str:
    if not isinstance(inp, dict):
        return str(inp)[:300]
    for k in ("command", "pattern", "query", "url", "description", "prompt"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v[:300]
    try:
        return json.dumps(inp, ensure_ascii=False)[:300]
    except Exception:
        return ""


def extract(e: Any, include_results: bool = False) -> List[dict]:
    """Convert a transcript entry into items {kind, role, text, ts, file?, tool?}.

    kinds: prompt, reply, bash, file, tool, error, output.
    """
    out: List[dict] = []
    if not isinstance(e, dict) or e.get("type") not in ("user", "assistant"):
        return out
    if e.get("isSidechain") or e.get("isMeta"):
        return out
    msg = e.get("message")
    if not isinstance(msg, dict):
        return out
    ts = to_epoch(e.get("timestamp"))
    role = msg.get("role") or e.get("type")
    for b in _blocks(msg.get("content")):
        bt = b.get("type")
        if bt == "text":
            text = str(b.get("text") or "").strip()
            if not text:
                continue
            if role == "user":
                if text.startswith(SYSTEM_PREFIXES):
                    continue
                out.append({"kind": "prompt", "role": "user", "text": text, "ts": ts})
            else:
                out.append({"kind": "reply", "role": "assistant", "text": text, "ts": ts})
        elif bt == "tool_use":
            name = str(b.get("name") or "?")
            inp = b.get("input") or {}
            item = {"role": "assistant", "ts": ts, "tool": name}
            if name == "Bash" and isinstance(inp, dict):
                d, c = inp.get("description"), inp.get("command", "")
                item.update(kind="bash", text=(f"{d}\n{c}" if d else str(c)))
            elif name in FILE_TOOLS and isinstance(inp, dict) and (inp.get("file_path") or inp.get("notebook_path")):
                p = str(inp.get("file_path") or inp.get("notebook_path"))
                item.update(kind="file", text=f"{name} {p}", file=p)
            else:
                item.update(kind="tool", text=f"{name} {_compact_input(inp)}")
            out.append(item)
        elif bt == "tool_result":
            txt = _result_text(b.get("content"))
            if b.get("is_error"):
                out.append({"kind": "error", "role": "tool", "text": txt[:1500], "ts": ts})
            elif include_results and txt.strip():
                out.append({"kind": "output", "role": "tool", "text": txt[:800], "ts": ts})
    return out


def _iter_entries(path: str, stats: Optional[dict] = None):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if stats is not None:
                stats["lines"] += 1
            try:
                e = json.loads(line)
            except ValueError:
                if stats is not None:
                    stats["bad"] += 1
                continue
            if not isinstance(e, dict):
                if stats is not None:
                    stats["bad"] += 1
                continue
            yield e


def parse_file(path: str, include_results: bool = False) -> dict:
    meta: Dict[str, Any] = {
        "cwd": None, "branch": None, "version": None, "model": None,
        "title": None, "custom_title": None, "first_prompt": None,
        "started": None, "ended": None,
    }
    docs: List[tuple] = []
    files = set()
    tools: Counter = Counter()
    usage: Dict[str, tuple] = {}
    msg_ids = set()
    n_prompts = 0
    stats = {"lines": 0, "bad": 0, "types": Counter()}

    for e in _iter_entries(path, stats):
        t = e.get("type")
        stats["types"][str(t)] += 1
        ts = to_epoch(e.get("timestamp"))
        if ts is not None:
            if meta["started"] is None or ts < meta["started"]:
                meta["started"] = ts
            if meta["ended"] is None or ts > meta["ended"]:
                meta["ended"] = ts
        if not e.get("isSidechain"):
            if e.get("cwd"):
                meta["cwd"] = e["cwd"]
            if e.get("gitBranch"):
                meta["branch"] = e["gitBranch"]
            if e.get("version"):
                meta["version"] = e["version"]
        if t in ("summary", "custom-title", "ai-title"):
            v = e.get("customTitle") or e.get("aiTitle") or e.get("summary") or e.get("title")
            if v:
                if t == "custom-title" or e.get("customTitle"):
                    meta["custom_title"] = str(v)
                else:
                    meta["title"] = str(v)
            continue
        if t == "assistant" and not e.get("isSidechain") and isinstance(e.get("message"), dict):
            msg = e["message"]
            mid = msg.get("id") or e.get("uuid") or "anon%d" % len(msg_ids)
            msg_ids.add(mid)
            model = msg.get("model")
            if isinstance(model, str) and model and not model.startswith("<"):
                meta["model"] = model
            u = msg.get("usage")
            if isinstance(u, dict):
                try:
                    usage[mid] = (
                        int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0),
                        int(u.get("output_tokens") or 0),
                        int(u.get("cache_read_input_tokens") or 0),
                    )
                except (TypeError, ValueError):
                    pass
        for it in extract(e, include_results):
            if it["kind"] == "prompt":
                n_prompts += 1
                if meta["first_prompt"] is None:
                    meta["first_prompt"] = " ".join(it["text"].split())[:200]
            if it.get("file"):
                files.add(it["file"])
            if it.get("tool"):
                tools[it["tool"]] += 1
            docs.append((it["kind"], it["ts"], it["text"][:MAX_DOC]))

    tok_in = sum(v[0] for v in usage.values())
    tok_out = sum(v[1] for v in usage.values())
    tok_cache = sum(v[2] for v in usage.values())
    return {
        "meta": meta, "docs": docs, "files": files, "tools": tools, "stats": stats,
        "n_msgs": n_prompts + len(msg_ids), "n_prompts": n_prompts,
        "tok_in": tok_in, "tok_out": tok_out, "tok_cache": tok_cache,
    }


def quick_info(path: str, max_lines: int = 400) -> dict:
    """Quick read (first lines) for sessions not indexed yet."""
    info = {"cwd": None, "branch": None, "first_prompt": None, "title": None}
    try:
        for i, e in enumerate(_iter_entries(path)):
            if i > max_lines:
                break
            if e.get("cwd"):
                info["cwd"] = e["cwd"]
            if e.get("gitBranch"):
                info["branch"] = e["gitBranch"]
            if e.get("type") in ("custom-title", "ai-title", "summary"):
                v = e.get("customTitle") or e.get("aiTitle") or e.get("summary") or e.get("title")
                if v and (e.get("type") == "custom-title" or not info["title"]):
                    info["title"] = str(v)
            for it in extract(e):
                if it["kind"] == "prompt" and not info["first_prompt"]:
                    info["first_prompt"] = " ".join(it["text"].split())[:200]
            if info["cwd"] and info["first_prompt"] and info["title"]:
                break
    except OSError:
        pass
    return info


def read_messages(path: str, include_results: bool = True, max_bytes: Optional[int] = None) -> List[dict]:
    """Messages of a session for display. With max_bytes, reads only the end of the file."""
    items: List[dict] = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if max_bytes and size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial line
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return items
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        items.extend(extract(e, include_results))
    return items

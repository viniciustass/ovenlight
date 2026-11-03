"""Embedded terminal: a pty bridged to the browser over a minimal WebSocket (standard library only).

Only fixed programs can run here, never a command sent by the browser:
  - "claude": `claude --resume <session id>` in the session folder
  - "shell":  the user's login shell, which starts a new `claude` right away; exiting claude leaves you in the shell
  - "bare":   just the login shell

The web handler enforces token, Host and Origin before calling `serve_ws`.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import itertools
import json
import os
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
from typing import Dict, List, Optional

from . import config

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 1 << 20  # 1 MB; input frames are keystrokes and pastes

# A tiny launcher: new session + controlling tty, then exec. Done in a separate
# interpreter so we never fork with preexec_fn from the multithreaded server.
_LAUNCHER = ("import os,sys,fcntl,termios;os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0);"
             "os.execvp(sys.argv[1],sys.argv[1:])")

_active: Dict[int, subprocess.Popen] = {}
_masters: Dict[int, int] = {}  # launcher pid -> pty master fd
_write_locks: Dict[int, threading.Lock] = {}
_active_lock = threading.Lock()


class WSClosed(Exception):
    pass


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def encode_frame(payload: bytes, opcode: int = 2, mask: Optional[bytes] = None) -> bytes:
    n = len(payload)
    head = bytearray([0x80 | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += struct.pack(">H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack(">Q", n)
    if mask:
        head += mask
        payload = bytes(a ^ b for a, b in zip(payload, itertools.cycle(mask)))
    return bytes(head) + payload


class WS:
    """Server side of RFC 6455, just what the terminal needs."""

    def __init__(self, rfile, wfile, sock=None):
        self.r, self.w, self.sock = rfile, wfile, sock
        self.lock = threading.Lock()
        self.closed = False

    def _read(self, n: int) -> bytes:
        data = self.r.read(n)
        if not data or len(data) < n:
            raise WSClosed()
        return data

    def recv(self) -> Optional[tuple]:
        """Return (opcode, payload) for a data message, or None when the peer closes."""
        buf, first_op = b"", None
        while True:
            b0, b1 = self._read(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            masked, ln = b1 & 0x80, b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            if ln > MAX_FRAME or not masked:  # clients must mask (RFC 6455 5.1)
                self.close(1009 if ln > MAX_FRAME else 1002)
                return None
            mask = self._read(4)
            payload = bytes(a ^ b for a, b in zip(self._read(ln), itertools.cycle(mask)))
            if op == 8:
                self.close()
                return None
            if op == 9:
                self.send(payload, opcode=10)
                continue
            if op == 10:
                continue
            if op != 0:
                first_op = op
            buf += payload
            if len(buf) > MAX_FRAME:
                self.close(1009)
                return None
            if fin:
                return first_op, buf

    def send(self, payload: bytes, opcode: int = 2) -> None:
        if self.closed:
            raise WSClosed()
        frame = encode_frame(payload, opcode)
        with self.lock:
            try:
                self.w.write(frame)
                self.w.flush()
            except OSError:
                self.closed = True
                raise WSClosed()

    def send_json(self, obj) -> None:
        self.send(json.dumps(obj).encode(), opcode=1)

    def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        try:
            self.send(struct.pack(">H", code), opcode=8)
        except WSClosed:
            pass
        self.closed = True

    def abort(self) -> None:
        """Drop the TCP connection (unblocks a pending recv)."""
        self.closed = True
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _set_size(fd: int, cols: int, rows: int) -> None:
    cols = max(2, min(500, int(cols or 80)))
    rows = max(2, min(300, int(rows or 24)))
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _write_all(fd: int, data: bytes) -> None:
    while data:
        n = os.write(fd, data)
        data = data[n:]


def program(mode: str, session_id: str) -> List[str]:
    if mode == "claude":
        return ["claude", "--resume", session_id]
    sh = os.environ.get("SHELL") or "/bin/bash"
    if mode == "shell":
        return [sh, "-l", "-c", 'claude; exec "${SHELL:-/bin/bash}" -l']
    if mode == "bare":
        return [sh, "-l"]
    raise ValueError("unknown terminal mode: %r" % mode)


def serve_ws(ws: WS, argv: List[str], cwd: str, cols: int = 80, rows: int = 24) -> int:
    """Run argv in a pty and pump bytes both ways until either side ends. Returns the exit code."""
    master, slave = pty.openpty()
    _set_size(master, cols, rows)
    env = config.clean_env({"TERM": "xterm-256color", "COLORTERM": "truecolor", "OVENLIGHT_TERMINAL_EMBEDDED": "1"})
    try:
        proc = subprocess.Popen([sys.executable, "-I", "-c", _LAUNCHER] + argv, stdin=slave, stdout=slave,
                                stderr=slave, cwd=cwd, env=env, close_fds=True)
    except OSError as exc:
        os.close(master)
        os.close(slave)
        ws.send(("\r\n\x1b[31mcould not start %s: %s\x1b[0m\r\n" % (argv[0], exc)).encode())
        ws.send_json({"t": "exit", "code": 127})
        ws.close()
        return 127
    os.close(slave)
    with _active_lock:
        _active[proc.pid] = proc
        _masters[proc.pid] = master
        _write_locks[proc.pid] = threading.Lock()

    def pump_out() -> None:
        try:
            while True:
                r, _, _ = select.select([master], [], [], 0.5)
                if not r:
                    if proc.poll() is not None:
                        break
                    continue
                try:
                    data = os.read(master, 65536)
                except OSError:  # EIO: the child closed the tty
                    break
                if not data:
                    break
                ws.send(data)
        except WSClosed:
            pass
        finally:
            code = proc.wait()
            try:
                ws.send_json({"t": "exit", "code": code})
            except WSClosed:
                pass
            ws.close()
            threading.Timer(2.0, ws.abort).start()  # if the browser never answers the close

    reader = threading.Thread(target=pump_out, daemon=True)
    reader.start()
    try:
        while not ws.closed:
            msg = ws.recv()
            if msg is None:
                break
            op, payload = msg
            if op == 2:
                _write_all(master, payload)
                continue
            try:
                m = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                continue
            if m.get("t") == "i" and isinstance(m.get("d"), str):
                _write_all(master, m["d"].encode("utf-8"))
            elif m.get("t") == "r":
                _set_size(master, m.get("c"), m.get("r"))
    except (WSClosed, OSError):
        pass
    finally:
        _terminate(proc)
        reader.join(timeout=3)
        try:
            os.close(master)
        except OSError:
            pass
        with _active_lock:
            _active.pop(proc.pid, None)
            _masters.pop(proc.pid, None)
            _write_locks.pop(proc.pid, None)
    return proc.returncode if proc.returncode is not None else -1


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGHUP, 2.0), (signal.SIGTERM, 1.0), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(proc.pid, sig)  # the launcher made the child a session (and group) leader
        except ProcessLookupError:
            try:
                proc.send_signal(sig)  # setsid may not have run yet
            except ProcessLookupError:
                return
        except PermissionError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def embedded_pids() -> List[int]:
    """Launcher pids of terminals currently open in the browser (this server process only)."""
    with _active_lock:
        return [pid for pid, p in _active.items() if p.poll() is None]


def send_to(pid: int, text: str) -> None:
    """Type a prompt into a browser terminal: bracketed paste (keeps newlines) + Enter."""
    with _active_lock:
        fd, lock = _masters.get(pid), _write_locks.get(pid)
    if fd is None or lock is None:
        raise KeyError(pid)
    with lock:
        _write_all(fd, b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~")
        import time
        time.sleep(0.15)
        _write_all(fd, b"\r")


def kill_all() -> None:
    with _active_lock:
        procs = list(_active.values())
    for p in procs:
        _terminate(p)

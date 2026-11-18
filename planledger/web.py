"""Local dashboard server (stdlib only).

    python -m planledger.web [--port 8765]

Reading: every figure on the page comes from the committed database (opened read-only).
Writing: the "New input" box saves the pasted text as a file under <data dir>/inbox and runs the very
same ingest function that `python -m planledger <file>` runs, so the first paste is the pack and every later
paste is an update, exactly like the command line. "Clear all data" removes the database files and nothing else.
The page polls /api/version and reloads state when the committed data changes.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import chat, export, pipeline
from .config import Config
from .store import Store
from .viewmodel import build_state, fingerprint, trace_item

STATIC = Path(__file__).parent / "static"
MAX_BODY = 8 * 1024
MAX_INPUT = 600 * 1024
JOB = {"id": 0, "running": False, "lines": [], "code": None, "file": None}
JOB_LOCK = threading.Lock()
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def _empty_state(cfg, why):
    return {"empty": True, "active": None, "runs": [], "revisions": [], "statements": [], "messages": [], "trace": [],
            "totals": {"calls": 0, "input": 0, "output": 0, "total": 0, "partial": False, "missing_usage": 0},
            "sources": [], "running": [], "provider_problem": cfg.provider_problem(), "model": cfg.model,
            "routing_guide": "", "fingerprint": "none", "generated": None, "note": why}


def open_store(cfg):
    if not (cfg.data_dir / "state.db").exists():
        return None
    try:
        return Store(cfg.data_dir, readonly=True)
    except Exception:
        return None


def _start_job(cfg, text):
    with JOB_LOCK:
        if JOB["running"]:
            return None
        inbox = cfg.data_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        n = len(list(inbox.glob("input_*.txt"))) + 1
        path = inbox / f"input_{n:03d}.txt"
        path.write_text(text, encoding="utf-8")
        JOB.update(id=JOB["id"] + 1, running=True, lines=[], code=None, file=path.name)
        jid = JOB["id"]

    def work():
        def out(line):
            with JOB_LOCK:
                JOB["lines"].append(str(line))
                del JOB["lines"][:-4000]
        code = 1
        try:
            code = pipeline.run_ingest(str(path), cfg, out=out)
        except Exception as e:  # report, never crash the server
            out(f"ERROR: {type(e).__name__}: {e}")
        finally:
            with JOB_LOCK:
                JOB.update(running=False, code=code)
    threading.Thread(target=work, daemon=True).start()
    return jid


def _reset(cfg):
    with JOB_LOCK:
        if JOB["running"]:
            return False
        JOB.update(lines=[], code=None, file=None)
    d = cfg.data_dir
    for name in ("state.db", "state.db-wal", "state.db-shm", "state.db-journal"):
        try:
            (d / name).unlink()
        except FileNotFoundError:
            pass
    for sub in ("inbox", "exports"):
        base = d / sub
        if base.is_dir():
            for f in base.rglob("*"):
                if f.is_file():
                    f.unlink()
            for f in sorted(base.rglob("*"), reverse=True):
                if f.is_dir():
                    f.rmdir()
    return True


def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PlanLedgerUI"

        def log_message(self, *a):
            pass

        def _host_ok(self):
            h = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") else "[::1]"
            return h in LOCAL_HOSTS

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _state(self):
            store = open_store(cfg)
            if store is None:
                return _empty_state(cfg, "No data directory yet. Run: python -m planledger <file>"), None
            return build_state(store, cfg), store

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "local access only"})
            u = urlparse(self.path)
            p = u.path
            try:
                if p == "/api/version":
                    store = open_store(cfg)
                    fp = fingerprint(store) if store else "none"
                    if store:
                        store.close()
                    return self._send(200, {"fingerprint": fp})
                if p == "/api/job":
                    with JOB_LOCK:
                        return self._send(200, dict(JOB, lines=list(JOB["lines"])))
                if p == "/api/state":
                    st, store = self._state()
                    if store:
                        store.close()
                    return self._send(200, st)
                if p == "/api/trace_item":
                    seq = int((parse_qs(u.query).get("seq") or ["0"])[0])
                    store = open_store(cfg)
                    item = trace_item(store, seq) if store else None
                    if store:
                        store.close()
                    return self._send(200 if item else 404, item or {"error": "not found"})
                if p == "/api/source":
                    sid = (parse_qs(u.query).get("id") or [""])[0]
                    store = open_store(cfg)
                    raw = store.source_raw(sid) if store else None
                    if store:
                        store.close()
                    if raw is None:
                        return self._send(404, {"error": "not found"})
                    return self._send(200, raw if isinstance(raw, bytes) else str(raw).encode("utf-8"), "text/plain; charset=utf-8")
                if p == "/api/export":
                    store = open_store(cfg)
                    if store is None:
                        return self._send(404, {"error": "nothing to export yet"})
                    text = export.build_text(store)
                    store.close()
                    return self._send(200, text, "text/plain; charset=utf-8",
                                      {"Content-Disposition": 'attachment; filename="final_plan_and_trace.txt"'})
                return self._static(p)
            except Exception as e:  # never leak a traceback to the page
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def _post_ok(self):
            if not self._host_ok():
                return False
            if self.headers.get("X-PlanLedger") != "compose":
                return False
            origin = self.headers.get("Origin")
            if origin:
                host = urlparse(origin).hostname or ""
                if host not in ("127.0.0.1", "localhost", "::1"):
                    return False
            return True

        def do_POST(self):
            path = urlparse(self.path).path
            if not self._host_ok():
                return self._send(403, {"error": "local access only"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if path == "/api/chat":
                    if n > MAX_BODY:
                        return self._send(413, {"error": "question too long"})
                    data = json.loads(self.rfile.read(n) or b"{}")
                    q = str(data.get("question", ""))[:600]
                    st, store = self._state()
                    if store:
                        store.close()
                    return self._send(200, chat.answer(q, st))
                if path in ("/api/ingest", "/api/reset"):
                    if not self._post_ok():
                        return self._send(403, {"error": "request refused"})
                    if n > MAX_INPUT:
                        return self._send(413, {"error": "input too long"})
                    data = json.loads(self.rfile.read(n) or b"{}")
                    if path == "/api/reset":
                        if data.get("confirm") != "DELETE":
                            return self._send(400, {"error": "confirmation missing"})
                        ok = _reset(cfg)
                        return self._send(200 if ok else 409, {"ok": ok})
                    text = str(data.get("text", ""))
                    if not text.strip():
                        return self._send(400, {"error": "nothing to read: the input is empty"})
                    jid = _start_job(cfg, text)
                    if jid is None:
                        return self._send(409, {"error": "a run is already in progress"})
                    return self._send(202, {"job": jid})
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._send(400, {"error": f"{type(e).__name__}: {e}"})

        def _static(self, p):
            if p in ("", "/"):
                p = "/index.html"
            target = (STATIC / p.lstrip("/")).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            return self._send(200, target.read_bytes(), ctype)

    return Handler


def serve(cfg, port=8765, host="127.0.0.1"):
    srv = ThreadingHTTPServer((host, port), make_handler(cfg))
    srv.daemon_threads = True
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m planledger.web", description="Local dashboard for PlanLedger.")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)
    cfg = Config.load()
    try:
        srv = serve(cfg, a.port)
    except OSError as e:
        print(f"Cannot listen on 127.0.0.1:{a.port}: {e}", file=sys.stderr)
        return 2
    print(f"PlanLedger dashboard: http://127.0.0.1:{a.port}/   data: {cfg.data_dir}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Loopback HTTP server for PlanLedger Evidence Desk UI and Grounded Chatbot."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .chatbot import PROCEDURAL_RULES_COLLECTION, GroundedChatbot
from .config import AppConfig
from .exporter import write_durable_exports
from .orchestrator import PlanLedgerOrchestrator

_WRITE_LOCK = threading.Lock()
STATIC_DIR = Path(__file__).resolve().parent / "static"


def build_state_payload(orchestrator: PlanLedgerOrchestrator) -> Dict[str, Any]:
    store = orchestrator.store
    runs = store.get_all_runs()
    calls = store.get_all_model_calls()
    events = store.get_all_audit_events()
    messages = store.get_all_messages()
    statements = store.get_all_statements()
    revisions = store.get_all_revisions()
    latest_rev = revisions[-1] if revisions else None

    tot_in = 0
    tot_out = 0
    tot_all = 0
    any_partial = False
    for c in calls:
        if (
            c.get("input_tokens") is not None
            and c.get("output_tokens") is not None
            and c.get("total_tokens") is not None
            and c.get("usage_complete", True)
        ):
            tot_in += int(c["input_tokens"])
            tot_out += int(c["output_tokens"])
            tot_all += int(c["total_tokens"])
        else:
            any_partial = True
            if c.get("input_tokens") is not None:
                tot_in += int(c["input_tokens"])
            if c.get("output_tokens") is not None:
                tot_out += int(c["output_tokens"])
            if c.get("total_tokens") is not None:
                tot_all += int(c["total_tokens"])

    total_elapsed = sum(float(r.get("elapsed_sec") or 0.0) for r in runs)

    return {
        "provider_status": orchestrator.config.provider_status(),
        "data_dir": str(orchestrator.config.data_dir),
        "latest_revision": latest_rev,
        "revisions": revisions,
        "runs": runs,
        "model_calls": calls,
        "audit_events": events,
        "messages": messages,
        "statements": statements,
        "procedural_rules": PROCEDURAL_RULES_COLLECTION,
        "session_metrics": {
            "runs_count": len(runs),
            "calls_count": len(calls),
            "input_tokens": tot_in,
            "output_tokens": tot_out,
            "total_tokens": tot_all,
            "tokens_partial": any_partial,
            "total_elapsed_sec": round(total_elapsed, 3),
        },
    }


def run_server(config: AppConfig) -> None:
    orchestrator = PlanLedgerOrchestrator(config)
    chatbot = GroundedChatbot(orchestrator.store)

    class PlanLedgerHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Keep server stdout quiet unless needed
            return

        def _send_json(self, data: Dict[str, Any], status: int = 200) -> None:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_file(
            self, path: Path, content_type: str, download_name: Optional[str] = None
        ) -> None:
            if not path.is_file():
                self._send_json({"error": f"File not found: {path.name}"}, status=404)
                return
            raw = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            if download_name:
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{download_name}"'
                )
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path

            if route == "/" or route == "/index.html":
                self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                return
            if route == "/styles.css":
                self._send_file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
                return
            if route == "/app.js":
                self._send_file(
                    STATIC_DIR / "app.js", "application/javascript; charset=utf-8"
                )
                return
            if route == "/api/state":
                self._send_json(build_state_payload(orchestrator))
                return
            if route.startswith("/api/export/"):
                kind = route.split("/api/export/", 1)[1].strip().lower()
                files = write_durable_exports(
                    orchestrator.store, orchestrator.config.exports_dir
                )
                if kind == "combined":
                    self._send_file(
                        files["combined"],
                        "text/plain; charset=utf-8",
                        "planledger_final_submission.txt",
                    )
                    return
                if kind == "plan":
                    self._send_file(
                        files["plan"], "text/plain; charset=utf-8", "final_plan.txt"
                    )
                    return
                if kind == "trace":
                    self._send_file(
                        files["trace"], "text/plain; charset=utf-8", "full_trace.txt"
                    )
                    return
                if kind == "json":
                    self._send_file(
                        files["json"],
                        "application/json; charset=utf-8",
                        "planledger_state.json",
                    )
                    return
                self._send_json({"error": f"Unknown export kind '{kind}'"}, status=400)
                return

            self._send_json({"error": "Not found"}, status=404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw_body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON request body."}, status=400)
                return

            if route == "/api/chat":
                query = str(body.get("query", ""))
                resp = chatbot.answer(query)
                self._send_json(resp)
                return

            if route == "/api/ingest":
                filename = str(body.get("filename") or "uploaded_message.txt").strip()
                content = body.get("content")
                if not isinstance(content, str):
                    self._send_json(
                        {"error": "Field 'content' (string) is required."}, status=400
                    )
                    return
                with _WRITE_LOCK:
                    result = orchestrator.ingest_bytes(
                        raw_bytes=content.encode("utf-8"),
                        filename=filename,
                    )
                    print(result["trace_text"], flush=True)
                self._send_json(
                    {
                        "run_id": result["run_id"],
                        "status": result["status"],
                        "state": build_state_payload(orchestrator),
                    }
                )
                return

            self._send_json({"error": "Not found"}, status=404)

    server = ThreadingHTTPServer((config.host, config.port), PlanLedgerHandler)
    print(
        f"PlanLedger Evidence Desk UI running at http://{config.host}:{config.port} (Data Dir: {config.data_dir})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

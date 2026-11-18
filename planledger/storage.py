"""SQLite persistence layer for PlanLedger sessions, sources, traces, and revisions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LedgerStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_type TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    raw_bytes BLOB NOT NULL,
                    raw_text TEXT NOT NULL,
                    encoding_errors TEXT NOT NULL DEFAULT '',
                    is_duplicate_of INTEGER,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    elapsed_sec REAL NOT NULL DEFAULT 0.0,
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    status_reason TEXT NOT NULL DEFAULT '',
                    planned_call_areas_json TEXT NOT NULL DEFAULT '[]',
                    observed_call_areas_json TEXT NOT NULL DEFAULT '[]',
                    recomputed_areas_json TEXT NOT NULL DEFAULT '[]',
                    active_revision_after INTEGER,
                    run_trace_text TEXT NOT NULL DEFAULT '',
                    summary_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS model_calls (
                    call_number INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    area_tag TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    reported_model TEXT,
                    provider TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    attempt_relation TEXT NOT NULL,
                    input_payload TEXT NOT NULL,
                    output_content TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    elapsed_sec REAL NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    subcounts_json TEXT NOT NULL DEFAULT '{}',
                    usage_complete INTEGER NOT NULL DEFAULT 1,
                    usage_note TEXT NOT NULL DEFAULT '',
                    error_detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    area_tag TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '',
                    statement_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    seq_order INTEGER NOT NULL,
                    raw_text TEXT NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    claimed_sender TEXT NOT NULL,
                    claimed_role TEXT NOT NULL,
                    sent_date TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    language TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    disposition_reason TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS statements (
                    statement_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    canonical_key TEXT NOT NULL,
                    statement_kind TEXT NOT NULL,
                    hardness TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    disposition_reason TEXT NOT NULL,
                    superseded_by TEXT NOT NULL DEFAULT '',
                    statement_json TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS revisions (
                    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    validation_grade TEXT NOT NULL,
                    is_provisional INTEGER NOT NULL DEFAULT 0,
                    accepted_facts_json TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    delta_json TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
                """
            )

    def has_baseline(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM revisions WHERE validation_grade IN ('VALID', 'PROVISIONAL')"
            ).fetchone()
            return bool(row and row["cnt"] > 0)

    def find_run_by_hash(self, sha256: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE sha256 = ? AND status IN ('accepted', 'provisional', 'rejected_conflict') ORDER BY run_id ASC LIMIT 1",
                (sha256,),
            ).fetchone()
            return dict(row) if row else None

    def start_run(
        self,
        run_type: str,
        filename: str,
        sha256: str,
        raw_bytes: bytes,
        raw_text: str,
        encoding_errors: str = "",
        is_duplicate_of: Optional[int] = None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runs (
                    run_type, filename, sha256, raw_bytes, raw_text,
                    encoding_errors, is_duplicate_of, started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress')
                """,
                (
                    run_type,
                    filename,
                    sha256,
                    raw_bytes,
                    raw_text,
                    encoding_errors,
                    is_duplicate_of,
                    utc_now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def record_model_call(self, call_data: Dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO model_calls (
                    run_id, area_tag, requested_model, reported_model, provider,
                    attempt_number, attempt_relation, input_payload, output_content,
                    outcome, elapsed_sec, input_tokens, output_tokens, total_tokens,
                    subcounts_json, usage_complete, usage_note, error_detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    call_data["run_id"],
                    call_data["area_tag"],
                    call_data["requested_model"],
                    call_data.get("reported_model"),
                    call_data["provider"],
                    call_data["attempt_number"],
                    call_data["attempt_relation"],
                    call_data["input_payload"],
                    call_data["output_content"],
                    call_data["outcome"],
                    float(call_data["elapsed_sec"]),
                    call_data.get("input_tokens"),
                    call_data.get("output_tokens"),
                    call_data.get("total_tokens"),
                    json.dumps(call_data.get("subcounts") or {}, ensure_ascii=False),
                    1 if call_data.get("usage_complete", True) else 0,
                    call_data.get("usage_note", ""),
                    call_data.get("error_detail", ""),
                    utc_now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def record_audit_event(
        self,
        run_id: int,
        event_type: str,
        area_tag: str,
        title: str,
        detail: str,
        message_id: str = "",
        statement_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO audit_events (
                    run_id, event_type, area_tag, message_id, statement_id,
                    title, detail, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event_type,
                    area_tag,
                    message_id,
                    statement_id,
                    title,
                    detail,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    utc_now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def save_messages_and_statements(
        self, run_id: int, messages: List[Dict[str, Any]]
    ) -> None:
        with self._connect() as conn:
            for idx, m in enumerate(messages, start=1):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO messages (
                        message_id, run_id, seq_order, raw_text, line_start, line_end,
                        claimed_sender, claimed_role, sent_date, channel, language,
                        summary, disposition, disposition_reason, record_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        m["message_id"],
                        run_id,
                        idx,
                        m["raw_text"],
                        int(m.get("line_start", 1)),
                        int(m.get("line_end", 1)),
                        m.get("claimed_sender", ""),
                        m.get("claimed_role", ""),
                        m.get("sent_date", ""),
                        m.get("channel", ""),
                        m.get("language", "en"),
                        m.get("summary", ""),
                        m.get("disposition", ""),
                        m.get("disposition_reason", ""),
                        json.dumps(m, ensure_ascii=False),
                    ),
                )
                for s in m.get("statements") or []:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO statements (
                            statement_id, message_id, run_id, canonical_key,
                            statement_kind, hardness, disposition, disposition_reason,
                            superseded_by, statement_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            s["statement_id"],
                            m["message_id"],
                            run_id,
                            s.get("canonical_key", ""),
                            s.get("statement_kind", ""),
                            s.get("hardness", ""),
                            s.get("disposition", ""),
                            s.get("disposition_reason", ""),
                            s.get("superseded_by", ""),
                            json.dumps(s, ensure_ascii=False),
                        ),
                    )

    def update_statement_superseded(
        self, statement_id: str, superseded_by: str, reason: str
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT statement_json FROM statements WHERE statement_id = ?",
                (statement_id,),
            ).fetchone()
            if not row:
                return
            s_obj = json.loads(row["statement_json"])
            s_obj["disposition"] = "superseded"
            s_obj["superseded_by"] = superseded_by
            s_obj["disposition_reason"] = reason
            conn.execute(
                """
                UPDATE statements
                SET disposition = 'superseded', superseded_by = ?, disposition_reason = ?, statement_json = ?
                WHERE statement_id = ?
                """,
                (
                    superseded_by,
                    reason,
                    json.dumps(s_obj, ensure_ascii=False),
                    statement_id,
                ),
            )

    def commit_revision(
        self,
        run_id: int,
        validation_grade: str,
        is_provisional: bool,
        accepted_facts: Dict[str, Any],
        plan: Dict[str, Any],
        validation: Dict[str, Any],
        delta: Dict[str, Any],
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO revisions (
                    run_id, created_at, validation_grade, is_provisional,
                    accepted_facts_json, plan_json, validation_json, delta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    utc_now_iso(),
                    validation_grade,
                    1 if is_provisional else 0,
                    json.dumps(accepted_facts, ensure_ascii=False),
                    json.dumps(plan, ensure_ascii=False),
                    json.dumps(validation, ensure_ascii=False),
                    json.dumps(delta, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        elapsed_sec: float,
        status: str,
        status_reason: str,
        planned_call_areas: List[str],
        observed_call_areas: List[str],
        recomputed_areas: List[str],
        active_revision_after: Optional[int],
        run_trace_text: str,
        summary: Dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET completed_at = ?,
                    elapsed_sec = ?,
                    status = ?,
                    status_reason = ?,
                    planned_call_areas_json = ?,
                    observed_call_areas_json = ?,
                    recomputed_areas_json = ?,
                    active_revision_after = ?,
                    run_trace_text = ?,
                    summary_json = ?
                WHERE run_id = ?
                """,
                (
                    utc_now_iso(),
                    float(elapsed_sec),
                    status,
                    status_reason,
                    json.dumps(planned_call_areas, ensure_ascii=False),
                    json.dumps(observed_call_areas, ensure_ascii=False),
                    json.dumps(recomputed_areas, ensure_ascii=False),
                    active_revision_after,
                    run_trace_text,
                    json.dumps(summary, ensure_ascii=False),
                    run_id,
                ),
            )

    def get_latest_revision(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM revisions ORDER BY revision_id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return self._hydrate_revision(dict(row))

    def get_revision(self, revision_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM revisions WHERE revision_id = ?", (revision_id,)
            ).fetchone()
            if not row:
                return None
            return self._hydrate_revision(dict(row))

    def get_all_revisions(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM revisions ORDER BY revision_id ASC"
            ).fetchall()
            return [self._hydrate_revision(dict(r)) for r in rows]

    def _hydrate_revision(self, d: Dict[str, Any]) -> Dict[str, Any]:
        d["is_provisional"] = bool(d["is_provisional"])
        d["accepted_facts"] = json.loads(d.pop("accepted_facts_json", "{}"))
        d["plan"] = json.loads(d.pop("plan_json", "{}"))
        d["validation"] = json.loads(d.pop("validation_json", "{}"))
        d["delta"] = json.loads(d.pop("delta_json", "{}"))
        return d

    def get_all_runs(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, run_type, filename, sha256, raw_text, encoding_errors,
                       is_duplicate_of, started_at, completed_at, elapsed_sec,
                       status, status_reason, planned_call_areas_json,
                       observed_call_areas_json, recomputed_areas_json,
                       active_revision_after, run_trace_text, summary_json
                FROM runs ORDER BY run_id ASC
                """
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["planned_call_areas"] = json.loads(
                    d.pop("planned_call_areas_json", "[]")
                )
                d["observed_call_areas"] = json.loads(
                    d.pop("observed_call_areas_json", "[]")
                )
                d["recomputed_areas"] = json.loads(
                    d.pop("recomputed_areas_json", "[]")
                )
                d["summary"] = json.loads(d.pop("summary_json", "{}"))
                out.append(d)
            return out

    def get_run_raw_bytes(self, run_id: int) -> Optional[bytes]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT raw_bytes FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return bytes(row["raw_bytes"]) if row else None

    def get_all_model_calls(
        self, run_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if run_id is not None:
                rows = conn.execute(
                    "SELECT * FROM model_calls WHERE run_id = ? ORDER BY call_number ASC",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM model_calls ORDER BY call_number ASC"
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["subcounts"] = json.loads(d.pop("subcounts_json", "{}"))
                d["usage_complete"] = bool(d["usage_complete"])
                out.append(d)
            return out

    def get_all_audit_events(
        self, run_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if run_id is not None:
                rows = conn.execute(
                    "SELECT * FROM audit_events WHERE run_id = ? ORDER BY event_id ASC",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_events ORDER BY event_id ASC"
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["metadata"] = json.loads(d.pop("metadata_json", "{}"))
                out.append(d)
            return out

    def get_all_messages(self, run_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if run_id is not None:
                rows = conn.execute(
                    "SELECT record_json, disposition, disposition_reason FROM messages WHERE run_id = ? ORDER BY seq_order ASC",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT record_json, disposition, disposition_reason FROM messages ORDER BY run_id ASC, seq_order ASC"
                ).fetchall()
            out = []
            for r in rows:
                rec = json.loads(r["record_json"])
                rec["disposition"] = r["disposition"]
                rec["disposition_reason"] = r["disposition_reason"]
                out.append(rec)
            return out

    def get_all_statements(
        self, run_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if run_id is not None:
                rows = conn.execute(
                    "SELECT statement_json, disposition, disposition_reason, superseded_by FROM statements WHERE run_id = ? ORDER BY rowid ASC",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT statement_json, disposition, disposition_reason, superseded_by FROM statements ORDER BY run_id ASC, rowid ASC"
                ).fetchall()
            out = []
            for r in rows:
                st = json.loads(r["statement_json"])
                st["disposition"] = r["disposition"]
                st["disposition_reason"] = r["disposition_reason"]
                st["superseded_by"] = r["superseded_by"]
                out.append(st)
            return out

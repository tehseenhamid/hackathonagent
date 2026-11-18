"""SQLite persistence. One writer (the ingest command); readers (web UI, chat)
open the file read-only. Every ingestion leaves an immutable audit trail:
sources (original bytes + hash), runs, trace items, statements and revisions.

A rejected or failed run never touches the last committed revision: revisions
are only inserted inside the same transaction that finishes a successful run.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .util import utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sources(
  source_id TEXT PRIMARY KEY, run_no INTEGER, sha256 TEXT, filename TEXT, size INTEGER,
  arrival INTEGER, raw BLOB, text TEXT);
CREATE TABLE IF NOT EXISTS runs(
  run_no INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, status TEXT, outcome TEXT,
  source_id TEXT, filename TEXT, sha256 TEXT, started_at TEXT, ended_at TEXT,
  elapsed_ms INTEGER, budget_s REAL, active_rev INTEGER, new_rev INTEGER, summary TEXT);
CREATE TABLE IF NOT EXISTS trace(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_no INTEGER, kind TEXT, call_no INTEGER,
  tag TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS statements(
  stmt_id TEXT PRIMARY KEY, run_no INTEGER, source_id TEXT, seq INTEGER, data TEXT);
CREATE TABLE IF NOT EXISTS revisions(
  rev_no INTEGER PRIMARY KEY, run_no INTEGER, created_at TEXT, grade TEXT,
  plan TEXT, facts TEXT, checks TEXT, delta TEXT);
"""


class Store:
    def __init__(self, data_dir: Path, readonly: bool = False):
        self.dir = Path(data_dir)
        self.path = self.dir / "state.db"
        self.readonly = readonly
        if readonly:
            if not self.path.exists():
                raise FileNotFoundError(str(self.path))
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        else:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(SCHEMA)
        self.db.row_factory = sqlite3.Row

    def close(self):
        self.db.close()

    # --------------------------------------------------------------- meta --
    def get_meta(self, k, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set_meta(self, k, v):
        self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    def next_call_no(self) -> int:
        n = int(self.get_meta("call_counter", "0")) + 1
        self.set_meta("call_counter", n)
        return n

    # ------------------------------------------------------------ recovery --
    def mark_interrupted(self):
        """A previous process died mid-run: keep its trace, mark it honestly."""
        self.db.execute("UPDATE runs SET status='interrupted', outcome='interrupted', ended_at=? WHERE status='running'",
                        (utc_now(),))
        rows = self.db.execute("SELECT seq, payload FROM trace WHERE kind='call'").fetchall()
        for r in rows:
            p = json.loads(r["payload"])
            if p.get("outcome") == "pending":
                p["outcome"] = "interrupted"
                p["error"] = "process ended before the provider answered"
                self.db.execute("UPDATE trace SET payload=? WHERE seq=?", (json.dumps(p), r["seq"]))

    # --------------------------------------------------------------- runs --
    def find_source_by_hash(self, sha: str):
        """Return the most recent completed run that ingested these exact bytes."""
        return self.db.execute(
            "SELECT r.* FROM runs r JOIN sources s ON s.source_id=r.source_id "
            "WHERE s.sha256=? AND r.status='done' AND r.kind IN ('baseline','update') "
            "ORDER BY r.run_no DESC LIMIT 1", (sha,)).fetchone()

    def has_committed_revision(self) -> bool:
        return self.db.execute("SELECT 1 FROM revisions LIMIT 1").fetchone() is not None

    def begin_run(self, kind, filename, sha, budget_s) -> int:
        cur = self.db.execute(
            "INSERT INTO runs(kind,status,filename,sha256,started_at,budget_s) VALUES(?,?,?,?,?,?)",
            (kind, "running", filename, sha, utc_now(), budget_s))
        return cur.lastrowid

    def add_source(self, run_no, sha, filename, raw: bytes, text: str | None) -> str:
        n = self.db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] + 1
        sid = f"S{n}"
        self.db.execute(
            "INSERT INTO sources(source_id,run_no,sha256,filename,size,arrival,raw,text) VALUES(?,?,?,?,?,?,?,?)",
            (sid, run_no, sha, filename, len(raw), n, raw, text))
        self.db.execute("UPDATE runs SET source_id=? WHERE run_no=?", (sid, run_no))
        return sid

    def update_run(self, run_no, **cols):
        if not cols:
            return
        keys = ", ".join(f"{k}=?" for k in cols)
        self.db.execute(f"UPDATE runs SET {keys} WHERE run_no=?", (*cols.values(), run_no))

    # ---------------------------------------------------------------- trace --
    def add_trace(self, run_no, kind, payload: dict, call_no=None, tag=None) -> int:
        cur = self.db.execute("INSERT INTO trace(run_no,kind,call_no,tag,payload) VALUES(?,?,?,?,?)",
                              (run_no, kind, call_no, tag, json.dumps(payload, ensure_ascii=False)))
        return cur.lastrowid

    def update_trace(self, seq, payload: dict):
        self.db.execute("UPDATE trace SET payload=? WHERE seq=?", (json.dumps(payload, ensure_ascii=False), seq))

    # ----------------------------------------------------------- statements --
    def add_statements(self, run_no, source_id, stmts):
        for s in stmts:
            self.db.execute("INSERT OR REPLACE INTO statements(stmt_id,run_no,source_id,seq,data) VALUES(?,?,?,?,?)",
                            (s["id"], run_no, source_id, s["seq"], json.dumps(s, ensure_ascii=False)))

    def all_statements(self):
        rows = self.db.execute("SELECT data FROM statements ORDER BY run_no, seq").fetchall()
        return [json.loads(r["data"]) for r in rows]

    def save_statement_states(self, stmts):
        for s in stmts:
            self.db.execute(
                "INSERT OR REPLACE INTO statements(stmt_id,run_no,source_id,seq,data) VALUES(?,?,?,?,?)",
                (s["id"], s["run_no"], s["source_id"], s["seq"], json.dumps(s, ensure_ascii=False)))

    # ------------------------------------------------------------ revisions --
    def latest_revision(self):
        r = self.db.execute("SELECT * FROM revisions ORDER BY rev_no DESC LIMIT 1").fetchone()
        return self._rev(r)

    def get_revision(self, n):
        return self._rev(self.db.execute("SELECT * FROM revisions WHERE rev_no=?", (n,)).fetchone())

    def all_revisions(self):
        return [self._rev(r) for r in self.db.execute("SELECT * FROM revisions ORDER BY rev_no").fetchall()]

    @staticmethod
    def _rev(r):
        if not r:
            return None
        return {"rev_no": r["rev_no"], "run_no": r["run_no"], "created_at": r["created_at"], "grade": r["grade"],
                "plan": json.loads(r["plan"]), "facts": json.loads(r["facts"]),
                "checks": json.loads(r["checks"]), "delta": json.loads(r["delta"])}

    def finish_run_atomic(self, run_no, *, status, outcome, elapsed_ms, summary: dict, active_rev,
                          revision: dict | None = None, statements=None):
        """One transaction: statement states + (optional) new revision + run row."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            new_rev = None
            if statements is not None:
                self.save_statement_states(statements)
            if revision is not None:
                new_rev = (self.db.execute("SELECT COALESCE(MAX(rev_no),0)+1 n FROM revisions").fetchone()["n"])
                self.db.execute(
                    "INSERT INTO revisions(rev_no,run_no,created_at,grade,plan,facts,checks,delta) VALUES(?,?,?,?,?,?,?,?)",
                    (new_rev, run_no, utc_now(), revision["grade"], json.dumps(revision["plan"], ensure_ascii=False),
                     json.dumps(revision["facts"], ensure_ascii=False), json.dumps(revision["checks"], ensure_ascii=False),
                     json.dumps(revision["delta"], ensure_ascii=False)))
                active_rev = new_rev
            self.db.execute(
                "UPDATE runs SET status=?, outcome=?, ended_at=?, elapsed_ms=?, active_rev=?, new_rev=?, summary=? WHERE run_no=?",
                (status, outcome, utc_now(), elapsed_ms, active_rev, new_rev,
                 json.dumps(summary, ensure_ascii=False), run_no))
            self.db.execute("COMMIT")
            return new_rev
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def finish_run_failed(self, run_no, *, outcome, elapsed_ms, summary: dict, active_rev, status="failed"):
        self.db.execute(
            "UPDATE runs SET status=?, outcome=?, ended_at=?, elapsed_ms=?, active_rev=?, summary=? WHERE run_no=?",
            (status, outcome, utc_now(), elapsed_ms, active_rev, json.dumps(summary, ensure_ascii=False), run_no))

    # ---------------------------------------------------------------- reads --
    def runs(self):
        out = []
        for r in self.db.execute("SELECT * FROM runs ORDER BY run_no").fetchall():
            d = dict(r)
            d["summary"] = json.loads(d["summary"]) if d.get("summary") else {}
            out.append(d)
        return out

    def trace_items(self, run_no=None):
        q = "SELECT * FROM trace" + (" WHERE run_no=?" if run_no else "") + " ORDER BY seq"
        rows = self.db.execute(q, (run_no,) if run_no else ()).fetchall()
        out = []
        for r in rows:
            p = json.loads(r["payload"])
            p.update({"seq": r["seq"], "run_no": r["run_no"], "kind": r["kind"]})
            if r["call_no"] is not None:
                p["call_no"] = r["call_no"]
            out.append(p)
        return out

    def sources(self):
        return [dict(r) for r in self.db.execute(
            "SELECT source_id,run_no,sha256,filename,size,arrival,text FROM sources ORDER BY arrival").fetchall()]

    def source_raw(self, source_id):
        r = self.db.execute("SELECT raw FROM sources WHERE source_id=?", (source_id,)).fetchone()
        return bytes(r["raw"]) if r else None

"""Read model shared by the browser UI and the chat helper. Pure reads of the
persisted state: nothing here writes, calls a model, or invents values."""
from __future__ import annotations

from .config import Config
from .deps import ROUTING_GUIDE
from .render import judging_rounds, totals
from .store import Store
from .util import local_now

LIGHT_ITEM_KEYS = ("seq", "run_no", "kind", "call_no", "tag", "attempt", "attempt_of", "requested_model", "resolved_model",
                   "outcome", "error", "elapsed_ms", "usage", "usage_note", "type", "area", "msg", "ids", "detail")


def fingerprint(store: Store) -> str:
    r = store.db.execute("SELECT COALESCE(MAX(run_no),0) a, COALESCE(MAX(ended_at),'') b, COUNT(*) c FROM runs").fetchone()
    t = store.db.execute("SELECT COALESCE(MAX(seq),0) a, COUNT(*) c FROM trace").fetchone()
    s = store.db.execute("SELECT COUNT(*) c FROM revisions").fetchone()
    return f"{r['a']}:{r['b']}:{r['c']}:{t['a']}:{t['c']}:{s['c']}"


def build_state(store: Store, cfg: Config | None = None) -> dict:
    runs = store.runs()
    rev = store.latest_revision()
    revs = store.all_revisions()
    items = store.trace_items()
    stmts = store.all_statements()
    sources = store.sources()
    light = []
    for i in items:
        light.append({k: i.get(k) for k in LIGHT_ITEM_KEYS if k in i})
    run_rows = []
    for r in runs:
        s = r.get("summary") or {}
        run_rows.append({**{k: r[k] for k in ("run_no", "kind", "status", "outcome", "filename", "sha256", "source_id", "started_at",
                                              "ended_at", "elapsed_ms", "budget_s", "active_rev", "new_rev")},
                         "messages": s.get("messages", []), "statements": s.get("statements", []), "delta": s.get("delta"),
                         "error": s.get("error"), "trace_total": s.get("trace_total"), "calls": s.get("calls"),
                         "grade": s.get("grade"), "duplicate_of_run": s.get("duplicate_of_run")})
    msgs = []
    for r in run_rows:
        for m in r["messages"]:
            msgs.append({**m, "run_no": r["run_no"]})
    state = {
        "generated": local_now(),
        "empty": rev is None,
        "provider_problem": cfg.provider_problem() if cfg else None,
        "model": cfg.model if cfg else None,
        "running": [r["run_no"] for r in runs if r["status"] == "running"],
        "runs": run_rows,
        "active": None,
        "revisions": [{"rev_no": v["rev_no"], "run_no": v["run_no"], "grade": v["grade"], "created_at": v["created_at"],
                       "delta": v["delta"], "checks": {"passed": v["checks"]["passed"], "total": v["checks"]["total"], "failed": v["checks"]["failed"]},
                       "total": v["plan"]["budget"]["total"], "remaining": v["plan"]["budget"]["remaining"]} for v in revs],
        "statements": [{k: s.get(k) for k in ("id", "run_no", "msg_id", "msg_idx", "sender", "role", "channel", "date", "key", "ent", "val",
                                              "status", "hard", "cond", "disposition", "reason", "by", "quote", "span", "lines", "note",
                                              "area", "evidence_mode", "source_id")} for s in stmts],
        "messages": msgs,
        "trace": light,
        "totals": totals(items),
        "sources": [{k: s[k] for k in ("source_id", "run_no", "filename", "sha256", "size", "arrival", "text")} for s in sources],
        "routing_guide": ROUTING_GUIDE,
        "fingerprint": fingerprint(store),
    }
    if rev:
        state["active"] = {"rev_no": rev["rev_no"], "run_no": rev["run_no"], "grade": rev["grade"], "plan": rev["plan"],
                           "checks": rev["checks"], "delta": rev["delta"], "created_at": rev["created_at"],
                           "judging_rounds": judging_rounds(rev["plan"]), "facts": rev["facts"]}
    return state


def trace_item(store: Store, seq: int):
    for i in store.trace_items():
        if i["seq"] == seq:
            return i
    return None

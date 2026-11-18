"""Durable exports built only from the persisted state (so they always match what
the terminal printed). `python -m planledger.export` rewrites them on demand; the
ingest command refreshes them after every run.

Files (inside <data dir>/exports/):
  final_plan_and_trace.txt   one readable file: final plan + decisions + full trace + totals  (upload this)
  plan_latest.txt            the active plan only
  trace_run<N>.txt           one file per run
  session.json               machine-readable copy
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .render import format_call, format_event, format_totals, plan_text, totals
from .store import Store
from .util import local_now


def _decisions(run):
    L = []
    s = run.get("summary") or {}
    for m in s.get("messages", []):
        L.append(f"  message {m['msg_id']} from {m.get('sender') or '?'} ({m.get('role')}, {m.get('channel')}, {m.get('date') or 'no date'}): {m['label'].upper()}"
                 + (f" - {m['irrelevant']}" if m.get("irrelevant") else ""))
    for st in s.get("statements", []):
        if st["disposition"] in ("rejected", "needs_clarification", "superseded", "tentative", "optional", "irrelevant"):
            ln = f" lines {st['lines'][0]}-{st['lines'][1]}" if st.get("lines") else ""
            L.append(f"    {st['id']} {st['disposition'].upper()}: {st['key']}" + (f"[{st['ent']}]" if st.get("ent") else "")
                     + f" = {json.dumps(st['val'], ensure_ascii=False)}")
            L.append(f"        reason: {st['reason']}")
            L.append(f"        evidence{ln}: \"{st.get('quote')}\"")
    return L


def build_text(store: Store) -> str:
    runs = store.runs()
    rev = store.latest_revision()
    items = store.trace_items()
    L = []
    L.append("PLANLEDGER - FINAL PLAN AND FULL TRACE")
    L.append(f"exported {local_now()}   runs: {len(runs)}   committed revisions: {len(store.all_revisions())}")
    L.append("")
    L.append("SESSION SUMMARY")
    for r in runs:
        t = (r.get("summary") or {}).get("trace_total") or totals(store.trace_items(r["run_no"]))
        L.append(f"  run {r['run_no']}: {r['kind']:<9} {r['filename']}  sha256 {str(r['sha256'])[:12]}  outcome {r['outcome']}  "
                 f"elapsed {(r['elapsed_ms'] or 0) / 1000:.2f}s  calls {t['calls']}  tokens {t['total']:,}"
                 + ("  (partial usage)" if t.get("partial") else "") + f"  active revision {r['active_rev']}")
    L.append("")
    L.append("=" * 78)
    if rev:
        L.append(plan_text(rev["plan"], rev["checks"], rev["rev_no"], rev["grade"]))
    else:
        L.append("No committed plan yet. See the trace below for why.")
    L.append("")
    L.append("=" * 78)
    L.append("REVISIONS, CHANGES AND REJECTED / HELD-BACK MESSAGES")
    for r in runs:
        L.append(f"RUN {r['run_no']} ({r['kind']}, {r['filename']}): {r['outcome']}; active revision after run: {r['active_rev']}"
                 + (f"; new revision {r['new_rev']}" if r.get("new_rev") else "; no new revision (last valid plan retained)" if r["kind"] != "baseline" else ""))
        d = (r.get("summary") or {}).get("delta")
        if d and r["kind"] != "baseline":
            L.append("  changed areas: " + (", ".join(d["changed_areas"]) or "none")
                     + "   recomputed by code: " + (", ".join(d["recomputed_areas"]) or "none")
                     + "   areas with model calls: " + (", ".join(d["called_areas"]) or "none"))
            for it in d["items"][:20]:
                L.append(f"    {it['path']}: {it['before']} -> {it['after']}")
        L += _decisions(r)
        if (r.get("summary") or {}).get("error"):
            L.append("  failure: " + r["summary"]["error"])
    L.append("")
    L.append("=" * 78)
    L.append("FULL EXECUTION TRACE (every attempted model call, in order, then deterministic events)")
    for r in runs:
        its = store.trace_items(r["run_no"])
        L.append("")
        L.append(f"##### RUN {r['run_no']} - {r['kind']} - {r['filename']} - {r['outcome']} #####")
        for i in its:
            L.append(format_call(i) if i["kind"] == "call" else format_event(i))
        L.append(format_totals(f"TOTAL for run {r['run_no']}", totals(its)))
    L.append("")
    L.append("=" * 78)
    L.append(format_totals("SESSION TOTAL TOKENS (baseline through all updates)", totals(items)))
    return "\n".join(L) + "\n"


def write_all(store: Store, data_dir: Path) -> Path:
    out = Path(data_dir) / "exports"
    out.mkdir(parents=True, exist_ok=True)
    text = build_text(store)
    (out / "final_plan_and_trace.txt").write_text(text, encoding="utf-8")
    rev = store.latest_revision()
    if rev:
        (out / "plan_latest.txt").write_text(plan_text(rev["plan"], rev["checks"], rev["rev_no"], rev["grade"]) + "\n", encoding="utf-8")
    for r in store.runs():
        its = store.trace_items(r["run_no"])
        body = "\n".join(format_call(i) if i["kind"] == "call" else format_event(i) for i in its)
        (out / f"trace_run{r['run_no']}.txt").write_text(body + "\n" + format_totals("TOTAL", totals(its)) + "\n", encoding="utf-8")
    session = {"runs": store.runs(), "revisions": store.all_revisions(), "trace": store.trace_items(),
               "totals": totals(store.trace_items())}
    (out / "session.json").write_text(json.dumps(session, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main(argv=None):
    from .config import Config
    cfg = Config.load()
    try:
        store = Store(cfg.data_dir, readonly=False)
    except Exception as e:
        print("cannot open state:", e)
        return 2
    out = write_all(store, cfg.data_dir)
    print(f"exports written to {out}")
    print(out / "final_plan_and_trace.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())

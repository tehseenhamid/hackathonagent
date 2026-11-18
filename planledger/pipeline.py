"""One ingest pipeline for every file: baseline pack or later update.

    bytes -> store original -> ONE reading call (verbatim) -> verified statements
          -> reconcile (authority / time / scope) -> deterministic solve -> independent validate
          -> keep the last valid plan unless the candidate is valid -> atomic commit -> print trace

Baseline-vs-update is decided from durable state, never from a flag or a filename.
Hard limits are enforced with monotonic deadlines; a late provider answer can never commit.
"""
from __future__ import annotations

import itertools
import json
import threading
import time
from pathlib import Path

from . import reader as rd
from .config import HARD_UPDATE_CALLS, Config
from .deps import closure, dirty_areas, fact_changes
from .planner import diff_plans, solve
from .providers import ProviderError, make_adapter
from .reconcile import (ACCEPTED, CLARIFY, GOVERNING_ROLES, OPTIONAL, REJECTED, SUPERSEDED, TENTATIVE,
                        build_facts, message_summaries, reconcile)
from .render import format_call, format_event, format_totals, plan_text, totals
from .store import Store
from .util import local_now, sha256_hex, short


class Stop(Exception):
    """Run cannot continue; carries an outcome label and a human message."""

    def __init__(self, outcome, msg):
        super().__init__(msg)
        self.outcome, self.msg = outcome, msg


class Run:
    def __init__(self, cfg, store, run_no, kind, out, t0, budget_s, adapter, call_limit):
        self.cfg, self.store, self.run_no, self.kind, self.out = cfg, store, run_no, kind, out
        self.t0, self.deadline, self.adapter, self.call_limit = t0, t0 + budget_s, adapter, call_limit
        self.calls = 0
        self.items = []
        self.last_call_no = None

    def remaining(self):
        return self.deadline - time.monotonic()

    def expired(self):
        return self.remaining() <= self.cfg.reserve_s * 0.25

    def event(self, type_, msg, area=None, ids=None, **detail):
        e = {"type": type_, "msg": msg, "area": area, "ids": ids or [], "detail": detail}
        self.store.add_trace(self.run_no, "event", e)
        self.items.append({**e, "kind": "event"})
        self.out(format_event(e))

    def model_call(self, tag, system, user, attempt=1, attempt_of=None, max_tokens=None):
        if self.calls >= self.call_limit:
            raise Stop("failed_call_limit", f"model-call limit reached ({self.call_limit} for this {self.kind}); no further call was made")
        usable = self.remaining() - self.cfg.reserve_s
        if usable < 2.0:
            raise Stop("failed_timeout", f"only {max(usable, 0):.1f}s of the {self.kind} budget remain after reserving time to save and print; no call was made")
        timeout = min(usable, self.cfg.per_call_cap_s)
        self.calls += 1
        call_no = self.store.next_call_no()
        rec = {"call_no": call_no, "tag": tag, "attempt": attempt, "attempt_of": attempt_of,
               "requested_model": self.cfg.model, "resolved_model": None, "outcome": "pending", "error": None,
               "elapsed_ms": None, "input": {"system": system, "user": user}, "output": None, "usage": None,
               "usage_note": None, "timeout_s": round(timeout, 2)}
        seq = self.store.add_trace(self.run_no, "call", rec, call_no, tag)
        self.out(f"--> sending model call #{call_no} [{tag}] attempt {attempt}; timeout {timeout:.1f}s; "
                 f"{self.remaining():.1f}s of {self.kind} budget left")
        holder = {}
        t = time.monotonic()

        def worker():
            try:
                holder["res"] = self.adapter.complete(system, user, timeout, max_tokens or self.cfg.max_output_tokens,
                                                      self.cfg.json_mode)
            except ProviderError as e:
                holder["err"] = e
            except Exception as e:  # never let an adapter bug kill the run silently
                holder["err"] = ProviderError("network", f"{type(e).__name__}: {e}")

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        th.join(timeout)
        rec["elapsed_ms"] = int((time.monotonic() - t) * 1000)
        if th.is_alive():
            holder["abandoned"] = True   # the worker only returns data, it never writes state
            rec["outcome"], rec["error"] = "timeout", f"no complete answer within {timeout:.1f}s; the late answer (if any) is discarded"
        elif "err" in holder:
            rec["outcome"], rec["error"] = holder["err"].kind, holder["err"].detail
        else:
            res = holder["res"]
            rec.update(outcome="ok", output=res.text, resolved_model=res.resolved_model, usage=res.usage,
                       usage_note=res.usage_note)
            rec["finish"] = res.finish
        self.store.update_trace(seq, rec)
        full = {**rec, "kind": "call", "run_no": self.run_no}
        self.items.append(full)
        self.out(format_call(full))
        self.last_call_no = call_no
        return rec


# ------------------------------------------------------------------ helpers --

def prior_context(rev) -> str:
    facts = rev["facts"]
    parts = []
    for k, it in sorted(facts["single"].items()):
        parts.append(f"{k}={json.dumps(it['v'], ensure_ascii=False, separators=(',', ':'))}")
    for k, d in sorted(facts["multi"].items()):
        for e, it in sorted(d.items(), key=lambda kv: str(kv[0])):
            parts.append(f"{k}[{e}]={json.dumps(it['v'], ensure_ascii=False, separators=(',', ':'))}")
    return "; ".join(parts)


def read_pack(run: Run, text, source_id, kind, prior):
    """The reading step. Attempt 1 reads everything. If the answer breaks the schema, it is sent back for repair.
    If it is well formed but left items out (a number nobody used), a small second call asks only about those
    sentences and the answers are merged. Whatever is still missing at the end is kept as a held-back statement."""
    best = None            # (messages, statements, issues, obj)
    errors, prev, last_fail = None, None, None
    mode, base_obj, gap_sentences = "full", None, []
    for attempt in range(1, run.cfg.max_read_attempts + 1):
        if mode == "gap":
            user = rd.build_gap_payload(kind, gap_sentences, prior)
        else:
            user = rd.build_user_payload(kind, text, prior)
            if errors:
                user += rd.REPAIR_SUFFIX.format(errors=errors)
        rec = run.model_call("reading", rd.SYSTEM_PROMPT, user, attempt=attempt, attempt_of=prev)
        prev = rec["call_no"]
        if rec["outcome"] != "ok":
            last_fail = rec
            run.event("model_failure", f"call #{rec['call_no']} {rec['outcome']}: {short(rec.get('error') or '', 200)}", "reading")
            if rec["outcome"] in ("timeout", "config") or best is not None:
                break
            if run.remaining() < 8:
                break
            errors = None
            continue
        obj, err = rd.parse_reader_output(rec["output"])
        if err:
            run.event("schema_failure", f"call #{rec['call_no']}: {err}", "reading")
            if mode == "gap":
                break
            errors = err
            continue
        if mode == "gap":
            obj = rd.merge_gap_fill(base_obj, obj, text)
        messages, stmts, issues = rd.extract(obj, text, run.run_no, source_id, strict=True)
        gaps = rd.missed_items(text, stmts, messages)
        issues = issues + ["omitted: " + g for g in rd.missed_numbers(text, stmts, messages)]
        if best is None or len(issues) <= len(best[2]):
            best = (messages, stmts, issues, obj)
        if not issues:
            break
        for i in issues[:6]:
            run.event("schema_issue", i, "reading")
        schema = [i for i in issues if not i.startswith("omitted: ")]
        if schema:
            mode, errors = "full", "\n".join("- " + i for i in issues[:8])
        elif gaps and run.remaining() > 20:
            mode, base_obj, gap_sentences, errors = "gap", obj, rd.gap_paragraphs(text, gaps), None
            run.event("gap_fill", f"{len(gaps)} sentence(s) had numbers no statement used; asking the model about only those sentences", "reading")
        else:
            break
    if best is None:
        if last_fail is not None and last_fail["outcome"] == "timeout":
            raise Stop("failed_timeout", "the model did not answer in time: " + short(last_fail.get("error") or "", 160)
                       + " (no state was changed)")
        if last_fail is not None:
            raise Stop("failed_provider", f"the model call failed ({last_fail['outcome']}): " + short(last_fail.get("error") or "", 200)
                       + " (no state was changed)")
        raise Stop("failed_parse", "the reading step produced no usable structured output (no state was changed)")
    # anything still missing is held back visibly rather than ignored
    left = rd.missed_items(text, best[1], best[0])
    if left:
        marked = rd.add_gap_markers(best[3], left, text)
        m2, s2, i2 = rd.extract(marked, text, run.run_no, source_id, strict=True)
        best = (m2, s2, [i for i in i2 if not i.startswith("omitted: ")] + ["omitted: " + g["sentence"] for g in left], marked)
    return best


def acceptable(val, prev_rev):
    if val["grade"] == "valid":
        return True
    if prev_rev is None:
        return True
    pv = prev_rev["checks"]
    return prev_rev["grade"] != "valid" and set(val["failed"]) <= set(pv["failed"]) and True


def evaluate(cfg, stmts, new_ids, lock_ids, lock_reasons=None):
    for s in stmts:
        if s["id"] in new_ids:
            s["lock"] = None
            if s["id"] in lock_ids:
                dis, why = (lock_reasons or {}).get(s["id"], (REJECTED, "would make the plan invalid"))
                s["lock"] = {"disposition": dis, "reason": why}
    groups, events = reconcile(stmts)
    facts = build_facts(groups, stmts)
    res = solve(facts, cfg)
    return {"groups": groups, "events": events, "facts": facts, "plan": res["plan"], "val": res["validation"]}


def failing_text(val, limit=3):
    bad = [c for c in val["checks"] if c["status"] == "fail"][:limit]
    return "; ".join(f"{c['label']} - {c['detail']}" for c in bad) or "validation failed"


def stmt_brief(s):
    return {k: s.get(k) for k in ("id", "msg_id", "msg_idx", "sender", "role", "date", "key", "ent", "val", "status", "hard",
                                  "disposition", "reason", "by", "quote", "lines", "note", "area")}


# --------------------------------------------------------------------- main --

def run_ingest(path, cfg: Config, out=None) -> int:
    out = out or (lambda s: print(s, flush=True))
    t0 = time.monotonic()
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        out(f"ERROR: cannot read '{path}': {e.strerror or e}")
        return 2
    store = Store(cfg.data_dir)
    try:
        store.mark_interrupted()
        return _ingest(cfg, store, p, raw, out, t0)
    finally:
        store.close()


def _ingest(cfg, store, p: Path, raw: bytes, out, t0) -> int:
    sha = sha256_hex(raw)
    kind = "update" if store.has_committed_revision() else "baseline"
    budget = cfg.update_budget_s if kind == "update" else cfg.baseline_budget_s
    dup = store.find_source_by_hash(sha)
    if dup:
        run_no = store.begin_run("duplicate", p.name, sha, 0)
        sid = store.add_source(run_no, sha, p.name, raw, raw.decode("utf-8", "replace"))
        rev = store.latest_revision()
        store.finish_run_atomic(run_no, status="done", outcome="duplicate_ignored", elapsed_ms=int((time.monotonic() - t0) * 1000),
                                summary={"duplicate_of_run": dup["run_no"], "note": "identical bytes already ingested; nothing re-run"},
                                active_rev=rev["rev_no"] if rev else None)
        store.add_trace(run_no, "event", {"type": "duplicate", "area": None, "ids": [],
                                          "msg": f"file is byte-identical to the one ingested in run {dup['run_no']}; no model call, state unchanged",
                                          "detail": {"sha256": sha}})
        out(f"PLANLEDGER run {run_no}: DUPLICATE. '{p.name}' (sha256 {sha[:12]}) is byte-identical to run {dup['run_no']}. "
            f"Nothing was re-run, no model call was made, revision {rev['rev_no'] if rev else '-'} is unchanged.")
        return 0

    run_no = store.begin_run(kind, p.name, sha, budget)
    adapter = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        store.add_source(run_no, sha, p.name, raw, None)
        msg = f"file is not valid UTF-8 (byte {e.start}): {e.reason}. Save it as UTF-8 and run again."
        store.add_trace(run_no, "event", {"type": "invalid_input", "msg": msg, "area": None, "ids": [], "detail": {}})
        _finish_failed(store, run_no, "invalid_input", t0, msg)
        out("ERROR: " + msg)
        return 2
    sid = store.add_source(run_no, sha, p.name, raw, text)
    limit = HARD_UPDATE_CALLS if kind == "update" else 99
    call_limit = min(cfg.max_update_calls, HARD_UPDATE_CALLS) if kind == "update" else cfg.max_baseline_calls
    run = Run(cfg, store, run_no, kind, out, t0, budget, None, call_limit)
    prev_rev = store.latest_revision()
    out("=" * 78)
    out(f"PLANLEDGER ingest   run {run_no}   {kind.upper()}   file: {p.name}   {local_now()}")
    out(f"sha256 {sha}   bytes {len(raw)}   stored as source {sid}")
    out(f"limits: {'120 s full run' if kind == 'baseline' else '60 s and 5 model calls per update'} (hard); "
        f"this run is bounded to {budget:.0f} s and {call_limit} model call(s)")
    if kind == "update":
        out(f"state: revision {prev_rev['rev_no']} is the last valid plan; it stays in force unless this update is accepted")
    try:
        if not text.strip():
            raise Stop("invalid_input", "the file is empty")
        prob = cfg.provider_problem()
        if prob:
            raise Stop("unconfigured", "model provider is not configured: " + prob +
                       " Copy .env.example to .env, fill it in, run `python -m planledger.probe`, then run this command again.")
        run.adapter = make_adapter(cfg)
        run.event("source_stored", f"original bytes and sha256 stored; {text.count(chr(10)) + 1} lines read verbatim", "reading")
        prior = prior_context(prev_rev) if prev_rev else None
        messages, new_stmts, issues, _obj = read_pack(run, text, sid, kind, prior)
        if prev_rev:
            fixed = rd.apply_corrections(new_stmts, text, prev_rev["facts"].get("multi", {}))
            if fixed:
                run.event("correction_filed", f"{fixed} statement(s) correct an amount that is already committed; filed as the new amount, not as an extra charge", "budget")
        for m in messages:
            m["run_no"] = run_no
        # extraction audit
        for g in [i for i in issues if i.startswith("omitted: ")][:6]:
            run.event("extraction_gap", g[len("omitted: "):], "reading")
        unc = rd.uncovered_numbers(text, new_stmts)
        if unc:
            run.event("extraction_audit", f"{len(unc)} number(s) in the source are not inside any cited evidence (check they are irrelevant): "
                      + " | ".join(short(u, 60) for u in unc[:6]), "reading")
        if len(messages) == 0:
            raise Stop("failed_parse", "the reader returned no messages")
        out(f"  reading complete: {len(messages)} message(s), {len(new_stmts)} candidate statement(s)")
        result = _decide(run, prev_rev, store.all_statements(), new_stmts, kind)
        if run.expired():
            raise Stop("failed_timeout", f"the {kind} time budget was exhausted before the result could be committed; the last valid plan is untouched")
        elapsed = int((time.monotonic() - t0) * 1000)
        summ = _summary(run, result, messages, kind)
        rev_no = store.finish_run_atomic(
            run_no, status="done", outcome=result["outcome"], elapsed_ms=elapsed, summary=summ,
            active_rev=prev_rev["rev_no"] if prev_rev else None, revision=result["revision"], statements=result["stmts"])
        active = rev_no or (prev_rev["rev_no"] if prev_rev else None)
        _print_result(run, store, result, active, elapsed)
        try:
            from .export import write_all
            write_all(store, cfg.data_dir)
        except Exception as e:  # export problems must not hide a committed result
            out(f"  note: export files could not be written: {e}")
        return 0
    except Stop as s:
        run.event("failed", s.msg, None)
        _finish_failed(store, run_no, s.outcome, t0, s.msg, prev_rev, run)
        tot = totals(run.items)
        out("-" * 78)
        out(f"RUN {run_no} FAILED ({s.outcome}): {s.msg}")
        out(f"The last valid plan is untouched" + (f" (revision {prev_rev['rev_no']})." if prev_rev else "; there is no committed plan yet."))
        out(format_totals(f"TRACE TOTAL for run {run_no}", tot))
        _session_total(store, out)
        return 1 if s.outcome != "invalid_input" else 2
    except KeyboardInterrupt:
        _finish_failed(store, run_no, "interrupted", t0, "interrupted by user", prev_rev, run)
        out(f"\nRUN {run_no} INTERRUPTED. The last valid plan is untouched.")
        return 130
    except Exception as e:  # an unexpected bug must never leave the run marked as running
        msg = f"internal error ({type(e).__name__}: {e}); no state was changed"
        run.event("failed", msg, None)
        _finish_failed(store, run_no, "failed_internal", t0, msg, prev_rev, run)
        out("-" * 78)
        out(f"RUN {run_no} FAILED (failed_internal): {msg}")
        out(f"The last valid plan is untouched" + (f" (revision {prev_rev['rev_no']})." if prev_rev else "; there is no committed plan yet."))
        return 1


def _finish_failed(store, run_no, outcome, t0, msg, prev_rev=None, run=None):
    summary = {"error": msg}
    if run is not None:
        summary["trace_total"] = totals(run.items)
    store.finish_run_failed(run_no, outcome=outcome, elapsed_ms=int((time.monotonic() - t0) * 1000), summary=summary,
                            active_rev=prev_rev["rev_no"] if prev_rev else None)


def _session_total(store, out):
    t = totals(store.trace_items())
    out(format_totals("SESSION TOTAL (all runs, including failed attempts)", t))


# ------------------------------------------------------------------ deciding --

def _decide(run: Run, prev_rev, prior_stmts, new_stmts, kind):
    cfg, out = run.cfg, run.out
    all_stmts = prior_stmts + new_stmts
    new_ids = {s["id"] for s in new_stmts}
    cand = evaluate(cfg, all_stmts, new_ids, set())
    locked = {}
    if kind == "update" and not acceptable(cand["val"], prev_rev):
        accepted_new = [s["id"] for s in new_stmts if s["disposition"] == ACCEPTED]
        run.event("conflict_search", f"applying every accepted statement makes the plan invalid ({failing_text(cand['val'])}); "
                  f"searching for the smallest set of statements to hold back among {len(accepted_new)}", None)
        found = None
        for k in range(1, min(len(accepted_new), 4) + 1):
            for combo in itertools.combinations(accepted_new, k):
                if run.remaining() < cfg.reserve_s + 3:
                    break
                trial = evaluate(cfg, all_stmts, new_ids, set(combo))
                if acceptable(trial["val"], prev_rev):
                    found = combo
                    break
            if found or run.remaining() < cfg.reserve_s + 3:
                break
        if found is None:
            found = tuple(accepted_new)
        why_full = failing_text(cand["val"])
        by_id = {s["id"]: s for s in all_stmts}
        for sid in found:
            s = by_id[sid]
            gov = s["role"] in GOVERNING_ROLES
            dis = CLARIFY if gov else REJECTED
            reason = (f"applying this would break the plan: {why_full}. "
                      + ("The sender is authoritative, so this needs an organizer decision. " if gov else "")
                      + f"The last valid plan (revision {prev_rev['rev_no']}) stays in force.")
            locked[sid] = (dis, reason)
        cand = evaluate(cfg, all_stmts, new_ids, set(locked), locked)
        for sid, (dis, reason) in locked.items():
            run.event("rejected" if dis == REJECTED else "needs_clarification",
                      f"{sid} ({by_id[sid]['key']}[{by_id[sid]['ent']}] = {short(str(by_id[sid]['val']), 60)}): {reason}",
                      by_id[sid].get("area"), [sid])
    for e in cand["events"]:
        if prev_rev is None or any(i in new_ids for i in e.get("ids", [])):
            run.event(e["type"], e["msg"], e.get("area"), e.get("ids"))
    plan, val, facts = cand["plan"], cand["val"], cand["facts"]
    # per-statement decisions (new ones), printed as deterministic events
    for s in new_stmts:
        run.event("decision", f"{s['id']} {s['disposition']}: {s['key']}" + (f"[{s['ent']}]" if s.get("ent") else "")
                  + f" = {short(json.dumps(s['val'], ensure_ascii=False), 70)} ({short(s['reason'] or '', 110)})", s.get("area"), [s["id"]])
    outcome, revision, delta = None, None, None
    new_disp = [s["disposition"] for s in new_stmts]
    if prev_rev is None:
        delta = {"items": diff_plans(None, plan), "changed_areas": sorted({"venue", "schedule", "food", "judging", "budget"}),
                 "recomputed_areas": ["venue", "schedule", "food", "judging", "budget"], "fact_changes": fact_changes(None, facts),
                 "called_areas": sorted({i["tag"] for i in run.items if i.get("kind") == "call"})}
        revision = {"grade": val["grade"], "plan": plan, "facts": facts, "checks": val, "delta": delta}
        outcome = "baseline_committed" if val["grade"] == "valid" else "baseline_provisional"
    else:
        items = diff_plans(prev_rev["plan"], plan)
        changes = fact_changes(prev_rev["facts"], facts)
        dirty = closure(dirty_areas(changes)) if changes else set()
        changed = sorted({d["area"] for d in items if d["area"] in ("venue", "schedule", "food", "judging", "budget")})
        delta = {"items": items, "changed_areas": changed, "recomputed_areas": sorted(dirty),
                 "fact_changes": changes, "called_areas": sorted({i["tag"] for i in run.items if i.get("kind") == "call"}),
                 "kept_valid_areas": sorted({"venue", "schedule", "food", "judging", "budget"} - set(changed))}
        n_rej = sum(1 for d in new_disp if d in (REJECTED, CLARIFY))
        n_acc = sum(1 for d in new_disp if d == ACCEPTED)
        if items and acceptable(val, prev_rev):
            revision = {"grade": val["grade"], "plan": plan, "facts": facts, "checks": val, "delta": delta}
            outcome = "update_partial" if n_rej else "update_accepted"
        else:
            plan, val, facts = prev_rev["plan"], prev_rev["checks"], prev_rev["facts"]
            if any(d == CLARIFY for d in new_disp) and not items:
                outcome = "update_blocked"
            elif n_rej:
                outcome = "update_rejected"
            else:
                outcome = "update_no_change"
    run.event("routing", "model calls observed: " + (", ".join(sorted({i['tag'] for i in run.items if i.get('kind') == 'call'})) or "none")
              + "; areas recomputed by code (never a model call): "
              + (", ".join(delta["recomputed_areas"]) if delta and delta.get("recomputed_areas") else "none"), None)
    return {"outcome": outcome, "revision": revision, "delta": delta, "plan": plan, "val": val, "facts": facts,
            "stmts": cand_stmts(all_stmts), "events": cand["events"]}


def cand_stmts(all_stmts):
    return all_stmts


def _summary(run, result, messages, kind):
    stmts = result["stmts"]
    mine = [s for s in stmts if s["run_no"] == run.run_no]
    return {"kind": kind, "messages": message_summaries(messages, stmts), "statements": [stmt_brief(s) for s in mine],
            "delta": result["delta"], "grade": result["val"]["grade"], "trace_total": totals(run.items),
            "calls": run.calls, "grade_failed": result["val"]["failed"]}


def _print_result(run: Run, store, result, active_rev, elapsed_ms):
    out = run.out
    outcome = result["outcome"]
    label = {"baseline_committed": "baseline plan committed", "baseline_provisional": "baseline plan committed as PROVISIONAL",
             "update_accepted": "update ACCEPTED, new revision committed", "update_partial": "update PARTIALLY accepted, new revision committed",
             "update_rejected": "update REJECTED, last valid plan retained", "update_blocked": "update BLOCKED pending clarification, last valid plan retained",
             "update_no_change": "update made no change to the plan, last valid plan retained"}[outcome]
    out("-" * 78)
    out(f"RESULT run {run.run_no}: {label}")
    out(f"active revision: {active_rev}   grade: {result['val']['grade'].upper()}   elapsed: {elapsed_ms / 1000:.2f} s")
    d = result["delta"]
    if run.kind == "update":
        out("changed areas        : " + (", ".join(d["changed_areas"]) or "none"))
        out("recomputed by code   : " + (", ".join(d["recomputed_areas"]) or "none") + "   (no model call)")
        out("areas with model call: " + (", ".join(d["called_areas"]) or "none"))
        out("kept valid unchanged : " + (", ".join(d.get("kept_valid_areas", [])) or "none"))
        for it in d["items"][:12]:
            out(f"  change {it['path']}: {it['before']} -> {it['after']}")
    out("")
    out(plan_text(result["plan"], result["val"], active_rev, result["val"]["grade"]))
    out("")
    out("=" * 78)
    out(format_totals(f"TRACE TOTAL for run {run.run_no}", totals(run.items)))
    _session_total(store, out)

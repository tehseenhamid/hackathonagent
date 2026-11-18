"""Plain-text rendering of plans and traces. The terminal output, the exported
files and the browser all show the same underlying data; this module only formats it."""
from __future__ import annotations

from .util import fmt_clock, fmt_offset, parse_clock, pkr


def tlabel(plan, mins):
    sc = plan["event"].get("start_clock")
    if sc:
        return fmt_clock(parse_clock(sc) + mins)
    return fmt_offset(mins)


def usage_str(u, note=None):
    if not u:
        return "tokens: unavailable" + (f" ({note})" if note else " (provider reported no usage for this call)")
    def f(x):
        return "unavailable" if x is None else f"{x:,}"
    s = f"tokens: input {f(u.get('input'))} | output {f(u.get('output'))} | total {f(u.get('total'))}"
    if u.get("sub"):
        s += "   [" + "; ".join(f"{k}: {v}" for k, v in u["sub"].items()) + "]"
    if note:
        s += f"   ({note})"
    return s


def format_call(c) -> str:
    out = []
    out.append("=" * 78)
    rel = f"retry of call #{c['attempt_of']}" if c.get("attempt_of") else "first attempt"
    out.append(f"MODEL CALL #{c['call_no']}   area tag: {c['tag']}   run {c['run_no']}   attempt {c.get('attempt', 1)} ({rel})")
    model = c.get("resolved_model") or "unavailable (provider did not report one)"
    out.append(f"model (provider-reported): {model}   |   configured: {c.get('requested_model')}")
    out.append(f"outcome: {c.get('outcome')}   elapsed: {(c.get('elapsed_ms') or 0) / 1000:.2f} s"
               + (f"   error: {c['error']}" if c.get("error") else ""))
    out.append("-" * 30 + " INPUT sent to the model " + "-" * 23)
    out.append("[system]\n" + (c.get("input", {}).get("system") or ""))
    out.append("[user]\n" + (c.get("input", {}).get("user") or ""))
    out.append("-" * 30 + " OUTPUT returned " + "-" * 31)
    out.append(c.get("output") if c.get("output") is not None else "(no output: " + str(c.get("outcome")) + ")")
    out.append(usage_str(c.get("usage"), c.get("usage_note")))
    return "\n".join(out)


def format_event(e) -> str:
    area = f" [{e['area']}]" if e.get("area") else ""
    ids = f"   evidence: {', '.join(e['ids'])}" if e.get("ids") else ""
    return f"  * {e['type']}{area}: {e['msg']}{ids}"


def totals(items):
    calls = [i for i in items if i.get("kind") == "call"]
    tin = tout = ttot = 0
    missing = 0
    for c in calls:
        u = c.get("usage")
        if not u or (u.get("input") is None and u.get("output") is None and u.get("total") is None):
            missing += 1
            continue
        tin += u.get("input") or 0
        tout += u.get("output") or 0
        if u.get("total") is not None:
            ttot += u["total"]
        elif u.get("input") is not None and u.get("output") is not None:
            ttot += u["input"] + u["output"]
        else:
            missing += 1
    return {"calls": len(calls), "input": tin, "output": tout, "total": ttot, "missing_usage": missing,
            "partial": missing > 0, "elapsed_ms": sum(c.get("elapsed_ms") or 0 for c in calls)}


def format_totals(label, t) -> str:
    s = (f"{label}: {t['calls']} model call(s); input {t['input']:,} | output {t['output']:,} | TOTAL TOKENS {t['total']:,}")
    if t["partial"]:
        s += f"   [PARTIAL: {t['missing_usage']} call(s) returned no usage, so this total excludes them]"
    return s


# ----------------------------------------------------------------- plan text --

def judging_rounds(plan):
    j = plan["judging"]
    rounds = {}
    for a in j["assignments"]:
        rounds.setdefault(a["round"], []).append(a)
    rows = []
    for r in sorted(rounds):
        al = rounds[r]
        teams = [a["team"] for a in al]
        rows.append({"round": r, "start": al[0]["start"], "end": al[0]["end"], "teams": f"{min(teams)}-{max(teams)}" if len(teams) > 1 else str(teams[0]),
                     "count": len(teams), "judges": sorted({jn for a in al for jn in a["judges"]})})
    return rows


def _u(x):
    return "unknown" if x is None else x


def plan_text(plan, val, rev_no, grade) -> str:
    ev, ve, sc, fo, ju, bu = (plan[k] for k in ("event", "venue", "schedule", "food", "judging", "budget"))
    L = []
    L.append(f"PLANLEDGER PLAN - revision {rev_no} - grade: {grade.upper()}  ({val['passed']} of {val['total']} checks pass)")
    if grade != "valid":
        L.append("  This plan is PROVISIONAL: see the failing or unverified checks below. It is a best-effort draft, not a guarantee.")
    L.append("")
    L.append("EVENT")
    L.append(f"  {_u(ev['students'])} students in teams of {_u(ev['team_size'])} = {_u(ev['teams'])} teams ({ev.get('teams_basis') or 'not derivable'}); {_u(ev['staff'])} staff"
             + (f"; {ev['volunteers']} volunteers" if ev.get("volunteers") else "")
             + f"; {_u(ev['duration_min'])} minutes; start " + (ev["start_clock"] if ev.get("start_clock") else "not stated (offsets shown)"))
    L.append("")
    L.append("VENUE")
    if ve.get("name"):
        L.append(f"  {ve['name']}: capacity {ve['capacity']}, hire {pkr(ve['rent_total'])}; {ve['headcount']} people on site "
                 f"({', '.join(f'{k} {v}' for k, v in ve['headcount_parts'].items() if v)})")
    else:
        L.append("  no venue could be chosen")
    for a in ve.get("alternatives", []):
        L.append(f"    - {a['name']}: {a['verdict']} - {a['reason']}")
    L.append("")
    L.append("SCHEDULE (all " + str(_u(sc["duration_min"])) + " minutes)" + ("" if sc["segments"] else ": none produced because required inputs are unknown"))
    for s in sc["segments"]:
        L.append(f"  {tlabel(plan, s['start']):>7} - {tlabel(plan, s['end']):<7} {s['dur']:>4} min  {s['label']}")
    for n in sc.get("notes", []):
        L.append("  note: " + n)
    L.append("")
    L.append("FOOD")
    L.append(f"  {_u(fo['qty'])} meals for {_u(fo['fed'])} people ({', '.join(f'{k} {v}' for k, v in fo['fed_parts'].items() if v)})")
    for l in fo["lines"]:
        L.append(f"    {l['qty']} x {pkr(l['price'])} {l['option']}" + (f" [{', '.join(l['diet'])}]" if l["diet"] else "") + f" = {pkr(l['subtotal'])}")
    for a in fo.get("alternatives", []):
        L.append(f"    - {a['name']}: {a['verdict']} - {a['reason']}")
    L.append("")
    L.append("JUDGING")
    L.append(f"  {_u(ju['teams'])} teams x {ju['panel']} judge(s), {_u(ju['slot_min'])} min each, at most {ju['max_teams'] or 'unlimited'} teams per judge: "
             f"minimum {_u(ju['min_needed'])} judges; {_u(ju['deployed'])} deployed from {_u(ju['confirmed'])} confirmed; {ju['rounds']} round(s)")
    for r in judging_rounds(plan):
        L.append(f"    round {r['round']} {tlabel(plan, r['start'])}-{tlabel(plan, r['end'])}: teams {r['teams']} ({r['count']} parallel lane(s))")
    L.append("")
    L.append("BUDGET")
    for l in bu["lines"]:
        calc = f"{l['qty']} x {pkr(l['rate'])} = " if l.get("qty") and l.get("rate") is not None else ""
        L.append(f"  {l['label']:<38} {calc}{pkr(l['amount'])}")
    for p in bu["paid_toward"]:
        L.append(f"  (already paid toward {p['for']}: {p['label']} {pkr(p['amount'])}; not added again)")
    for x in bu["excluded"]:
        L.append(f"  NOT BUDGETED: {x['label']} {pkr(x['amount'])} - {x['reason']}")
    L.append(f"  TOTAL {pkr(bu['total'])}   CAP {pkr(bu['cap'])}   REMAINING {pkr(bu['remaining'])}")
    L.append("")
    L.append("CHECKS")
    for c in val["checks"]:
        L.append(f"  [{c['status'].upper():<4}] {c['label']}: {c['detail']}")
    if plan.get("assumptions"):
        L.append("")
        L.append("ASSUMPTIONS (flagged, not facts from the messages)")
        for a in plan["assumptions"]:
            L.append("  - " + a)
    if plan.get("unknown"):
        L.append("UNKNOWN INPUTS")
        for u in plan["unknown"]:
            L.append(f"  - {u['item']}: {u['why']}" + (" (blocking)" if u["blocking"] else ""))
    return "\n".join(L)

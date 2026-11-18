"""Independent validator. It re-derives every headline number from the plan's own
line items and the accepted facts (it does not call the planner's solving
functions) and asserts the six organizer checks plus every typed hard rule.

Statuses: pass | fail | warn | na.  A plan is `valid` only when nothing fails and
nothing hard is unverified; otherwise it is `provisional` (never silently valid).
"""
from __future__ import annotations

from .util import ceil_div, fmt_offset, norm_entity, parse_clock, parse_offset, same_entity, tokens

AREAS = ("venue", "schedule", "food", "judging", "budget")

_PRAYER = {"prayer", "namaz", "salah", "salat", "zuhr", "dhuhr", "jumma", "jummah", "jumuah"}
_LUNCH = {"lunch", "meal", "meals", "food", "khana", "catering"}
_JUDGE = {"judging", "judge", "judges", "evaluation", "demo", "demos", "showcase", "presentations",
          "presentation", "pitching", "jamming"}
_SUBMIT = {"submission", "submit", "freeze", "deadline"}
FEATURE_ALIAS = {"backup power": "generator", "power backup": "generator", "standby generator": "generator",
                 "backup generator": "generator", "ups": "generator", "wi-fi": "wifi", "wireless": "wifi",
                 "air conditioning": "ac", "air conditioned": "ac", "a/c": "ac", "projectors": "projector"}


def activity_kind(name) -> str:
    t = set(tokens(name))
    if t & _PRAYER:
        return "prayer"
    if t & _LUNCH:
        return "lunch"
    if t & _JUDGE:
        return "judging"
    if t & _SUBMIT:
        return "submission"
    if "break" in t:
        return "break"
    return "other"


def norm_feature(f) -> str:
    f = " ".join(str(f).lower().split())
    return FEATURE_ALIAS.get(f, f)


def to_offset(x, start_clock):
    """'+45' -> 45 ; 'HH:MM' -> minutes after event start (needs start_clock) ; else None."""
    if x is None:
        return None
    o = parse_offset(x)
    if o is not None:
        return o
    c = parse_clock(x)
    if c is not None and start_clock is not None:
        return c - start_clock
    return None


def match_segments(segs, activity):
    k = activity_kind(activity)
    out = []
    for s in segs:
        if s["kind"] in ("build",):
            continue
        sk = s.get("canon") or activity_kind(s["label"])
        if k in ("prayer", "break") and sk in ("prayer", "break") and (k == sk or k == "break" or sk == "break"):
            out.append(s)
        elif k not in ("prayer", "break", "other") and sk == k:
            out.append(s)
        elif k == "other" and (same_entity(activity, s["label"]) or same_entity(activity, s.get("name"))):
            out.append(s)
    return out


def schedule_violations(segs, facts, start_clock):
    """Evaluate sched.window / sched.order facts against laid-out segments.
    Returns (violations, unverifiable) lists of strings."""
    bad, unver = [], []
    judging = [s for s in segs if s["kind"] == "judging"]
    for ent, it in sorted((facts["multi"].get("sched.window") or {}).items(), key=lambda kv: str(kv[0])):
        w = it["v"]
        lo, hi = to_offset(w.get("from"), start_clock), to_offset(w.get("to"), start_clock)
        need_clock = [x for x in (w.get("from"), w.get("to")) if x is not None and parse_offset(x) is None]
        if need_clock and start_clock is None:
            unver.append(f"window for '{ent}' uses clock times but the event start time is not stated")
            continue
        k = activity_kind(ent)
        if k == "submission":
            if not judging:
                unver.append(f"'{ent}' deadline: no judging segment to anchor the submission cut-off")
                continue
            t = judging[0]["start"]
            if hi is not None and t > hi:
                bad.append(f"submission cut-off at {fmt_offset(t)} is after {fmt_offset(hi)} ({ent})")
            if lo is not None and t < lo:
                bad.append(f"submission cut-off at {fmt_offset(t)} is before {fmt_offset(lo)} ({ent})")
            continue
        ms = match_segments(segs, ent)
        if not ms:
            unver.append(f"no scheduled activity matches '{ent}', so its window cannot be checked")
            continue
        for s in ms:
            rule = w.get("rule", "within")
            if rule == "within":
                if lo is not None and s["start"] < lo:
                    bad.append(f"{s['label']} starts {fmt_offset(s['start'])}, before the allowed start {fmt_offset(lo)}")
                if hi is not None and s["end"] > hi:
                    bad.append(f"{s['label']} ends {fmt_offset(s['end'])}, after the allowed end {fmt_offset(hi)}")
            elif rule == "fixed":
                if lo is not None and s["start"] != lo:
                    bad.append(f"{s['label']} starts {fmt_offset(s['start'])}, but must start at {fmt_offset(lo)}")
            elif rule == "not_overlap":
                if lo is not None and hi is not None and s["start"] < hi and s["end"] > lo:
                    bad.append(f"{s['label']} ({fmt_offset(s['start'])}-{fmt_offset(s['end'])}) overlaps the forbidden window {fmt_offset(lo)}-{fmt_offset(hi)}")
    for ent, it in sorted((facts["multi"].get("sched.order") or {}).items(), key=lambda kv: str(kv[0])):
        first, then = it["ent"], it["v"]
        a, b = match_segments(segs, first), match_segments(segs, then)
        if not a or not b:
            unver.append(f"order rule '{first}' before '{then}': one of the activities is not scheduled")
            continue
        if min(s["start"] for s in b) < max(s["end"] for s in a) and not (first == then):
            bad.append(f"'{first}' must come before '{then}' but the schedule has them overlapping or reversed")
    return bad, unver


# ------------------------------------------------------- typed custom rules --

_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "==": lambda a, b: a == b,
        ">": lambda a, b: a > b, "<": lambda a, b: a < b}


def metric_value(plan, metric):
    ev, v, f, j, s, b = plan["event"], plan["venue"], plan["food"], plan["judging"], plan["schedule"], plan["budget"]
    return {
        "students": ev.get("students"), "teams": ev.get("teams"), "staff": ev.get("staff"),
        "volunteers": ev.get("volunteers"), "judges_assigned": j.get("deployed"),
        "judges_confirmed": j.get("confirmed"), "rounds": j.get("rounds"),
        "judging_minutes": (j["end_min"] - j["start_min"]) if j.get("end_min") is not None else None,
        "food_qty": f.get("qty"), "total_cost": b.get("total"), "venue_capacity": v.get("capacity"),
        "build_minutes": s.get("build_minutes"),
        "break_minutes": sum(x["min"] for x in ev.get("breaks", [])),
    }.get(metric)


def evaluate_rule(r, plan):
    """-> (status, detail) with status in pass|fail|unverifiable. Closed operators only; no eval."""
    t = r.get("type")
    if t == "quantity":
        val = metric_value(plan, r.get("metric"))
        if val is None:
            return "unverifiable", f"{r.get('metric')} is unknown in this plan"
        ok = _OPS[r["op"]](val, r["value"])
        return ("pass" if ok else "fail"), f"{r['metric']}={val} must be {r['op']} {r['value']}"
    if t == "feature":
        feats = {norm_feature(x) for x in plan["venue"].get("features", [])}
        need = norm_feature(r.get("feature", ""))
        return ("pass" if need in feats else "fail"), f"venue feature '{need}' {'present' if need in feats else 'missing'}"
    if t == "exclusion":
        item = r.get("item", "")
        hit = [l["label"] for l in plan["budget"]["lines"] if same_entity(item, l["label"])]
        return ("fail" if hit else "pass"), (f"excluded item '{item}' is budgeted as {hit}" if hit else f"'{item}' is not budgeted")
    if t == "eligibility":
        judge, rule, team = r.get("judge"), r.get("rule"), r.get("team")
        if not judge or not team:
            return "unverifiable", "eligibility rule needs both a judge and a team"
        tn = "".join(ch for ch in str(team) if ch.isdigit())
        if not tn:
            return "unverifiable", f"team '{team}' cannot be mapped to a team number"
        hits = [a for a in plan["judging"].get("assignments", []) if str(a["team"]) == tn and any(same_entity(judge, x) for x in a["judges"])]
        if rule == "cannot_judge":
            return ("fail" if hits else "pass"), f"{judge} {'is' if hits else 'is not'} assigned to team {tn}"
        return ("pass" if hits else "fail"), f"{judge} {'is' if hits else 'is not'} assigned to team {tn}"
    if t == "conditional":
        cond = r.get("if") or {}
        st, det = evaluate_rule(cond, plan) if cond.get("type") == "quantity" else ("unverifiable", "unsupported condition")
        if st == "unverifiable":
            return "unverifiable", "condition cannot be evaluated: " + det
        if st == "fail":
            return "pass", f"condition not met ({det}); rule does not apply"
        return evaluate_rule(r.get("then") or {"type": "opaque"}, plan)
    if t in ("window", "order"):
        return "unverifiable", "evaluated by the schedule check"
    return "unverifiable", "rule type cannot be checked automatically: " + str(r.get("text", ""))[:120]


# ------------------------------------------------------------------ checks --

def _fv(facts, k, d=None):
    it = facts["single"].get(k)
    return it["v"] if it else d


def validate(plan, facts, cfg=None):
    checks = []

    def add(cid, label, status, detail, ids=None, items=None):
        checks.append({"id": cid, "label": label, "status": status, "detail": detail,
                       "evidence": ids or [], "items": items or []})

    ev, ve, sc, fo, ju, bu = (plan[k] for k in ("event", "venue", "schedule", "food", "judging", "budget"))
    cap = _fv(facts, "event.budget_cap")

    # 1 figures ---------------------------------------------------------------
    errs = []
    total = 0
    for l in bu["lines"]:
        if l.get("qty") is not None and l.get("rate") is not None and l["amount"] != l["qty"] * l["rate"]:
            errs.append(f"{l['label']}: {l['qty']} x {l['rate']} != {l['amount']}")
        if l.get("amount") is not None:
            total += l["amount"]
    if total != bu["total"]:
        errs.append(f"budget total {bu['total']} != sum of lines {total}")
    if cap is not None and bu.get("remaining") != cap - total:
        errs.append(f"remaining {bu.get('remaining')} != cap {cap} - total {total}")
    S, size, tm = ev.get("students"), ev.get("team_size"), ev.get("teams")
    if S is not None and size and tm is not None and _fv(facts, "event.teams") is None and ceil_div(S, size) != tm:
        errs.append(f"teams {tm} != ceil({S}/{size})")
    parts = fo.get("fed_parts", {})
    if fo.get("fed") is not None and sum(parts.values()) != fo["fed"]:
        errs.append(f"meal headcount {fo['fed']} != sum of groups {sum(parts.values())}")
    if fo.get("lines") and sum(x["subtotal"] for x in fo["lines"]) != sum(l["amount"] for l in bu["lines"] if l["area"] == "food" and l.get("kind") == "food"):
        errs.append("food line subtotals do not match the food budget lines")
    cp, pn, mx = ju.get("teams"), ju.get("panel"), ju.get("max_teams")
    if cp and mx and ju.get("min_needed") != max(pn, ceil_div(cp * pn, mx)):
        errs.append(f"minimum judges {ju.get('min_needed')} != max({pn}, ceil({cp}x{pn}/{mx}))")
    add("figures", "Figures are arithmetically correct", "fail" if errs else "pass",
        "; ".join(errs) if errs else "line totals, grand total, remaining funds, team count and meal headcount all recompute exactly")

    # 2 budget ----------------------------------------------------------------
    if cap is None:
        add("budget", "Within budget", "fail", "no budget cap is stated in the accepted facts")
    elif bu["total"] > cap:
        add("budget", "Within budget", "fail", f"total Rs {bu['total']:,} exceeds the cap Rs {cap:,} by Rs {bu['total'] - cap:,}",
            facts["single"]["event.budget_cap"]["ids"])
    elif bu.get("unknown"):
        add("budget", "Within budget", "fail", "total is only a lower bound: " + "; ".join(u["item"] for u in bu["unknown"]))
    else:
        add("budget", "Within budget", "pass", f"total Rs {bu['total']:,} <= cap Rs {cap:,}; Rs {cap - bu['total']:,} remaining",
            facts["single"]["event.budget_cap"]["ids"])

    # 3 venue -----------------------------------------------------------------
    problems = []
    if not ve.get("name"):
        problems.append("no venue chosen")
    else:
        need = (ev.get("students") or 0) + (ev.get("staff") or 0) + (ev.get("volunteers") or 0) + (ju.get("deployed") or 0)
        if ve.get("capacity") is None:
            problems.append("venue capacity unknown")
        elif ve["capacity"] < need:
            problems.append(f"capacity {ve['capacity']} < {need} people on site")
        have = {norm_feature(x) for x in ve.get("features", [])}
        miss = [r for r in ve.get("required_features", []) if norm_feature(r) not in have]
        if miss:
            problems.append("missing required features: " + ", ".join(miss))
        if ve.get("hours_ok") is False:
            problems.append(ve.get("hours_note") or "venue hours do not cover the event")
        if ve.get("headcount") != need:
            problems.append(f"plan headcount {ve.get('headcount')} != recomputed {need}")
    add("venue_fit", "Venue fits", "fail" if problems else "pass",
        "; ".join(problems) if problems else f"{ve['name']} seats {ve['capacity']} for {ve['headcount']} people on site; required features present",
        ve.get("evidence", []))

    # 4 schedule --------------------------------------------------------------
    segs = sc["segments"]
    sp = []
    D = sc["duration_min"]
    if not segs:
        sp.append("no schedule produced")
    else:
        cur = 0
        for s in segs:
            if s["start"] != cur:
                sp.append(f"gap or overlap before {s['label']} (expected {cur}, found {s['start']})")
            if s["end"] - s["start"] != s["dur"] or s["dur"] < 0:
                sp.append(f"{s['label']} duration mismatch")
            cur = s["end"]
        if D is None or cur != D:
            sp.append(f"schedule ends at {cur} but the event lasts {D} minutes")
        for b in ev.get("breaks", []):
            seg = [s for s in segs if s["kind"] == "break" and s.get("name") == b["name"]]
            if not seg or seg[0]["dur"] < b["min"]:
                sp.append(f"required {b['name']} break of {b['min']} minutes is missing or too short")
        if sc.get("build_minutes", 0) < (cfg.min_build_min if cfg else 0):
            sp.append(f"only {sc.get('build_minutes')} minutes of build time (policy minimum {cfg.min_build_min})")
        bad, unver = schedule_violations(segs, facts, parse_clock(ev.get("start_clock")) if ev.get("start_clock") else None)
        sp += bad
        j = [s for s in segs if s["kind"] == "judging"]
        if j and ju.get("assignments"):
            if any(a["start"] < j[0]["start"] or a["end"] > j[0]["end"] for a in ju["assignments"]):
                sp.append("a judging slot falls outside the judging segment")
    add("schedule_valid", "Schedule is valid", "fail" if sp else "pass",
        "; ".join(sp) if sp else f"{len(segs)} contiguous segments fill exactly {D} minutes; breaks, windows and order rules hold")

    # 5 judging ---------------------------------------------------------------
    jp = []
    teams = ju.get("teams") or 0
    byteam, slots = {}, {}
    for a in ju.get("assignments", []):
        byteam.setdefault(a["team"], []).append(a)
        for jn in a["judges"]:
            if (jn, a["start"]) in slots:
                jp.append(f"{jn} is double-booked at {fmt_offset(a['start'])}")
            slots[(jn, a["start"])] = a["team"]
        if a["end"] - a["start"] < (ju.get("slot_min") or 0):
            jp.append(f"team {a['team']} slot shorter than {ju.get('slot_min')} minutes")
        if len(set(a["judges"])) < (ju.get("panel") or 1):
            jp.append(f"team {a['team']} has fewer than {ju.get('panel')} distinct judges")
    missing = [t for t in range(1, teams + 1) if t not in byteam]
    if missing:
        jp.append(f"{len(missing)} team(s) have no judging slot (first: team {missing[0]})")
    multi = [t for t, v in byteam.items() if len(v) != 1]
    if multi:
        jp.append(f"teams judged more than once: {multi[:5]}")
    load = {}
    for (jn, _), _t in slots.items():
        load[jn] = load.get(jn, 0) + 1
    if ju.get("max_teams") and any(v > ju["max_teams"] for v in load.values()):
        worst = max(load.items(), key=lambda kv: kv[1])
        jp.append(f"{worst[0]} judges {worst[1]} teams, above the limit {ju['max_teams']}")
    if ju.get("confirmed") is not None and len(load) > ju["confirmed"]:
        jp.append(f"{len(load)} judges used but only {ju['confirmed']} confirmed")
    if ju.get("confirmed") is None:
        jp.append("number of confirmed judges is not stated")
    if ju.get("slot_min") is None:
        jp.append("minutes per team is not stated")
    add("judging_coverage", "Enough judges, every team covered", "fail" if jp else "pass",
        "; ".join(jp) if jp else f"all {teams} teams have {ju['panel']} judge(s) in a {ju['slot_min']}-minute slot; {len(load)} judges used "
        f"(max {max(load.values()) if load else 0} teams each, limit {ju.get('max_teams') or 'none stated'}); no double-booking",
        ju.get("evidence", []))

    # 6 food coverage -----------------------------------------------------------
    fp = []
    need = (ev.get("students") or 0) + (ev.get("staff") or 0) + (ev.get("volunteers") or 0) + (ju.get("deployed") or 0)
    inc = {norm_entity(e): it["v"] for e, it in (facts["multi"].get("food.include") or {}).items()}
    for g, n in (("judges", ju.get("deployed") or 0), ("staff", ev.get("staff") or 0), ("volunteers", ev.get("volunteers") or 0)):
        if inc.get(g) is False:
            need -= n
    if fo.get("qty") is None:
        fp.append("no meal quantity could be planned (the headcount or a menu price is unknown)")
    elif fo["qty"] < need:
        fp.append(f"{fo['qty']} meals planned or ordered for {need} people")
    claim = fo.get("claim_qty")
    if claim is not None and claim < need:
        fp.append(f"a stated order of {claim} meals is below the {need} required")
    if fo.get("min_order") and fo.get("qty") is not None and fo["qty"] < fo["min_order"]:
        fp.append(f"order below the caterer minimum {fo['min_order']}")
    for dname, it in sorted((facts["multi"].get("food.diet") or {}).items(), key=lambda kv: str(kv[0])):
        v = it["v"]
        want = v.get("min") if v.get("min") is not None else ceil_div((v.get("pct") or 0) * (fo.get("qty") or 0), 100)
        got = sum(x["qty"] for x in fo.get("lines", []) if norm_entity(dname) in [norm_entity(t) for t in x.get("diet", [])])
        if got < want:
            fp.append(f"{dname} meals {got} < required {want}")
    if fo.get("fed") != need:
        fp.append(f"plan headcount {fo.get('fed')} != recomputed {need}")
    add("food_coverage", "Food covers everyone who must be fed", "fail" if fp else "pass",
        "; ".join(fp) if fp else f"{fo['qty']} meals for {need} people ({', '.join(f'{k} {v}' for k, v in parts.items() if v)}); dietary minima met",
        fo.get("evidence", []))

    # 7 areas -------------------------------------------------------------------
    miss = []
    if not ve.get("name"):
        miss.append("venue")
    if not segs:
        miss.append("schedule")
    if not fo.get("lines"):
        miss.append("food")
    if not ju.get("assignments"):
        miss.append("judging")
    if not bu.get("lines"):
        miss.append("budget")
    add("areas_complete", "All five areas covered", "fail" if miss else "pass",
        ("missing: " + ", ".join(miss)) if miss else "venue, schedule, food, judging and budget are all present")

    # 8 hard rules ---------------------------------------------------------------
    rules, rfail, runv = [], [], []
    for ent, it in sorted((facts["multi"].get("rule.custom") or {}).items(), key=lambda kv: str(kv[0])):
        st, det = evaluate_rule(it["v"], plan)
        rules.append({"label": ent, "status": st, "detail": det, "evidence": it["ids"], "rule": it["v"]})
        (rfail if st == "fail" else runv if st == "unverifiable" else []).append(f"{ent}: {det}")
    if (it := facts["multi"].get("venue.require")):
        have = {norm_feature(x) for x in ve.get("features", [])}
        for ent, x in sorted(it.items(), key=lambda kv: str(kv[0])):
            ok = norm_feature(ent) in have
            rules.append({"label": f"venue requires {ent}", "status": "pass" if ok else "fail",
                          "detail": f"{ve.get('name')} {'has' if ok else 'lacks'} {ent}", "evidence": x["ids"], "rule": {"type": "feature"}})
            if not ok:
                rfail.append(f"venue lacks {ent}")
    bad, unver = schedule_violations(segs, facts, parse_clock(ev["start_clock"]) if ev.get("start_clock") else None) if segs else ([], [])
    runv += unver
    for u in facts.get("unverified", []):
        runv.append(f"{u['label']}: {u['why']}")
    blocking_unknown = [u for u in plan.get("unknown", []) if u.get("blocking")]
    for u in blocking_unknown:
        runv.append(f"unknown required input: {u['item']} ({u['why']})")
    if rfail:
        add("hard_rules", "Every stated hard rule holds", "fail", "; ".join(rfail), items=rules)
    elif runv:
        add("hard_rules", "Every stated hard rule holds", "warn",
            "cannot be verified, so the plan is not unconditionally valid: " + "; ".join(runv), items=rules)
    else:
        add("hard_rules", "Every stated hard rule holds", "pass",
            f"{len(rules)} typed rule(s) checked, none violated" if rules else "no extra hard rules beyond the five areas", items=rules)

    # 9 discrepancies ------------------------------------------------------------
    if plan.get("discrepancies"):
        add("consistency", "Source facts agree with each other", "warn", "; ".join(d["detail"] for d in plan["discrepancies"]))
    else:
        add("consistency", "Source facts agree with each other", "pass", "no conflicting counts detected")

    failed = [c["id"] for c in checks if c["status"] == "fail"]
    warned = [c["id"] for c in checks if c["status"] == "warn"]
    grade = "valid" if not failed and not [w for w in warned if w == "hard_rules"] else "provisional"
    return {"checks": checks, "failed": failed, "warned": warned, "grade": grade,
            "passed": sum(1 for c in checks if c["status"] == "pass"), "total": len(checks)}

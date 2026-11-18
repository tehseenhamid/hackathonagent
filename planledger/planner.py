"""Deterministic planner: venue, schedule, food, judging and budget solved together.

All arithmetic is exact integer maths. Candidate plans (judge count x venue x menu
item) are generated, each is checked by the independent validator, and the best
valid one is chosen with documented tie-breaks:

  1. fewest judges that make the whole plan valid (more judges only add cost and meals)
  2. the preferred menu item (an explicit choice, otherwise the first-quoted item;
     cheaper alternatives are reported with their saving, never silently swapped in)
  3. lowest total cost, then lowest venue rent, then name
Nothing here calls a model, and nothing here contains a fact from any message pack.
"""
from __future__ import annotations

import itertools
import time

from .util import ceil_div, fmt_clock, norm_entity, parse_clock, same_entity
from .validator import (AREAS, activity_kind, match_segments, norm_feature, schedule_violations,
                        to_offset, validate)


def fv(facts, key, default=None):
    it = facts["single"].get(key)
    return it["v"] if it else default


def fids(facts, key):
    it = facts["single"].get(key)
    return list(it["ids"]) if it else []


def multi(facts, key):
    return facts["multi"].get(key) or {}


# ---------------------------------------------------------------- basics --

def basics(facts, cfg):
    b = {"unknown": [], "assumptions": [], "discrepancies": []}

    def unk(item, why, blocking=True):
        b["unknown"].append({"item": item, "why": why, "blocking": blocking})

    S, size, tm = fv(facts, "event.students"), fv(facts, "event.team_size"), fv(facts, "event.teams")
    b["students"] = S
    if S is None:
        unk("number of students", "no accepted statement gives the number of participants")
    b["team_size"] = size
    if tm is not None:
        b["teams"], b["teams_basis"] = tm, "stated"
        if S and size and ceil_div(S, size) != tm:
            b["discrepancies"].append({"detail": f"{tm} teams stated but {S} students in teams of {size} gives {ceil_div(S, size)}",
                                       "ids": fids(facts, "event.teams") + fids(facts, "event.students")})
    elif S and size:
        b["teams"], b["teams_basis"] = ceil_div(S, size), f"ceil({S} students / {size} per team)"
    else:
        b["teams"], b["teams_basis"] = None, None
        unk("number of teams", "neither a team count nor a team size is stated")
    b["staff"] = fv(facts, "event.staff")
    if b["staff"] is None:
        b["staff"] = 0
        b["assumptions"].append("Staff headcount is not stated in the accepted messages: 0 assumed (flagged, affects venue and meals).")
    b["volunteers"] = fv(facts, "event.volunteers") or 0
    D = fv(facts, "event.duration_min")
    sc, ec = fv(facts, "event.start_clock"), fv(facts, "event.end_clock")
    if D is None and sc and ec:
        D = (parse_clock(ec) - parse_clock(sc)) % (24 * 60)
    elif D is not None and sc and ec and (parse_clock(ec) - parse_clock(sc)) % (24 * 60) != D:
        b["discrepancies"].append({"detail": f"duration {D} min disagrees with start {sc} / end {ec}",
                                   "ids": fids(facts, "event.duration_min")})
    b["duration"] = D
    if D is None:
        unk("event duration", "no accepted statement gives the length of the event")
    b["start_clock"] = sc
    b["breaks"] = [{"name": e, "min": it["v"], "ids": it["ids"]} for e, it in sorted(multi(facts, "event.break_min").items(), key=lambda kv: str(kv[0]))]
    b["panel"] = fv(facts, "judge.panel")
    if b["panel"] is None:
        b["panel"] = 1
        b["assumptions"].append("Judges per team is not stated: one judge per team assumed.")
    b["slot"] = fv(facts, "judge.minutes")
    if b["slot"] is None:
        unk("judging minutes per team", "no accepted statement gives the time each team needs")
    b["max_teams"] = fv(facts, "judge.max_teams")
    if b["max_teams"] is None:
        b["assumptions"].append("No per-judge team limit is stated: none applied.")
    b["confirmed"] = fv(facts, "judge.confirmed")
    if b["confirmed"] is None:
        unk("confirmed judges", "no accepted statement gives how many judges are available")
    b["fee"] = fv(facts, "judge.fee")
    if b["fee"] is None:
        b["assumptions"].append("No judge honorarium is stated: Rs 0 assumed (flagged in the budget).")
        b["unknown"].append({"item": "judge honorarium", "why": "not stated; Rs 0 assumed", "blocking": False})
    cap = fv(facts, "event.budget_cap")
    if cap is None:
        unk("budget cap", "no accepted statement gives the total budget")
    P, mx, T = b["panel"], b["max_teams"], b["teams"]
    if T:
        b["min_judges"] = max(P, ceil_div(T * P, mx) if mx else P)
    else:
        b["min_judges"] = P
    named = sorted(multi(facts, "judge.avail").keys(), key=str)
    b["named_judges"] = [n for n in named if n]
    return b


# ---------------------------------------------------------- judging maths --

def judge_pool(b, j):
    names = list(b["named_judges"][:j])
    k = 1
    while len(names) < j:
        n = f"Judge {k}"
        k += 1
        if n not in names:
            names.append(n)
    return names


def avail_windows(facts, start_clock):
    out = {}
    for ent, it in multi(facts, "judge.avail").items():
        v = it["v"]
        out[ent] = {"absent": v.get("absent", False), "lo": to_offset(v.get("from"), start_clock),
                    "hi": to_offset(v.get("to"), start_clock)}
    return out


def assign_judging(teams, pool, panel, slot, cap, avail, start_off, max_rounds):
    """Round-by-round assignment: each round takes the least-loaded available judges."""
    loads = {n: 0 for n in pool}
    order = {n: i for i, n in enumerate(pool)}
    assignments, team, rnd = [], 1, 0
    while team <= teams and rnd < max_rounds:
        t0, t1 = start_off + rnd * slot, start_off + (rnd + 1) * slot
        free = []
        for n in pool:
            a = next((v for e, v in avail.items() if same_entity(e, n)), None)
            if a:
                if a["absent"] or (a["lo"] is not None and t0 < a["lo"]) or (a["hi"] is not None and t1 > a["hi"]):
                    continue
            if cap is not None and loads[n] >= cap:
                continue
            free.append(n)
        free.sort(key=lambda n: (loads[n], order[n]))
        lanes = min(len(free) // panel, teams - team + 1)
        for lane in range(lanes):
            js = free[lane * panel:(lane + 1) * panel]
            for n in js:
                loads[n] += 1
            assignments.append({"team": team, "round": rnd + 1, "start": t0, "end": t1, "judges": js})
            team += 1
        rnd += 1
    return {"assignments": assignments, "rounds": rnd, "loads": loads, "unassigned": list(range(team, teams + 1))}


# ------------------------------------------------------------------ layout --

def make_items(b, facts, judging_dur, cfg):
    items = []
    for br in b["breaks"]:
        items.append({"id": f"break:{br['name']}", "label": f"{str(br['name']).title()} break", "kind": "break",
                      "canon": activity_kind(br["name"]), "name": br["name"], "dur": br["min"]})
    lunch = cfg.lunch_min
    explicit = None
    for ent, it in multi(facts, "sched.window").items():
        if activity_kind(ent) == "lunch" and it["v"].get("dur") is not None:
            explicit = it["v"]["dur"]
    if explicit is not None:
        lunch = explicit
    items.append({"id": "lunch", "label": "Lunch", "kind": "lunch", "canon": "lunch", "name": "lunch", "dur": lunch,
                  "assumed": explicit is None})
    for ent, it in sorted(multi(facts, "sched.window").items(), key=lambda kv: str(kv[0])):
        k = activity_kind(ent)
        if k == "other" and it["v"].get("dur"):
            items.append({"id": f"custom:{norm_entity(ent)}", "label": str(ent).title(), "kind": "custom",
                          "canon": "other", "name": ent, "dur": it["v"]["dur"]})
    items.append({"id": "judging", "label": "Judging", "kind": "judging", "canon": "judging", "name": "judging",
                  "dur": judging_dur})
    return items


def item_constraints(items, facts, start_clock):
    cons = {i["id"]: {"lo": None, "hi": None, "forbid": []} for i in items}
    for ent, it in multi(facts, "sched.window").items():
        w = it["v"]
        if activity_kind(ent) == "submission":
            continue
        lo, hi = to_offset(w.get("from"), start_clock), to_offset(w.get("to"), start_clock)
        for s in match_segments(items, ent):
            c = cons[s["id"]]
            rule = w.get("rule", "within")
            if rule == "within":
                if lo is not None:
                    c["lo"] = lo if c["lo"] is None else max(c["lo"], lo)
                if hi is not None:
                    c["hi"] = hi if c["hi"] is None else min(c["hi"], hi)
            elif rule == "fixed" and lo is not None:
                c["lo"], c["hi"] = lo, lo + s["dur"]
            elif rule == "not_overlap" and lo is not None and hi is not None:
                c["forbid"].append((lo, hi))
    return cons


def _place(order, D, B, cons):
    segs, cursor, left, n = [], 0, B, len(order)
    for k, item in enumerate(order):
        share = left // (n - k)
        start = cursor + share
        c = cons.get(item["id"], {})
        lo, hi = c.get("lo"), c.get("hi")
        if lo is not None:
            start = max(start, lo)
        if hi is not None:
            start = min(start, hi - item["dur"])
        start = max(start, cursor)
        for _ in range(4):
            for fa, fb in c.get("forbid", []):
                if start < fb and start + item["dur"] > fa:
                    start = fb
        gap = start - cursor
        if gap > left:
            return None
        if gap > 0:
            segs.append({"id": f"build:{len(segs)}", "label": "Build time", "kind": "build", "canon": "build",
                         "name": "build", "start": cursor, "end": start, "dur": gap})
        left -= gap
        segs.append({"id": item["id"], "label": item["label"], "kind": item["kind"], "canon": item["canon"],
                     "name": item["name"], "start": start, "end": start + item["dur"], "dur": item["dur"]})
        cursor = start + item["dur"]
    if left > 0:
        segs.append({"id": f"build:{len(segs)}", "label": "Build time", "kind": "build", "canon": "build",
                     "name": "build", "start": cursor, "end": cursor + left, "dur": left})
    return segs


def layout(D, items, facts, start_clock, deadline):
    total = sum(i["dur"] for i in items)
    B = D - total
    default = list(items)
    seq, cursor = [], 0
    for it in default:  # sequential fallback (also used when the event is too short)
        seq.append({"id": it["id"], "label": it["label"], "kind": it["kind"], "canon": it["canon"], "name": it["name"],
                    "start": cursor, "end": cursor + it["dur"], "dur": it["dur"]})
        cursor += it["dur"]
    if B < 0:
        return seq, False
    cons = item_constraints(items, facts, start_clock)
    orders = [default]
    if len(items) <= 6:
        orders += [list(p) for p in itertools.permutations(items) if list(p) != default]
    first = None
    for order in orders:
        if time.monotonic() > deadline:
            break
        segs = _place(order, D, B, cons)
        if segs is None:
            continue
        first = first or segs
        bad, _ = schedule_violations(segs, facts, start_clock)
        if not bad:
            return segs, True
    return (first or seq), False


# ------------------------------------------------------------------ venues --

def venue_info(ent, it, D, start_clock):
    v = it["v"]
    info = {"name": ent, "capacity": v.get("cap"), "features": sorted(v.get("feat", [])), "open": v.get("open"),
            "close": v.get("close"), "per": v.get("per", "flat"), "rate": v.get("rent"), "ids": it["ids"],
            "rent_total": None, "hours_ok": None, "hours_note": None}
    if v.get("rent") is not None and D is not None:
        info["rent_total"] = v["rent"] * ceil_div(D, 60) if info["per"] == "hour" else v["rent"]
    if v.get("open") and v.get("close") and D is not None:
        o, c = parse_clock(v["open"]), parse_clock(v["close"])
        if start_clock is not None:
            s = start_clock
            info["hours_ok"] = (o <= s and s + D <= c)
            info["hours_note"] = f"{ent} is open {v['open']}-{v['close']}; the event runs {fmt_clock(s)}-{fmt_clock(s + D)}"
        else:
            info["hours_ok"] = (c - o) >= D
            info["hours_note"] = f"{ent} is open only {c - o} minutes ({v['open']}-{v['close']}), shorter than the {D}-minute event"
    return info


def venue_reasons(vi, need, required):
    why = []
    if vi["capacity"] is None:
        why.append("capacity not quoted")
    elif vi["capacity"] < need:
        why.append(f"capacity {vi['capacity']} < {need} people on site")
    if vi["rent_total"] is None:
        why.append("rent not quoted")
    have = {norm_feature(x) for x in vi["features"]}
    miss = [r for r in required if norm_feature(r) not in have]
    if miss:
        why.append("lacks required " + ", ".join(miss))
    if vi["hours_ok"] is False:
        why.append(vi["hours_note"])
    return why


# -------------------------------------------------------------------- food --

def food_options(facts):
    opts = []
    for ent, it in multi(facts, "food.option").items():
        v = it["v"]
        if v.get("price") is None:
            continue
        opts.append({"name": ent, "price": v["price"], "per": v.get("per", "head"), "deliv": v.get("deliv"),
                     "min": v.get("min") or 0, "diet": v.get("diet", []), "ids": it["ids"], "first": it.get("first", (9, 9))})
    pref = [e for e in multi(facts, "food.choice").keys()]
    opts.sort(key=lambda o: (0 if any(same_entity(p, o["name"]) for p in pref) else 1, o["first"], o["price"], str(o["name"])))
    return opts


# ----------------------------------------------------------------- assemble --

def assemble(facts, b, cfg, j, vi, fopt, jplan, opts_all):
    S, staff, vol = b["students"] or 0, b["staff"], b["volunteers"]
    D = b["duration"]
    need_site = S + staff + vol + j
    inc = {norm_entity(e): it["v"] for e, it in multi(facts, "food.include").items()}
    parts = {"students": S, "staff": staff, "volunteers": vol, "judges": j}
    for g in ("judges", "staff", "volunteers"):
        if inc.get(g) is False:
            parts[g] = 0
    fed = sum(parts.values())
    claim = fv(facts, "food.order_qty")
    qty = claim if claim is not None else fed
    qty = max(qty, fopt["min"] if fopt else 0)

    plan = {"version": 1}
    plan["event"] = {"students": b["students"], "team_size": b["team_size"], "teams": b["teams"], "teams_basis": b["teams_basis"],
                     "staff": staff, "volunteers": vol, "duration_min": D, "start_clock": b["start_clock"],
                     "breaks": [{"name": x["name"], "min": x["min"]} for x in b["breaks"]]}
    required = [e for e in multi(facts, "venue.require").keys()]
    for ent, it in multi(facts, "rule.custom").items():
        if it["v"].get("type") == "feature" and it["v"].get("target", "venue") == "venue":
            required.append(it["v"].get("feature", ""))
    plan["venue"] = {"name": vi["name"] if vi else None, "capacity": vi["capacity"] if vi else None,
                     "rent_rate": vi["rate"] if vi else None, "rent_per": vi["per"] if vi else None,
                     "rent_total": vi["rent_total"] if vi else None, "features": vi["features"] if vi else [],
                     "open": vi["open"] if vi else None, "close": vi["close"] if vi else None,
                     "hours_ok": vi["hours_ok"] if vi else None, "hours_note": vi["hours_note"] if vi else None,
                     "headcount": need_site, "headcount_parts": {"students": S, "staff": staff, "volunteers": vol, "judges": j},
                     "required_features": sorted({norm_feature(x) for x in required}), "alternatives": [],
                     "evidence": vi["ids"] if vi else []}

    segs, ok = jplan["segments"], jplan["layout_ok"]
    jseg = next((s for s in segs if s["kind"] == "judging"), None)
    plan["schedule"] = {"duration_min": D, "segments": segs, "build_minutes": sum(s["dur"] for s in segs if s["kind"] == "build"),
                        "clock_known": bool(b["start_clock"]), "start_clock": b["start_clock"],
                        "judging_start": jseg["start"] if jseg else None, "notes": [], "layout_ok": ok}
    pool, sim = jplan["pool"], jplan["sim"]
    assigns = [{**a, "start": a["start"], "end": a["end"]} for a in sim["assignments"]]
    plan["judging"] = {"teams": b["teams"], "panel": b["panel"], "slot_min": b["slot"], "max_teams": b["max_teams"],
                       "confirmed": b["confirmed"], "deployed": j, "min_needed": b["min_judges"],
                       "reserve": (b["confirmed"] - j) if b["confirmed"] is not None else None,
                       "rounds": sim["rounds"], "start_min": jseg["start"] if jseg else None,
                       "end_min": jseg["end"] if jseg else None, "judges": pool, "assignments": assigns,
                       "loads": {n: v for n, v in sim["loads"].items() if v}, "policy": cfg.judge_policy,
                       "evidence": fids(facts, "judge.confirmed") + fids(facts, "judge.max_teams") + fids(facts, "judge.minutes")}

    lines, bud, unknown = [], [], list(b["unknown"])
    delivery_assumed = []
    diet_need = {}
    for dn, it in sorted(multi(facts, "food.diet").items(), key=lambda kv: str(kv[0])):
        v = it["v"]
        diet_need[dn] = v["min"] if v.get("min") is not None else ceil_div((v.get("pct") or 0) * qty, 100)
    used, rest = {}, qty
    for dn, n in diet_need.items():
        tagged = sorted([o for o in opts_all if norm_entity(dn) in [norm_entity(t) for t in o["diet"]]], key=lambda o: (o["price"], str(o["name"])))
        o = tagged[0] if tagged else fopt
        if o is None:
            continue
        used.setdefault((o["name"], tuple(o["diet"]) if tagged else (dn,)), [o, 0])[1] += n
        rest -= n
        if not tagged:
            plan.setdefault("_notes", []).append(f"No menu item is tagged {dn}: {n} meals assumed available at the {o['name']} price.")
    if fopt and rest > 0:
        used.setdefault((fopt["name"], ()), [fopt, 0])[1] += rest
    for (name, tags), (o, n) in sorted(used.items(), key=lambda kv: str(kv[0])):
        lines.append({"option": o["name"], "qty": n, "price": o["price"], "per": o["per"], "subtotal": n * o["price"],
                      "diet": list(tags), "evidence": o["ids"]})
    delivered = {}
    for ln in lines:
        o = next(x for x in opts_all if x["name"] == ln["option"])
        delivered[o["name"]] = o
    plan["food"] = {"fed_parts": parts, "fed": fed, "qty": qty if fopt else None, "claim_qty": claim,
                    "min_order": fopt["min"] if fopt else None, "lines": lines, "alternatives": [],
                    "option": fopt["name"] if fopt else None, "evidence": (fopt["ids"] if fopt else []) + fids(facts, "event.staff") + fids(facts, "event.students")}
    # ---- budget lines
    if vi and vi["rent_total"] is not None:
        hours = ceil_div(D, 60) if (vi["per"] == "hour" and D) else 1
        bud.append({"id": "venue", "area": "venue", "kind": "venue", "label": f"Venue: {vi['name']}", "qty": hours,
                    "rate": vi["rate"], "amount": vi["rent_total"],
                    "note": f"{vi['rate']:,} per hour x {hours} h" if vi["per"] == "hour" else "flat hire", "evidence": vi["ids"]})
    for i, ln in enumerate(lines):
        bud.append({"id": f"food:{i}", "area": "food", "kind": "food", "label": f"Meals: {ln['option']}", "qty": ln["qty"],
                    "rate": ln["price"], "amount": ln["subtotal"], "note": "per head", "evidence": ln["evidence"]})
    for name, o in sorted(delivered.items(), key=lambda kv: str(kv[0])):
        if o["deliv"] is None:
            delivery_assumed.append(name)
        elif o["deliv"] > 0:
            bud.append({"id": f"delivery:{name}", "area": "food", "kind": "delivery", "label": f"Delivery: {name}", "qty": 1,
                        "rate": o["deliv"], "amount": o["deliv"], "note": "flat delivery fee", "evidence": o["ids"]})
    if delivery_assumed:
        unknown.append({"item": "delivery fee for " + ", ".join(map(str, delivery_assumed)),
                        "why": "not stated; Rs 0 assumed", "blocking": False})
    if j and b["fee"] is not None:
        bud.append({"id": "honorarium", "area": "judging", "kind": "honorarium", "label": "Judge honorarium", "qty": j, "rate": b["fee"],
                    "amount": j * b["fee"], "note": f"{j} deployed judges (policy: {cfg.judge_policy})", "evidence": fids(facts, "judge.fee")})
    paid = []
    for ent, it in sorted(multi(facts, "cost.commitment").items(), key=lambda kv: str(kv[0])):
        v = it["v"]
        if v["for"] in ("venue", "food", "judging"):
            paid.append({"label": ent, "amount": v["amt"], "for": v["for"], "evidence": it["ids"]})
        else:
            bud.append({"id": f"commit:{norm_entity(ent)}", "area": "budget", "kind": "commitment", "label": str(ent).title(), "qty": None,
                        "rate": None, "amount": v["amt"], "note": "already committed from the event budget", "evidence": it["ids"]})
    for ent, it in sorted(multi(facts, "cost.fee").items(), key=lambda kv: str(kv[0])):
        v = it["v"]
        bud.append({"id": f"fee:{norm_entity(ent)}", "area": v["for"] if v["for"] != "other" else "budget", "kind": "fee",
                    "label": str(ent).title(), "qty": None, "rate": None, "amount": v["amt"], "note": "confirmed extra charge",
                    "evidence": it["ids"]})
    excluded = [{"label": str(it["ent"] or k), "amount": it["v"]["amt"], "reason": "request/quote not approved by anyone with authority",
                 "evidence": it["ids"]} for k, it in sorted(facts.get("requests", {}).items(), key=lambda kv: str(kv[0]))]
    total = sum(l["amount"] for l in bud)
    cap = fv(facts, "event.budget_cap")
    bud_unknown = []
    if vi is None:
        bud_unknown.append({"item": "venue cost (no venue option available)"})
    elif vi["rent_total"] is None:
        bud_unknown.append({"item": f"rent for {vi['name']}"})
    if fopt is None:
        bud_unknown.append({"item": "meal price (no priced menu option)"})
    plan["budget"] = {"lines": bud, "total": total, "cap": cap, "remaining": (cap - total) if cap is not None else None,
                      "paid_toward": paid, "excluded": excluded, "unknown": bud_unknown,
                      "lower_bound": bool(bud_unknown), "evidence": fids(facts, "event.budget_cap")}
    plan["unknown"] = unknown
    plan["assumptions"] = list(b["assumptions"]) + plan.pop("_notes", [])
    lunch_assumed = any(s["kind"] == "lunch" for s in segs) and not any(
        activity_kind(e) == "lunch" and it["v"].get("dur") is not None for e, it in multi(facts, "sched.window").items())
    if lunch_assumed:
        plan["assumptions"].append(f"No lunch duration is stated: a {cfg.lunch_min}-minute lunch slot is assumed in the schedule.")
    if fopt and len(opts_all) > 1 and not multi(facts, "food.choice"):
        plan["assumptions"].append(f"Menu item '{fopt['name']}' kept as the first-quoted item; alternatives are listed with their saving.")
    if not b["start_clock"]:
        plan["assumptions"].append("No event start time is stated: the schedule is shown as offsets from the start (T+h:mm); clock times are unconfirmed.")
    plan["assumptions"].append(
        f"Judges deployed = minimum needed ({b['min_judges']}) unless more are needed to fit the schedule; "
        f"confirmed-but-unused judges are reserves and are neither paid nor fed. Meals and honorarium follow the {j} deployed judges.")
    if b["confirmed"] is not None and b["confirmed"] != j:
        plan["discrepancies_info"] = f"{b['confirmed']} judges confirmed vs {j} deployed"
    plan["discrepancies"] = list(b["discrepancies"])
    return plan


def _cost_rank(plan):
    return plan["budget"]["total"]


# -------------------------------------------------------------------- solve --

def solve(facts, cfg, now=time.monotonic):
    """Return {plan, validation, candidates_checked, unsat_reason}. Bounded by cfg.solver_timeout_s."""
    deadline = now() + cfg.solver_timeout_s
    b = basics(facts, cfg)
    D = b["duration"] or 0
    venues_f = multi(facts, "venue.option")
    vinfos = [venue_info(e, it, b["duration"], parse_clock(b["start_clock"]) if b["start_clock"] else None)
              for e, it in sorted(venues_f.items(), key=lambda kv: str(kv[0]))]
    choice = [e for e in multi(facts, "venue.choice").keys()]
    hard_choice = any(it.get("hard") for it in multi(facts, "venue.choice").values())
    opts = food_options(facts)
    required = [e for e in multi(facts, "venue.require").keys()]
    for ent, it in multi(facts, "rule.custom").items():
        if it["v"].get("type") == "feature":
            required.append(it["v"].get("feature", ""))

    K, mj = b["confirmed"], b["min_judges"]
    if cfg.judge_policy == "all_confirmed" and K:
        js = [K]
    elif K is not None:
        js = list(range(mj, K + 1)) or [K]
    else:
        js = [mj]
    sc = parse_clock(b["start_clock"]) if b["start_clock"] else None
    avail = avail_windows(facts, b["start_clock"] and sc)
    best, best_key, checked = None, None, 0
    teams = b["teams"] or 0
    slot = b["slot"] or 0

    for j in js:
        if now() > deadline and best:
            break
        pool = judge_pool(b, j)
        max_rounds = (D // slot) if (D and slot) else 1
        sim = assign_judging(teams, pool, b["panel"], slot, b["max_teams"], {}, 0, max(max_rounds, 1)) if slot else \
            {"assignments": [], "rounds": 0, "loads": {n: 0 for n in pool}, "unassigned": list(range(1, teams + 1))}
        segs, lok = [], False
        for _ in range(4):
            items = make_items(b, facts, sim["rounds"] * slot, cfg)
            segs, lok = layout(D, items, facts, sc, deadline) if D else ([], False)
            jseg = next((s for s in segs if s["kind"] == "judging"), None)
            if not jseg or not slot:
                break
            sim2 = assign_judging(teams, pool, b["panel"], slot, b["max_teams"], avail, jseg["start"], max(max_rounds, 1))
            if sim2["rounds"] == sim["rounds"] and len(sim2["assignments"]) == len(sim["assignments"]):
                sim = sim2
                break
            sim = sim2
        jplan = {"segments": segs, "layout_ok": lok, "pool": pool, "sim": sim}
        need_site = (b["students"] or 0) + b["staff"] + b["volunteers"] + j
        vcands = vinfos or [None]
        for vi in vcands:
            if vi and hard_choice and choice and not any(same_entity(c, vi["name"]) for c in choice):
                continue
            for fo in (opts or [None]):
                if now() > deadline and best:
                    break
                plan = assemble(facts, b, cfg, j, vi, fo, jplan, opts)
                val = validate(plan, facts, cfg)
                checked += 1
                pref_v = 0 if (vi and any(same_entity(c, vi["name"]) for c in choice)) else 1
                opt_idx = opts.index(fo) if fo in opts else 0
                key = (len(val["failed"]), 0 if val["grade"] == "valid" else 1, j, opt_idx, pref_v,
                       plan["budget"]["total"], vi["rent_total"] if vi and vi["rent_total"] is not None else 10 ** 12,
                       str(vi["name"]) if vi else "")
                if best_key is None or key < best_key:
                    best, best_key, best_val = plan, key, val
                    best_vi, best_need = vi, need_site
        if best is not None and best_key[0] == 0 and best_key[1] == 0:
            break   # smallest valid judge count found; more judges only add cost

    plan, val = best, best_val
    # alternatives (explanations of what was not chosen)
    j = plan["judging"]["deployed"]
    need = plan["venue"]["headcount"]
    for vi in vinfos:
        why = venue_reasons(vi, need, required)
        chosen = vi["name"] == plan["venue"]["name"]
        row = {"name": vi["name"], "capacity": vi["capacity"], "rent_total": vi["rent_total"], "features": vi["features"],
               "evidence": vi["ids"]}
        if chosen:
            row["verdict"], row["reason"] = "chosen", "cheapest option that fits everyone, has every required feature and keeps the whole plan valid"
        elif why:
            row["verdict"], row["reason"] = "rejected", "; ".join(why)
        else:
            delta = (vi["rent_total"] - plan["venue"]["rent_total"]) if plan["venue"]["rent_total"] is not None else None
            row["verdict"] = "feasible"
            row["reason"] = f"also fits; costs Rs {delta:,} more" if delta and delta > 0 else "also fits"
        plan["venue"]["alternatives"].append(row)
    cur = plan["food"]["option"]
    cur_price = next((o["price"] for o in opts if o["name"] == cur), None)
    for o in opts:
        row = {"name": o["name"], "price": o["price"], "evidence": o["ids"]}
        if o["name"] == cur:
            row["verdict"], row["reason"] = "chosen", "preferred or first-quoted item; feasible within the budget"
        else:
            d = (cur_price - o["price"]) * plan["food"]["qty"] if (cur_price is not None and plan["food"]["qty"]) else None
            row["verdict"] = "alternative"
            row["reason"] = ((f"would save Rs {d:,} on meals" if d > 0 else f"would cost Rs {-d:,} more on meals" if d < 0 else "same meal cost")
                             if d is not None else "alternative item")
        plan["food"]["alternatives"].append(row)
    plan["solver"] = {"candidates_checked": checked, "tie_break": "fewest judges -> preferred menu item -> lowest total cost -> lowest venue rent -> name",
                      "timed_out": now() > deadline}
    return {"plan": plan, "validation": val, "candidates": checked}


# --------------------------------------------------------------------- diff --

_SKIP = {"evidence", "ids", "solver"}


def _flat(o, prefix, out):
    if isinstance(o, dict):
        for k, v in o.items():
            if k in _SKIP:
                continue
            _flat(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(o, list) and o and all(isinstance(x, dict) and "id" in x for x in o):
        for x in o:
            _flat(x, f"{prefix}[{x['id']}]", out)
    else:
        out[prefix] = o


def diff_plans(old, new):
    a, b = {}, {}
    if old:
        _flat(old, "", a)
    _flat(new, "", b)
    items = []
    for p in sorted(set(a) | set(b)):
        if a.get(p) == b.get(p) and (p in a) == (p in b):
            continue
        va, vb = a.get(p), b.get(p)
        if p.startswith("judging.assignments") or p.startswith("judging.loads") or p == "judging.judges":
            va, vb = (f"{len(va)} entries" if isinstance(va, (list, dict)) else va), (f"{len(vb)} entries" if isinstance(vb, (list, dict)) else vb)
            if p != "judging.assignments":
                continue
        area = p.split(".")[0].split("[")[0]
        if area in ("assumptions", "unknown", "discrepancies", "discrepancies_info"):
            area = "notes"
        items.append({"path": p, "area": area, "before": va, "after": vb})
    return items

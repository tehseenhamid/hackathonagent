"""Reconciliation: plain code that decides which extracted statements apply.

Inputs are candidate statements (from the reader, in arrival order). Output is a
per-statement disposition with a reason and evidence reference, plus the set of
canonical accepted facts the planner may use.

Rules (documented, deterministic, no hardcoded sender names):
  * Authority is by role and scope. A vendor can change its own quote, not the
    required service count; a judge can change their own availability only.
  * Tentative notes, proposals and requests are recorded but never applied as facts.
  * Among authorised, firm statements about the same thing, a later statement wins
    unless its stated date is older than the one it would replace (newest-by-arrival
    does NOT automatically win over a later-dated statement).
  * Conditional statements are kept, not applied, and reported as unverified.
"""
from __future__ import annotations

from .reader import KEY_SPECS, MERGE_FIELDS
from .util import date_cmp, parse_date, same_entity, short

ACCEPTED, SUPERSEDED, IRRELEVANT, OPTIONAL = "accepted", "superseded", "irrelevant", "optional"
TENTATIVE, REJECTED, CLARIFY = "tentative", "rejected", "needs_clarification"

# role -> areas where the role may state binding rules / schedule constraints
ROLE_AREAS = {
    "organizer": {"event", "venue", "schedule", "food", "judging", "budget"},
    "finance": {"budget"},
    "venue": {"venue", "schedule"},
    "caterer": {"food", "schedule"},
    "judging": {"judging", "schedule"},
    "judge": set(),
    "vendor": set(),
    "society": set(),
    "other": set(),
}
# role -> exact keys / prefixes that role may set
ROLE_KEYS = {
    "organizer": ["*"],
    "finance": ["cost.", "event.budget_cap"],
    "venue": ["venue.option"],
    "caterer": ["food.option", "food.order_qty"],
    "judging": ["judge."],
    "judge": ["judge.avail"],
    "vendor": ["cost.commitment", "cost.fee"],
    "society": [],
    "other": [],
}
AREA_KEYS = ("rule.custom", "sched.window", "sched.order", "venue.require", "venue.choice",
             "food.choice", "food.diet", "food.include")
GOVERNING_ROLES = {"organizer"}


def authority(s) -> tuple[bool, str]:
    role, key = s["role"], s["key"]
    if key == "info":
        return True, ""
    if key == "cost.request":
        return True, ""          # anyone may ask; nobody is bound
    for pat in ROLE_KEYS.get(role, []):
        if pat == "*" or key == pat or (pat.endswith(".") and key.startswith(pat)):
            if role == "judge" and key == "judge.avail":
                ent, sender = s.get("ent"), s.get("sender")
                if ent and sender and not same_entity(ent, sender):
                    return False, f"a judge may only change their own availability ('{ent}' is not the sender)"
            return True, ""
    if key in AREA_KEYS and s.get("area") in ROLE_AREAS.get(role, set()):
        return True, ""
    return False, f"role '{role}' has no authority over {key}" + (f" ({s['area']})" if s.get("area") else "")


def _group_ent(s):
    if s["key"] == "sched.order":
        return f"{s.get('ent')}|{s.get('val')}"
    if s["key"] == "judge.avail" and not s.get("ent"):
        return s.get("sender")
    return s.get("ent")


def _older(a, b) -> bool:
    """True if statement a carries a date strictly older than statement b."""
    c = date_cmp(parse_date(a.get("date")), parse_date(b.get("date")))
    return c is not None and c < 0


def _set(s, disp, reason, by=None):
    s["disposition"], s["reason"], s["by"] = disp, reason, by


def static_disposition(s):
    """Return (disposition, reason) if the statement is settled without looking at others."""
    if s.get("lock"):
        return s["lock"]["disposition"], s["lock"]["reason"]
    if s["key"] == "info":
        return IRRELEVANT, s.get("note") or "no planning value"
    if s.get("evidence_unverified"):
        return CLARIFY, "the model's quoted evidence could not be found in the original text, so it is not applied"
    if s.get("ambiguity"):
        return CLARIFY, "extraction was malformed or outside the vocabulary: " + short(s["ambiguity"], 140)
    if s["status"] == "outdated":
        return SUPERSEDED, "the message itself marks this information as replaced or cancelled"
    ok, why = authority(s)
    if not ok:
        if s["role"] == "other":
            return CLARIFY, "sender role could not be established, so authority cannot be verified"
        return REJECTED, why
    if s["key"] == "cost.request":
        return OPTIONAL, "a quote or request: nothing in the pack approves it, so it is not budgeted"
    if s["status"] == "proposal":
        return OPTIONAL, "a proposal or suggestion that nobody has approved"
    if s["status"] == "tentative":
        return TENTATIVE, "stated as tentative or subject to change; recorded but not applied"
    if s.get("cond"):
        return TENTATIVE, "applies only if: " + short(s["cond"], 100) + " (condition not verifiable from the pack)"
    return None


def reconcile(stmts):
    """Mutates each statement's disposition fields; returns (groups, events)."""
    events = []
    for s in stmts:
        s["disposition"] = s["reason"] = s["by"] = None
    groups = []   # dicts: key, ent, members
    for s in stmts:
        d = static_disposition(s)
        if d:
            _set(s, *d)
            if d[0] in (REJECTED, CLARIFY) and not s.get("lock"):
                events.append({"type": "scope" if d[0] == REJECTED else "clarify", "area": s.get("area"),
                               "msg": f"{s['id']} {d[0]}: {d[1]}", "ids": [s["id"]]})
            continue
        if s["key"] == "cost.request":  # handled above (OPTIONAL) - kept for safety
            continue
        ge = _group_ent(s)
        for g in groups:
            if g["key"] == s["key"] and ((ge is None and g["ent"] is None) or
                                         (ge is not None and g["ent"] is not None and same_entity(ge, g["ent"]))):
                g["members"].append(s)
                break
        else:
            groups.append({"key": s["key"], "ent": ge, "members": [s]})
    # latest of the optional requests per entity is informational only
    for g in groups:
        kind = KEY_SPECS[g["key"]][0]
        if kind in MERGE_FIELDS:
            _fold_merge(g, kind, events)
        else:
            _fold_single(g, events)
    return groups, events


def _fold_single(g, events):
    winner = None
    for s in g["members"]:
        if winner is None:
            winner = s
            continue
        if _older(s, winner):
            _set(s, SUPERSEDED, f"older than {winner['id']} (dated {winner.get('date')}); the later-dated statement is kept", winner["id"])
        else:
            if s["val"] != winner["val"]:
                events.append({"type": "conflict", "area": s.get("area"),
                               "msg": f"{g['key']}[{g['ent']}]: {winner['id']} ({short(str(winner['val']), 40)}) vs "
                                      f"{s['id']} ({short(str(s['val']), 40)}); {s['id']} is later and authorised, so it replaces the earlier one",
                               "ids": [winner["id"], s["id"]]})
            _set(winner, SUPERSEDED, f"replaced by later statement {s['id']}", s["id"])
            winner = s
    _set(winner, ACCEPTED, "authorised, firm, and the latest valid statement for this item")
    g["winner"] = winner


def _fold_merge(g, kind, events):
    fields = MERGE_FIELDS[kind]
    fw = {}
    for s in g["members"]:
        for f in fields:
            if f not in s["val"]:
                continue
            cur = fw.get(f)
            if cur is None or not _older(s, cur):
                if cur is not None and cur["val"].get(f) != s["val"].get(f):
                    events.append({"type": "conflict", "area": s.get("area"),
                                   "msg": f"{g['key']}[{g['ent']}].{f}: {cur['id']} ({cur['val'].get(f)}) replaced by later {s['id']} ({s['val'].get(f)})",
                                   "ids": [cur["id"], s["id"]]})
                fw[f] = s
    for s in g["members"]:
        mine = [f for f in fields if fw.get(f) is s]
        if mine:
            _set(s, ACCEPTED, "authorised and firm; sets " + ", ".join(mine))
        else:
            by = next((fw[f]["id"] for f in fields if f in s["val"] and f in fw), None)
            _set(s, SUPERSEDED, "every field it sets was later replaced or is dated older than a kept statement", by)
    g["fields"] = fw


def build_facts(groups, stmts):
    """Canonical accepted facts with the statement ids that support them."""
    single, multi = {}, {}
    for g in groups:
        key, kind = g["key"], KEY_SPECS[g["key"]][0]
        if kind in MERGE_FIELDS:
            val, ids = {}, []
            for f, s in g["fields"].items():
                val[f] = s["val"][f]
                if s["id"] not in ids:
                    ids.append(s["id"])
            multi.setdefault(key, {})[g["ent"]] = {"ent": g["ent"], "v": val, "ids": ids,
                                                   "first": min((m["run_no"], m["seq"]) for m in g["members"])}
            continue
        w = g["winner"]
        item = {"ent": g["ent"], "v": w["val"], "ids": [w["id"]], "hard": w.get("hard", False),
                "first": min((m["run_no"], m["seq"]) for m in g["members"])}
        if KEY_SPECS[key][1] or key == "judge.avail":
            multi.setdefault(key, {})[g["ent"]] = item
        else:
            single[key] = item
    optional, unverified = {}, []
    for s in stmts:
        if s["disposition"] == OPTIONAL and s["key"] == "cost.request":
            optional[s["ent"] or s["id"]] = {"ent": s["ent"], "v": s["val"], "ids": [s["id"]]}
        if s["disposition"] == TENTATIVE and s.get("cond") and s.get("hard"):
            unverified.append({"label": s.get("ent") or s["key"], "why": "conditional hard rule not applied: " + short(s["cond"], 100),
                               "ids": [s["id"]]})
        if s["disposition"] in (CLARIFY,) and s.get("hard"):
            unverified.append({"label": s.get("ent") or s["key"], "why": "hard rule that could not be verified: " + short(s["reason"] or "", 100),
                               "ids": [s["id"]]})
    # later requests replace earlier ones for the same entity; keep only the last
    return {"single": single, "multi": multi, "requests": optional, "unverified": unverified}


def message_summaries(messages, stmts):
    by_id = {s["id"]: s for s in stmts}
    out = []
    for m in messages:
        counts = {}
        for sid in m["stmt_ids"]:
            d = by_id[sid]["disposition"] or "pending"
            counts[d] = counts.get(d, 0) + 1
        if not m["stmt_ids"]:
            label = "irrelevant"
        elif set(counts) == {ACCEPTED}:
            label = "accepted"
        elif ACCEPTED in counts and len(counts) > 1:
            label = "partial"
        elif len(counts) == 1:
            label = next(iter(counts))
        else:
            label = "mixed"
        out.append({**{k: m[k] for k in ("msg_id", "msg_idx", "sender", "role", "channel", "date")},
                    "run_no": m.get("run_no"),
                    "label": label, "counts": counts, "irrelevant": m.get("irrelevant"),
                    "stmt_ids": m["stmt_ids"]})
    return out

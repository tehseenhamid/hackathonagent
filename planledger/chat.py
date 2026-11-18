"""Ask the Plan: a local, read-only, deterministic helper. No model call, no
external search, no plan mutation. It answers from two clearly separate
collections and says which one each answer came from:

  1. "Student Brief rules"   - knowledge/brief_rules.md (procedural rules, page-referenced)
  2. "Live plan and evidence" - the persisted raw messages, accepted facts, plan, budget
                                arithmetic, trace and rejection records of THIS session

Intent matching uses curated English / Roman Urdu aliases plus a small BM25-style
retrieval over both collections; answers are grounded templates with message
ids and exact quotes. Unknown questions get an honest "cannot verify".
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from .render import judging_rounds, totals
from .util import pkr, short

KNOW = Path(__file__).parent / "knowledge" / "brief_rules.md"

RU_WORDS = {"kitne", "kitna", "kitni", "kharcha", "bacha", "bachay", "khana", "jagah", "waqt", "kab", "kyun", "kiun", "ghalat",
            "purana", "purani", "naya", "badla", "tabdeel", "namaz", "hai", "hain", "ka", "ki", "ke", "mein", "me", "kya", "kaun",
            "kaunsa", "paisa", "rakha", "gaya", "gayi", "ho", "hua", "hoga", "batao", "bata", "mujhe", "yeh", "ye", "wala", "kon"}

INTENTS = {
    "judges": {"judge", "judges", "jaj", "panel", "coverage", "honorarium", "slot", "slots", "assigned", "assignment"},
    "budget": {"budget", "spend", "spent", "spending", "cost", "costs", "total", "remaining", "left", "afford", "cap", "kharcha",
               "bacha", "bachay", "paisa", "paise", "rupees", "rs", "expense", "expenses", "headroom", "exceed"},
    "food": {"food", "lunch", "meal", "meals", "khana", "boxes", "box", "caterer", "catering", "menu", "quantity", "qty", "fed"},
    "venue": {"venue", "hall", "jagah", "room", "capacity", "seats", "generator", "location"},
    "schedule": {"schedule", "timetable", "prayer", "namaz", "break", "waqt", "timing", "slot", "start", "end", "lunch", "judging", "agenda"},
    "superseded": {"superseded", "supersede", "outdated", "old", "older", "replaced", "purana", "purani", "obsolete", "stale", "earlier"},
    "rejected": {"rejected", "reject", "wrong", "ghalat", "refused", "rejection", "held", "blocked", "invalid", "plausible"},
    "affected": {"affected", "changed", "change", "recomputed", "recalculated", "badla", "tabdeel", "diff", "delta", "areas", "update", "updated"},
    "deadline": {"deadline", "freeze", "limit", "limits", "timeout", "kab", "when", "cutoff", "cut", "freeze", "1:25", "minutes", "seconds"},
    "trace": {"trace", "tokens", "token", "calls", "call", "model", "usage", "latency", "provider"},
    "checks": {"valid", "validity", "checks", "check", "grade", "pass", "passed", "fail", "failed", "provisional", "validation"},
    "assumptions": {"assume", "assumed", "assumption", "assumptions", "unknown", "unverified", "flag", "flagged", "discrepancy", "unclear"},
    "messages": {"message", "messages", "irrelevant", "ignored", "tentative", "optional", "sender", "who", "said", "evidence", "source"},
}
SAMPLES = [
    "How many judges are assigned and does every team have one?",
    "Kitne judges lagay gaye hain aur har team cover hai?",
    "What is the current spend and how much budget is left?",
    "Budget mein kitna bacha hai?",
    "Which quotes were superseded?",
    "Which messages were rejected and why?",
    "Ghalat message kaunsa tha aur kyun reject hua?",
    "Which areas changed in the last update?",
    "How many meals are ordered and for whom?",
    "Why was this venue chosen?",
    "What are the deadlines and limits?",
    "How many tokens and model calls were used?",
]


STOP = {"the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "in", "on", "for", "and", "or", "what", "which", "how", "do", "does",
        "did", "it", "this", "that", "me", "my", "we", "you", "i", "can", "will", "with", "at", "by", "from", "as", "about", "tell", "show",
        "hai", "hain", "ka", "ki", "ke", "mein", "me", "kya", "ko", "se", "ye", "yeh", "wala", "kab", "ho"}


def _tok(s):
    return re.findall(r"[a-z0-9:]+", (s or "").lower())


def _content_tok(s):
    return [w for w in _tok(s) if w not in STOP and len(w) > 1]


def load_brief():
    chunks, cur = [], None
    try:
        text = KNOW.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        if line.startswith("## "):
            cur = {"title": line[3:].strip(), "text": ""}
            chunks.append(cur)
        elif cur is not None:
            cur["text"] += line + "\n"
    for c in chunks:
        m = re.match(r"\[(Brief p\.[0-9\-]+)\]\s*(.*)", c["title"])
        c["ref"], c["name"] = (m.group(1), m.group(2)) if m else ("Brief", c["title"])
        c["text"] = c["text"].strip()
    return chunks


def _matched(query, text):
    return len(set(_content_tok(query)) & set(_tok(text)))


def _bm25(query, docs):
    """docs: list of (id, text). Returns [(score,id)] best first. Tiny BM25 for a handful of chunks."""
    q = set(_content_tok(query))
    toks = {i: _tok(t) for i, t in docs}
    n = len(docs) or 1
    avg = sum(len(t) for t in toks.values()) / n or 1
    df = {w: sum(1 for t in toks.values() if w in t) for w in q}
    out = []
    for i, t in toks.items():
        s = 0.0
        for w in q:
            tf = t.count(w)
            if not tf:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(t) / avg))
        if s:
            out.append((s, i))
    return sorted(out, reverse=True)


def detect_intent(question):
    q = _tok(question)
    scores = {}
    for name, words in INTENTS.items():
        s = sum(1 for w in q if w in words)
        if s:
            scores[name] = s
    if not scores:
        return None, 0
    best = sorted(scores.items(), key=lambda kv: (-kv[1], list(INTENTS).index(kv[0])))[0]
    return best


def is_roman_urdu(question):
    return sum(1 for w in _tok(question) if w in RU_WORDS) >= 2


def _cite_stmt(s):
    return {"type": "message", "id": s["id"], "label": f"message {s['msg_id']} ({s.get('sender') or 'sender unknown'}), {s['id']}",
            "quote": s.get("quote"), "lines": s.get("lines"), "source_id": s.get("source_id")}


def _stmts(state, **flt):
    out = []
    for s in state["statements"]:
        if all((s.get(k) in v) if isinstance(v, (list, set, tuple)) else (s.get(k) == v) for k, v in flt.items()):
            out.append(s)
    return out


def answer(question, state):
    q = (question or "").strip()
    if not q:
        return _na("Ask a question about the plan, the messages, the trace or the brief rules.")
    ru = is_roman_urdu(q)
    if state["empty"]:
        brief = _brief_answer(q)
        if brief:
            return brief
        return _na("No plan has been committed yet, so there is nothing in the live plan to answer from. Run `python -m planledger <file>` first.")
    intent, score = detect_intent(q)
    fn = {"judges": _judges, "budget": _budget, "food": _food, "venue": _venue, "schedule": _schedule,
          "superseded": _superseded, "rejected": _rejected, "affected": _affected, "deadline": _deadline,
          "trace": _trace, "checks": _checks, "assumptions": _assumptions, "messages": _messages}.get(intent)
    if intent == "deadline":
        return _deadline(state, q, ru)
    if fn:
        r = fn(state, ru)
        if r:
            r["intent"] = intent
            return r
    return _retrieve(q, state, ru)


def _na(msg):
    return {"answer": msg, "collection": "Not verifiable", "citations": [], "intent": None}


def _live(text, cites, intent=None, ru_line=None, ru=False):
    t = (ru_line + "\n\n" if (ru and ru_line) else "") + text
    return {"answer": t, "collection": "Live plan and evidence", "citations": cites, "intent": intent}


def _judges(state, ru):
    p = state["active"]["plan"]
    j = p["judging"]
    chk = next((c for c in state["active"]["checks"]["checks"] if c["id"] == "judging_coverage"), None)
    ev = [s for s in _stmts(state, key=["judge.confirmed", "judge.minutes", "judge.max_teams", "judge.panel", "judge.fee"],
                            disposition="accepted")]
    rounds = judging_rounds(p)
    text = (f"{j['deployed']} judges are deployed out of {j['confirmed']} confirmed (minimum needed: {j['min_needed']}). "
            f"{j['teams']} teams x {j['panel']} judge(s) at {j['slot_min']} minutes each, at most {j['max_teams'] or 'no stated limit'} teams per judge, "
            f"gives {j['rounds']} round(s). Coverage check: {chk['status'].upper()} - {chk['detail']}")
    if rounds:
        text += "\nRounds: " + "; ".join(f"round {r['round']} teams {r['teams']}" for r in rounds)
    ru_line = f"{j['teams']} teams ke liye {j['deployed']} judges lagaye gaye hain (confirmed {j['confirmed']}); coverage check: {chk['status'].upper()}."
    return _live(text, [_cite_stmt(s) for s in ev], "judges", ru_line, ru)


def _budget(state, ru):
    b = state["active"]["plan"]["budget"]
    lines = "; ".join(f"{l['label']} {pkr(l['amount'])}" for l in b["lines"])
    text = (f"Current total spend is {pkr(b['total'])} against a cap of {pkr(b['cap'])}, leaving {pkr(b['remaining'])}. Lines: {lines}.")
    if b["paid_toward"]:
        text += " Already paid toward computed lines (not added again): " + "; ".join(f"{p['label']} {pkr(p['amount'])}" for p in b["paid_toward"]) + "."
    if b["excluded"]:
        text += " Not budgeted (no authority approved it): " + "; ".join(f"{x['label']} {pkr(x['amount'])}" for x in b["excluded"]) + "."
    ev = [s for s in _stmts(state, disposition="accepted") if s["key"].startswith("cost.") or s["key"] == "event.budget_cap"]
    ev += [s for s in _stmts(state, disposition="optional") if s["key"] == "cost.request"]
    return _live(text, [_cite_stmt(s) for s in ev], "budget",
                 f"Abhi tak ka kharcha {pkr(b['total'])} hai, cap {pkr(b['cap'])} hai, bacha hua {pkr(b['remaining'])}.", ru)


def _food(state, ru):
    f = state["active"]["plan"]["food"]
    parts = ", ".join(f"{k} {v}" for k, v in f["fed_parts"].items() if v)
    lines = "; ".join(f"{l['qty']} x {pkr(l['price'])} ({l['option']})" for l in f["lines"])
    text = f"{f['qty']} meals are planned for {f['fed']} people ({parts}): {lines}."
    if f.get("claim_qty") is not None:
        text += f" An order quantity of {f['claim_qty']} is on record."
    sup = [s for s in _stmts(state, disposition="superseded") if s["key"].startswith("food.")]
    rej = [s for s in _stmts(state, disposition=["rejected", "needs_clarification"]) if s["key"].startswith("food.")]
    if sup:
        text += " Superseded food quotes: " + "; ".join(f"{s['id']} {s['val']}" for s in sup) + "."
    if rej:
        text += " Held back: " + "; ".join(f"{s['id']} ({s['reason']})" for s in rej) + "."
    ev = [s for s in _stmts(state, disposition="accepted") if s["key"].startswith("food.") or s["key"] in ("event.students", "event.staff")]
    ev += sup + rej
    return _live(text, [_cite_stmt(s) for s in ev], "food", f"{f['fed']} logon ke liye {f['qty']} meals plan hain.", ru)


def _venue(state, ru):
    v = state["active"]["plan"]["venue"]
    alts = "; ".join(f"{a['name']}: {a['verdict']} - {a['reason']}" for a in v["alternatives"])
    text = (f"{v['name']} (capacity {v['capacity']}, hire {pkr(v['rent_total'])}) hosts {v['headcount']} people on site. Alternatives: {alts}.")
    ev = [s for s in _stmts(state, key="venue.option", disposition="accepted")]
    return _live(text, [_cite_stmt(s) for s in ev], "venue", f"Venue {v['name']} chuna gaya: capacity {v['capacity']}, {v['headcount']} log.", ru)


def _schedule(state, ru):
    p = state["active"]["plan"]
    from .render import tlabel
    seg = "; ".join(f"{tlabel(p, s['start'])}-{tlabel(p, s['end'])} {s['label']}" for s in p["schedule"]["segments"])
    ev = [s for s in _stmts(state, disposition="accepted") if s["key"] in ("event.break_min", "event.duration_min", "judge.minutes")]
    return _live(f"Schedule ({p['schedule']['duration_min']} minutes): {seg}.", [_cite_stmt(s) for s in ev], "schedule",
                 "Schedule neeche di gayi hai; namaz ka break shamil hai.", ru)


def _superseded(state, ru):
    ss = _stmts(state, disposition="superseded")
    if not ss:
        return _live("No statement has been superseded in this session.", [], "superseded")
    text = "Superseded statements:\n" + "\n".join(f"- {s['id']} {s['key']}" + (f"[{s['ent']}]" if s.get("ent") else "") + f" = {s['val']}: {s['reason']}" for s in ss)
    return _live(text, [_cite_stmt(s) for s in ss], "superseded", f"{len(ss)} purani/replace hui statements hain.", ru)


def _rejected(state, ru):
    rs = _stmts(state, disposition=["rejected", "needs_clarification"])
    if not rs:
        return _live("No message has been rejected or held back so far.", [], "rejected", "Abhi tak koi message reject nahi hua.", ru)
    text = "Rejected or held back:\n" + "\n".join(
        f"- {s['id']} ({s.get('sender') or 'unknown sender'}, {s['disposition']}): {s['key']}" + (f"[{s['ent']}]" if s.get("ent") else "") + f" = {s['val']}. Reason: {s['reason']}" for s in rs)
    runs = [r for r in state["runs"] if r["outcome"] in ("update_rejected", "update_blocked")]
    if runs:
        text += f"\nRetained plan: revision {runs[-1]['active_rev']} stayed in force."
    return _live(text, [_cite_stmt(s) for s in rs], "rejected", f"{len(rs)} message(s) reject/hold hue; wajah neeche likhi hai.", ru)


def _affected(state, ru):
    runs = [r for r in state["runs"] if r["kind"] == "update" and r.get("delta")]
    if not runs:
        d = state["active"]["delta"]
        return _live("No update has been ingested yet. The baseline recomputed all five areas.", [], "affected")
    r = runs[-1]
    d = r["delta"]
    text = (f"Last update (run {r['run_no']}, {r['outcome']}): changed areas: {', '.join(d['changed_areas']) or 'none'}; recomputed by code: "
            f"{', '.join(d['recomputed_areas']) or 'none'}; areas with model calls: {', '.join(d['called_areas']) or 'none'}; kept valid unchanged: "
            f"{', '.join(d.get('kept_valid_areas', [])) or 'none'}. Active revision {r['active_rev']}.")
    if d["items"]:
        text += "\nChanges: " + "; ".join(f"{i['path']}: {i['before']} to {i['after']}" for i in d["items"][:8])
    cites = [_cite_stmt(s) for s in state["statements"] if s["run_no"] == r["run_no"]]
    return _live(text, cites, "affected", f"Run {r['run_no']} mein badle hue areas: {', '.join(d['changed_areas']) or 'koi nahi'}.", ru)


def _deadline(state, q, ru):
    chunks = load_brief()
    pick = [c for c in chunks if c["name"].lower().startswith(("limits", "the day", "how the test"))]
    text = "From the Student Brief:\n" + "\n".join(f"- {c['ref']} {c['name']}: {c['text']}" for c in pick)
    return {"answer": text, "collection": "Student Brief rules", "intent": "deadline",
            "citations": [{"type": "brief", "id": c["ref"], "label": f"{c['ref']} {c['name']}", "quote": short(c["text"], 120)} for c in pick]}


def _trace(state, ru):
    t = state["totals"]
    last = [i for i in state["trace"] if i["kind"] == "call"][-3:]
    text = (f"{t['calls']} model call(s) in the session; provider-reported tokens: input {t['input']:,}, output {t['output']:,}, total {t['total']:,}"
            + (f" (PARTIAL: {t['missing_usage']} call(s) reported no usage)" if t["partial"] else "") + ".")
    if last:
        text += " Latest calls: " + "; ".join(f"#{c['call_no']} [{c['tag']}] {c['outcome']} {((c.get('elapsed_ms') or 0) / 1000):.1f}s" for c in last)
    return {"answer": text, "collection": "Live plan and evidence", "intent": "trace",
            "citations": [{"type": "trace", "id": str(c["seq"]), "label": f"trace call #{c['call_no']}", "quote": None} for c in last]}


def _checks(state, ru):
    a = state["active"]
    cs = a["checks"]["checks"]
    text = f"Revision {a['rev_no']} is {a['grade'].upper()}: {a['checks']['passed']} of {a['checks']['total']} checks pass.\n" + "\n".join(
        f"- [{c['status'].upper()}] {c['label']}: {c['detail']}" for c in cs)
    return _live(text, [], "checks", f"Revision {a['rev_no']} ka grade: {a['grade'].upper()}.", ru)


def _assumptions(state, ru):
    p = state["active"]["plan"]
    text = "Assumptions (not facts from the messages):\n" + "\n".join("- " + a for a in p.get("assumptions", [])) if p.get("assumptions") else "No assumptions are flagged."
    if p.get("unknown"):
        text += "\nUnknown inputs:\n" + "\n".join(f"- {u['item']}: {u['why']}" for u in p["unknown"])
    if state["active"]["facts"].get("unverified"):
        text += "\nUnverified hard rules:\n" + "\n".join(f"- {u['label']}: {u['why']}" for u in state["active"]["facts"]["unverified"])
    return _live(text, [], "assumptions")


def _messages(state, ru):
    ms = state["messages"]
    if not ms:
        return None
    text = "Messages and how each was treated:\n" + "\n".join(
        f"- run {m['run_no']} message {m['msg_id']} from {m.get('sender') or 'unknown'} ({m['role']}): {m['label'].upper()}" + (f" - {m['irrelevant']}" if m.get("irrelevant") else "")
        + (f" [{', '.join(f'{k} {v}' for k, v in m['counts'].items())}]" if m.get("counts") else "") for m in ms)
    return _live(text, [], "messages")


def _brief_answer(q):
    chunks = load_brief()
    hits = _bm25(q, [(i, c["title"] + " " + c["text"]) for i, c in enumerate(chunks)])
    if not hits or hits[0][0] < 1.0:
        return None
    best = [chunks[i] for sc, i in hits[:2] if _matched(q, chunks[i]["title"] + " " + chunks[i]["text"]) >= 2]
    if not best:
        return None
    return {"answer": "From the Student Brief:\n" + "\n".join(f"- {c['ref']} {c['name']}: {c['text']}" for c in best),
            "collection": "Student Brief rules", "intent": "brief",
            "citations": [{"type": "brief", "id": c["ref"], "label": f"{c['ref']} {c['name']}", "quote": short(c["text"], 140)} for c in best]}


def _retrieve(q, state, ru):
    brief = _brief_answer(q)
    docs = [(s["id"], " ".join(str(x) for x in (s.get("quote"), s.get("note"), s.get("key"), s.get("ent"), s.get("sender"))))
            for s in state["statements"]]
    hits = _bm25(q, docs)
    live = None
    by = {s["id"]: s for s in state["statements"]}
    hits = [(sc, i) for sc, i in hits if _matched(q, " ".join(str(x) for x in (by[i].get("quote"), by[i].get("note"), by[i].get("key"), by[i].get("ent")))) >= 2]
    if hits and hits[0][0] >= 1.5:
        top = [by[i] for _, i in hits[:3]]
        live = {"answer": "Closest evidence in the messages:\n" + "\n".join(
            f"- {s['id']} ({s['disposition']}): \"{s.get('quote')}\" - {s['key']}" + (f"[{s['ent']}]" if s.get("ent") else "") for s in top),
            "collection": "Live plan and evidence", "citations": [_cite_stmt(s) for s in top], "intent": "retrieval"}
    if live and brief:
        return {"answer": live["answer"] + "\n\n" + brief["answer"], "collection": "Live plan and evidence + Student Brief rules",
                "citations": live["citations"] + brief["citations"], "intent": "retrieval"}
    if live or brief:
        return live or brief
    return _na("I cannot verify that from the committed plan, the raw messages, the trace or the Student Brief rules, so I will not guess. "
               "Try asking about judges, budget, food, venue, schedule, rejected or superseded messages, changed areas, the trace, or the deadlines.")

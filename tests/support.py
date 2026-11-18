"""TEST-ONLY helpers. Nothing here is imported by the production package.

`ScriptedAdapter` stands in for a model provider in offline tests. Its token numbers
are invented by the tests and every test trace is labelled as such; they are never
presented as provider-reported usage.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from planledger.config import Config
from planledger.providers import ModelResult, ProviderError

# ---- an independently authored synthetic pack (different numbers, senders, formats) ----
PACK_A = """\
#1 | Registrar office | email | 2 Dec
Approved: a 4-hour coding sprint for 120 students in teams of 4. Total budget Rs 500,000 including everything. We will have 8 staff on duty. Keep a 20 minute prayer break.

#2 | Hall booking desk | email | 1 Dec
Room Z seats 60 for Rs 20,000. Hall X seats 150 for Rs 60,000. Hall Y seats 300 for Rs 90,000 and has a standby generator.

#3 | Catering | whatsapp | 25 Nov
Regular lunch box Rs 650 per head.

#4 | Catering | whatsapp | 3 Dec
Assalam o alaikum! Regular lunch box ab Rs 700 per head ho gaya hai, light wala Rs 500. Delivery free.

#5 | Judges' lead | email | 3 Dec
14 judges are confirmed. Each gets Rs 4,000. Allow 20 minutes per team and no judge should see more than 4 teams.

#6 | Accounts | email | 1 Dec
Already paid from the budget: banners Rs 15,000 and trophies Rs 30,000.

#7 | Student council | whatsapp | 4 Dec
Can we add fireworks at the end? Quote is Rs 40,000.

#8 | Dr Noor | whatsapp | 4 Dec
I might arrive 15 minutes late, will confirm tomorrow.
"""


def stmt(key, ent, val, quote, status="confirmed", hard=False, cond=None, replaces=None, note=""):
    return {"key": key, "ent": ent, "val": val, "status": status, "hard": hard, "cond": cond, "replaces": replaces,
            "quote": quote, "note": note or key}


def msg(i, frm, role, ch, date, stmts, irrelevant=None):
    return {"id": str(i), "from": frm, "role": role, "channel": ch, "date": date, "lang": "en",
            "irrelevant": irrelevant, "stmts": stmts}


SCRIPT_A = {"messages": [
    msg(1, "Registrar office", "organizer", "email", "2 Dec", [
        stmt("event.students", None, 120, "a 4-hour coding sprint for 120 students in teams of 4", hard=True),
        stmt("event.team_size", None, 4, "120 students in teams of 4", hard=True),
        stmt("event.duration_min", None, 240, "a 4-hour coding sprint", hard=True),
        stmt("event.budget_cap", None, 500000, "Total budget Rs 500,000 including everything", hard=True),
        stmt("event.staff", None, 8, "8 staff on duty", hard=True),
        stmt("event.break_min", "prayer", 20, "Keep a 20 minute prayer break", hard=True)]),
    msg(2, "Hall booking desk", "venue", "email", "1 Dec", [
        stmt("venue.option", "room z", {"cap": 60, "rent": 20000}, "Room Z seats 60 for Rs 20,000"),
        stmt("venue.option", "hall x", {"cap": 150, "rent": 60000}, "Hall X seats 150 for Rs 60,000"),
        stmt("venue.option", "hall y", {"cap": 300, "rent": 90000, "feat": ["generator"]}, "Hall Y seats 300 for Rs 90,000 and has a standby generator")]),
    msg(3, "Catering", "caterer", "chat", "25 Nov", [
        stmt("food.option", "regular", {"price": 650}, "Regular lunch box Rs 650 per head")]),
    msg(4, "Catering", "caterer", "chat", "3 Dec", [
        stmt("food.option", "regular", {"price": 700, "deliv": 0}, "Regular lunch box ab Rs 700 per head ho gaya hai"),
        stmt("food.option", "light", {"price": 500, "deliv": 0}, "light wala Rs 500")]),
    msg(5, "Judges' lead", "judging", "email", "3 Dec", [
        stmt("judge.confirmed", None, 14, "14 judges are confirmed"),
        stmt("judge.fee", None, 4000, "Each gets Rs 4,000"),
        stmt("judge.minutes", None, 20, "Allow 20 minutes per team", hard=True),
        stmt("judge.max_teams", None, 4, "no judge should see more than 4 teams", hard=True)]),
    msg(6, "Accounts", "finance", "email", "1 Dec", [
        stmt("cost.commitment", "banners", {"amt": 15000, "for": "other"}, "banners Rs 15,000"),
        stmt("cost.commitment", "trophies", {"amt": 30000, "for": "other"}, "trophies Rs 30,000")]),
    msg(7, "Student council", "society", "chat", "4 Dec", [
        stmt("cost.request", "fireworks", {"amt": 40000}, "Can we add fireworks at the end? Quote is Rs 40,000.", status="proposal")]),
    msg(8, "Dr Noor", "judge", "chat", "4 Dec", [
        stmt("judge.avail", "dr noor", {"from": "+15"}, "I might arrive 15 minutes late", status="tentative")]),
]}

UPD_GENUINE = "Accounts here. The banners bill is Rs 22,000, not Rs 15,000."
UPD_GENUINE_SCRIPT = {"messages": [msg(1, "Accounts", "finance", "email", None, [
    stmt("cost.commitment", "banners", {"amt": 22000, "for": "other"}, "The banners bill is Rs 22,000, not Rs 15,000", replaces="banners")])]}

UPD_WRONG = "Catering: to keep your cost low we have trimmed your order to 90 boxes."
UPD_WRONG_SCRIPT = {"messages": [msg(1, "Catering", "caterer", "chat", None, [
    stmt("food.order_qty", None, 90, "we have trimmed your order to 90 boxes", hard=True)])]}

UPD_IMPOSSIBLE = "Registrar: the venue must have a projector."
UPD_IMPOSSIBLE_SCRIPT = {"messages": [msg(1, "Registrar", "organizer", "email", None, [
    stmt("venue.require", "projector", True, "the venue must have a projector", hard=True)])]}

UPD_OUTSIDER = "Student council: we decided to cancel the prayer break."
UPD_OUTSIDER_SCRIPT = {"messages": [msg(1, "Student council", "society", "chat", None, [
    stmt("event.break_min", "prayer", 0, "we decided to cancel the prayer break", hard=True)])]}


class ScriptedAdapter:
    """Returns canned JSON chosen by a substring of the user payload. TEST ONLY."""
    name = "scripted-test"

    def __init__(self, routes, delay=0.0, usage=True):
        self.routes, self.delay, self.usage, self.calls = routes, delay, usage, []

    def complete(self, system, user, timeout_s, max_tokens, json_mode):
        self.calls.append({"system": system, "user": user, "timeout_s": timeout_s})
        if self.delay:
            time.sleep(self.delay)
        for needle, resp in self.routes:
            if needle in user:
                if isinstance(resp, Exception):
                    raise resp
                n = len(self.calls)
                text = resp if isinstance(resp, str) else json.dumps(resp)
                u = {"input": 100 * n, "output": 10 * n, "total": 110 * n, "sub": {}} if self.usage else None
                return ModelResult(text=text, resolved_model="scripted-test-model-1", usage=u,
                                   usage_note=None if self.usage else "TEST: provider reported no usage")
        raise ProviderError("http", "HTTP 400: no scripted route matched (test)")


def make_cfg(tmp: Path, **over) -> Config:
    env = {"PLANLEDGER_DATA_DIR": str(tmp), "PLANLEDGER_PROVIDER": "openai", "PLANLEDGER_BASE_URL": "http://127.0.0.1:9/v1",
           "PLANLEDGER_MODEL": "test-model", "PLANLEDGER_API_KEY": "sk-test-secret-123"}
    env.update({k: str(v) for k, v in over.items()})
    return Config.load(env=env, cwd=Path(tmp))


def write(tmp: Path, name: str, text: str) -> Path:
    p = Path(tmp) / name
    p.write_text(text, encoding="utf-8", newline="")
    return p


def tmpdir():
    return tempfile.TemporaryDirectory()

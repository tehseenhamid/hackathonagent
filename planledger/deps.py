"""Dependency map between facts and plan areas, used to explain which areas were
recomputed after a change. Recalculation is deterministic code and never makes a
model call; model calls are a separate, observed list."""
from __future__ import annotations

from .reader import key_area

AREAS = ("venue", "schedule", "food", "judging", "budget")

# fact key (prefix) -> areas whose inputs it feeds directly
DIRECT = [
    ("event.students", {"venue", "food", "judging", "budget", "schedule"}),
    ("event.staff", {"venue", "food", "budget"}),
    ("event.volunteers", {"venue", "food", "budget"}),
    ("event.team_size", {"judging", "schedule", "venue", "food", "budget"}),
    ("event.teams", {"judging", "schedule", "venue", "food", "budget"}),
    ("event.duration_min", {"schedule", "venue", "judging", "budget"}),
    ("event.start_clock", {"schedule", "venue", "judging"}),
    ("event.end_clock", {"schedule", "venue", "judging"}),
    ("event.break_min", {"schedule", "judging"}),
    ("event.budget_cap", {"budget"}),
    ("venue.", {"venue", "budget", "schedule"}),
    ("food.", {"food", "budget"}),
    ("judge.", {"judging", "schedule", "venue", "food", "budget"}),
    ("cost.", {"budget"}),
    ("sched.", {"schedule", "judging"}),
]
# derived edges: when the left area changes, these must be re-derived
EDGES = {
    "judging": {"schedule", "venue", "food", "budget"},
    "schedule": {"judging"},
    "venue": {"budget"},
    "food": {"budget"},
}

ROUTING_GUIDE = """ROUTING GUIDE (written before any update is opened)

Default path for an ordinary update (a new message in the same pack):
  model calls : 1 call tagged "reading" - the whole new message is read verbatim and turned into candidate facts.
  no model call: venue, schedule, food, judging, budget. They are recomputed by plain code only when an accepted fact
                 that feeds them changed (see the dependency map). Recomputing is not a model call.
  Therefore the predicted observed calls are: reading x1 (x2 only if the first answer fails strict validation).

An area-tagged model call (venue, schedule, food, judging or budget) is NOT part of the default path. This build makes
none: a statement the reader cannot map to a closed rule is kept as an opaque candidate with exact evidence and is
marked needs_clarification instead of being guessed by a second model.

Dependency map (fact -> areas it feeds):
  audience (students, staff, volunteers)     -> venue occupancy, food quantity, budget
  team count / team size                     -> judging, schedule, and (through judges on site) venue, food, budget
  judge count, fee, minutes, per-judge limit -> judging, schedule, and (through judges deployed) venue, food, budget
  venue quote / features / hours             -> venue, budget, schedule availability
  menu price / quantity / dietary rule       -> food, budget
  committed costs, fees, budget cap          -> budget only
  schedule windows / breaks / order          -> schedule, judging
"""


def key_areas(key: str, val=None) -> set:
    if key == "rule.custom":
        a = key_area(key, val)
        return {a} if a in AREAS else set(AREAS)
    for pre, areas in DIRECT:
        if key == pre or (pre.endswith(".") and key.startswith(pre)):
            return set(areas)
    return set()


def closure(areas: set) -> set:
    out, stack = set(areas), list(areas)
    while stack:
        a = stack.pop()
        for b in EDGES.get(a, ()):
            if b not in out:
                out.add(b)
                stack.append(b)
    return out


def _flat(facts):
    out = {}
    for k, it in facts.get("single", {}).items():
        out[(k, None)] = it["v"]
    for k, d in facts.get("multi", {}).items():
        for e, it in d.items():
            out[(k, e)] = it["v"]
    return out


def fact_changes(old, new):
    a, b = (_flat(old) if old else {}), _flat(new)
    ch = []
    for k in sorted(set(a) | set(b), key=lambda x: (x[0], str(x[1]))):
        if a.get(k) != b.get(k) or ((k in a) != (k in b)):
            ch.append({"key": k[0], "ent": k[1], "before": a.get(k), "after": b.get(k)})
    return ch


def dirty_areas(changes):
    d = set()
    for c in changes:
        d |= key_areas(c["key"], c["after"] if isinstance(c["after"], dict) else c["before"])
    return d

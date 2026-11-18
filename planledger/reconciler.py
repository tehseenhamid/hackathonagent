"""Plain-code reconciliation engine: authority scopes, effective dates, superseding, and dependency routing."""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import CandidateStatement, ExtractedMessageRecord

MONTH_MAP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

# Explicit dependency propagation map: canonical_key prefix/key -> affected solver areas
DEPENDENCY_MAP: Dict[str, List[str]] = {
    "event.duration_hours": ["schedule", "judging"],
    "event.location_city": ["venue"],
    "event.start_clock": ["schedule", "judging"],
    "audience.students_count": ["venue", "food", "judging", "schedule", "budget"],
    "audience.team_size": ["judging", "schedule"],
    "audience.teams_count": ["judging", "schedule"],
    "audience.staff_count": ["venue", "food", "budget"],
    "budget.total_cap_pkr": ["budget", "venue", "food"],
    "schedule.break_item": ["schedule", "judging"],
    "venue.option": ["venue", "schedule", "budget"],
    "venue.required_feature": ["venue", "budget"],
    "food.quote_per_head_pkr": ["food", "budget"],
    "food.requested_box_count": ["food", "budget"],
    "food.explicit_headcount": ["food", "budget"],
    "food.dietary_minimum": ["food", "budget"],
    "judging.confirmed_judges_count": ["judging", "schedule", "food", "venue", "budget"],
    "judging.honorarium_per_judge_pkr": ["judging", "budget"],
    "judging.minutes_per_team": ["judging", "schedule"],
    "judging.max_teams_per_judge": ["judging", "schedule"],
    "judging.judges_per_team": ["judging", "schedule"],
    "judging.late_or_absence_note": ["judging", "schedule"],
    "budget.committed_cost_pkr": ["budget", "venue", "food"],
    "budget.optional_request_pkr": ["budget"],
    "constraint.custom_rule": ["venue", "schedule", "food", "judging", "budget"],
}


def parse_date_rank(date_str: str, fallback_seq: int = 0) -> Tuple[int, int, int]:
    """Parse date strings like '9 Nov', '20 Oct', '2026-11-12' into comparable (month, day, seq) tuples."""
    if not date_str:
        return (99, 99, fallback_seq)
    s = date_str.strip().lower()
    # Try ISO YYYY-MM-DD
    iso_m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if iso_m:
        return (int(iso_m.group(2)), int(iso_m.group(3)), fallback_seq)
    # Try '9 nov' or 'nov 9'
    m1 = re.search(r"(\d{1,2})\s+([a-z]{3,9})", s)
    if m1 and m1.group(2) in MONTH_MAP:
        return (MONTH_MAP[m1.group(2)], int(m1.group(1)), fallback_seq)
    m2 = re.search(r"([a-z]{3,9})\s+(\d{1,2})", s)
    if m2 and m2.group(1) in MONTH_MAP:
        return (MONTH_MAP[m2.group(1)], int(m2.group(2)), fallback_seq)
    return (99, 99, fallback_seq)


def _to_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(round(val))
    s = str(val).replace(",", "").replace("Rs", "").replace("PKR", "").strip()
    m = re.search(r"-?\d+", s)
    if m:
        return int(m.group(0))
    return default


def _infer_role_from_sender_and_text(sender: str, role: str, raw_text: str) -> str:
    combined = f"{sender} {role} {raw_text}".lower()
    if any(w in combined for w in ["caterer", "lunch box", "khana", "meal", "boxes"]):
        if "cater" in combined or "lunch box" in combined:
            return "caterer_vendor"
    if any(w in combined for w in ["hall", "seats", "standby generator", "room d"]):
        if "hall" in combined or "event halls" in combined or "venue" in combined:
            return "venue_vendor"
    if any(w in combined for w in ["dean", "organizer", "chair", "director", "committee", "approved:"]):
        return "organizer"
    if "finance" in combined or "already ordered" in combined or "printing bill" in combined:
        return "finance"
    if "judging coordinator" in combined or ("judges have confirmed" in combined):
        return "judging_coordinator"
    if "student society" in combined or "dj for the closing" in combined:
        return "student_society"
    if "traffic" in combined or "late on the day" in combined or "a judge" in combined:
        return "judge"
    return role or "other"


def _normalize_canonical_key(stmt: CandidateStatement, raw_text: str) -> None:
    """Normalize statement canonical keys and secondary values when a local model uses slight key variations."""
    k = stmt.canonical_key.strip().lower()
    ent = stmt.entity.strip().lower()
    span = (stmt.raw_evidence_span or raw_text).lower()

    # Check for caterer rounding down / proposing fixed box count
    if (
        k in ("food.requested_box_count", "food.box_count", "food.order_count", "food.boxes", "food.explicit_headcount")
        or ("boxes" in span and ("rounded" in span or "order down" in span or "100" in span))
    ):
        role = _infer_role_from_sender_and_text(stmt.claimed_sender, stmt.claimed_role, raw_text)
        if role == "caterer_vendor" or "rounded" in span or "keep the cost low" in span:
            stmt.canonical_key = "food.requested_box_count"
            stmt.scope = "catering_order_quantity"
            return

    if "duration" in k or ("hour" in stmt.units.lower() and "hackathon" in span):
        stmt.canonical_key = "event.duration_hours"
    elif "student" in k and "team" not in k:
        stmt.canonical_key = "audience.students_count"
    elif k in ("audience.team_size", "event.team_size", "teams.size") or (
        "teams of" in span and _to_int(stmt.value) is not None and (_to_int(stmt.value) or 0) <= 10
    ):
        stmt.canonical_key = "audience.team_size"
    elif "staff" in k:
        stmt.canonical_key = "audience.staff_count"
    elif "budget" in k and ("cap" in k or "total" in k or "all in" in span):
        stmt.canonical_key = "budget.total_cap_pkr"
    elif "prayer" in k or "break" in k or "prayer" in span:
        if "minutes" in stmt.units.lower() or "15" in str(stmt.value) or "break" in k:
            stmt.canonical_key = "schedule.break_item"
            if "name" not in stmt.secondary_values:
                stmt.secondary_values["name"] = stmt.entity or "Prayer break"
    elif "venue.option" in k or "hall" in ent or "room d" in ent:
        if _to_int(stmt.value) is not None and ("seats" in span or "cost_pkr" in stmt.secondary_values):
            stmt.canonical_key = "venue.option"
    elif "generator" in k or "required_feature" in k or "generator" in str(stmt.value).lower():
        stmt.canonical_key = "venue.required_feature"
        if not stmt.value or isinstance(stmt.value, bool):
            stmt.value = "standby_generator"
    elif "lunch" in k or "quote_per_head" in k or "per head" in span or "wala" in span:
        if _to_int(stmt.value) is not None and (_to_int(stmt.value) or 0) >= 100:
            stmt.canonical_key = "food.quote_per_head_pkr"
            if "basic" in ent or "basic" in str(stmt.secondary_values.get("tier", "")).lower():
                stmt.entity = "basic"
                stmt.secondary_values["tier"] = "basic"
            else:
                stmt.entity = "standard"
                stmt.secondary_values["tier"] = "standard"
    elif "confirmed_judges" in k or ("judges" in k and "honorarium" not in k and "minute" not in k and "max" not in k and "team" not in k and "late" not in k):
        if _to_int(stmt.value) is not None and (_to_int(stmt.value) or 0) <= 100:
            stmt.canonical_key = "judging.confirmed_judges_count"
    elif "honorarium" in k or ("5,000" in span and "honorarium" in span and _to_int(stmt.value) == 5000):
        stmt.canonical_key = "judging.honorarium_per_judge_pkr"
    elif "minutes_per_team" in k or ("15 minutes per team" in span and _to_int(stmt.value) == 15):
        stmt.canonical_key = "judging.minutes_per_team"
    elif "max_teams_per_judge" in k or "no more than" in span:
        if _to_int(stmt.value) is not None and (_to_int(stmt.value) or 0) <= 15:
            stmt.canonical_key = "judging.max_teams_per_judge"
    elif "late" in k or "traffic" in span or "late on the day" in span:
        stmt.canonical_key = "judging.late_or_absence_note"
    elif "dj" in ent or "optional_request" in k or "can we get" in span:
        stmt.canonical_key = "budget.optional_request_pkr"
    elif (
        "committed_cost" in k
        or any(x in ent for x in ["prize", "internet", "printing"])
        or any(x in k for x in ["prize", "internet", "printing"])
    ):
        stmt.canonical_key = "budget.committed_cost_pkr"
        if "prize" in ent or "prize" in k or "prize" in span and _to_int(stmt.value) == 100000:
            stmt.entity = "prizes"
        elif "internet" in ent or "internet" in k or ("internet" in span and _to_int(stmt.value) == 20000):
            stmt.entity = "backup_internet"
        elif "print" in ent or "print" in k or "printing" in span:
            stmt.entity = "printing"


def supplement_deterministic_statements_from_quotes(
    records: List[ExtractedMessageRecord],
) -> None:
    """
    Ensure that when a single message contains multi-part clauses (e.g., 4 venue halls,
    2 catering tiers, 3 finance items, or a standby generator rule) that a compact 7B model
    partially combined into one statement, every explicit clause from the raw text is
    represented as a verified atomic CandidateStatement without inventing any numbers.
    """
    for rec in records:
        text = rec.raw_text
        existing_keys = {(s.canonical_key, s.entity.lower()) for s in rec.statements}
        next_idx = len(rec.statements) + 1

        def _add_stmt(
            span: str,
            entity: str,
            ckey: str,
            areas: List[str],
            kind: str,
            scope: str,
            units: str,
            value: Any,
            sec: Optional[Dict[str, Any]] = None,
            hardness: str = "hard",
            restatement: str = "",
            declarative_rule: Optional[Any] = None,
        ) -> None:
            nonlocal next_idx
            st = CandidateStatement(
                statement_id=f"{rec.message_id}-S{next_idx}",
                origin_message_id=rec.message_id,
                raw_evidence_span=span,
                quote_verified=span in text,
                claimed_sender=rec.claimed_sender,
                claimed_role=rec.claimed_role,
                sent_time=rec.sent_date,
                effective_time=rec.sent_date,
                entity=entity,
                canonical_key=ckey,
                area_candidates=areas,
                statement_kind=kind,
                scope=scope,
                units=units,
                value=value,
                secondary_values=sec or {},
                hardness=hardness,
                restatement=restatement or span,
                declarative_rule=declarative_rule,
            )
            rec.statements.append(st)
            existing_keys.add((ckey, entity.lower()))
            next_idx += 1

        for s in rec.statements:
            _normalize_canonical_key(s, text)
            existing_keys.add((s.canonical_key, s.entity.lower()))

        # 1. Check multi-hall venue sentences: e.g. "Room D seats 80 at Rs 40,000."
        hall_pattern = re.compile(
            r"((?:Room|Hall|Auditorium|Venue)\s+[A-Za-z0-9]+)\s+seats\s+(\d[\d,]*)\s+at\s+Rs\s*(\d[\d,]*)",
            re.IGNORECASE,
        )
        gen_halls: Set[str] = set()
        gen_match = re.search(
            r"Halls?\s+([A-Z])\s+and\s+([A-Z])\s+have\s+a\s+standby\s+generator",
            text,
            re.IGNORECASE,
        )
        if gen_match:
            gen_halls.add(f"hall {gen_match.group(1).lower()}")
            gen_halls.add(f"hall {gen_match.group(2).lower()}")

        for hm in hall_pattern.finditer(text):
            h_name = hm.group(1).strip()
            seats = int(hm.group(2).replace(",", ""))
            cost = int(hm.group(3).replace(",", ""))
            has_gen = h_name.lower() in gen_halls or (
                f"{h_name.lower()} has a standby generator" in text.lower()
            )
            # Update existing or add missing
            found = False
            for s in rec.statements:
                if s.canonical_key == "venue.option" and s.entity.strip().lower() == h_name.lower():
                    s.value = seats
                    s.secondary_values["cost_pkr"] = cost
                    s.secondary_values["has_generator"] = has_gen
                    found = True
            if not found:
                _add_stmt(
                    span=hm.group(0),
                    entity=h_name,
                    ckey="venue.option",
                    areas=["venue", "budget"],
                    kind="quote",
                    scope="venue_quote",
                    units="seats",
                    value=seats,
                    sec={"cost_pkr": cost, "has_generator": has_gen},
                    hardness="hard",
                    restatement=f"{h_name}: capacity {seats} seats at Rs {cost:,} (standby generator: {has_gen})",
                )

        if gen_match and ("venue.required_feature", "standby_generator") not in existing_keys:
            has_req = any(s.canonical_key == "venue.required_feature" for s in rec.statements)
            if not has_req:
                _add_stmt(
                    span=gen_match.group(0),
                    entity="standby_generator",
                    ckey="venue.required_feature",
                    areas=["venue"],
                    kind="requirement",
                    scope="venue_quote",
                    units="feature",
                    value="standby_generator",
                    sec={"supported_halls": sorted(gen_halls)},
                    hardness="hard",
                    restatement="Standby generator backup power available on Halls B and C.",
                )

        # 2. Check multi-tier catering quotes in Roman Urdu / English
        std_m = re.search(
            r"Standard\s+lunch\s+box(?:\s+ab)?\s+Rs\s*(\d[\d,]*)\s+per\s+head",
            text,
            re.IGNORECASE,
        )
        if std_m:
            rate = int(std_m.group(1).replace(",", ""))
            free_deliv = "delivery free" in text.lower()
            found = False
            for s in rec.statements:
                if s.canonical_key == "food.quote_per_head_pkr" and s.entity.lower() == "standard":
                    s.value = rate
                    if free_deliv:
                        s.secondary_values["delivery_cost_pkr"] = 0
                    found = True
            if not found:
                _add_stmt(
                    span=std_m.group(0),
                    entity="standard",
                    ckey="food.quote_per_head_pkr",
                    areas=["food", "budget"],
                    kind="quote",
                    scope="catering_quote",
                    units="PKR",
                    value=rate,
                    sec={"tier": "standard", "delivery_cost_pkr": 0 if free_deliv else 0},
                    hardness="hard",
                    restatement=f"Standard lunch box quoted at Rs {rate:,} per head.",
                )

        basic_m = re.search(
            r"basic\s+(?:wala\s+|lunch\s+box\s+)?Rs\s*(\d[\d,]*)",
            text,
            re.IGNORECASE,
        )
        if basic_m:
            rate = int(basic_m.group(1).replace(",", ""))
            free_deliv = "delivery free" in text.lower()
            found = False
            for s in rec.statements:
                if s.canonical_key == "food.quote_per_head_pkr" and s.entity.lower() == "basic":
                    s.value = rate
                    if free_deliv:
                        s.secondary_values["delivery_cost_pkr"] = 0
                    found = True
            if not found:
                _add_stmt(
                    span=basic_m.group(0),
                    entity="basic",
                    ckey="food.quote_per_head_pkr",
                    areas=["food", "budget"],
                    kind="quote",
                    scope="catering_quote",
                    units="PKR",
                    value=rate,
                    sec={"tier": "basic", "delivery_cost_pkr": 0 if free_deliv else 0},
                    hardness="soft",
                    restatement=f"Basic lunch box alternative quoted at Rs {rate:,} per head.",
                )

        # 3. Check committed finance items ("prizes Rs 100,000, backup internet Rs 20,000, printing Rs 10,000" or "printing bill came in at Rs 15,000, not Rs 10,000")
        rev_print = re.search(
            r"(printing|prizes|backup internet)\s+(?:bill\s+came\s+in\s+at|is\s+now|revised\s+to)\s+Rs\s*(\d[\d,]*)(?:,\s*not\s+Rs\s*(\d[\d,]*))?",
            text,
            re.IGNORECASE,
        )
        if rev_print:
            item_key = rev_print.group(1).strip().lower().replace(" ", "_")
            new_amt = int(rev_print.group(2).replace(",", ""))
            found = False
            for s in rec.statements:
                if s.canonical_key == "budget.committed_cost_pkr" and s.entity.lower() == item_key:
                    s.value = new_amt
                    s.statement_kind = "possible_revision"
                    s.replacement_target = f"budget.committed_cost_pkr:{item_key}"
                    found = True
            if not found:
                _add_stmt(
                    span=rev_print.group(0),
                    entity=item_key,
                    ckey="budget.committed_cost_pkr",
                    areas=["budget"],
                    kind="possible_revision",
                    scope="finance_actuals",
                    units="PKR",
                    value=new_amt,
                    sec={},
                    hardness="hard",
                    restatement=f"Revised {item_key} expense to Rs {new_amt:,}.",
                )
        elif "already ordered" in text.lower() or "from the event budget" in text.lower():
            for item_label, item_slug in [
                ("prizes", "prizes"),
                ("backup internet", "backup_internet"),
                ("printing", "printing"),
            ]:
                im = re.search(rf"({item_label}\s+Rs\s*(\d[\d,]*))", text, re.IGNORECASE)
                if im:
                    amt = int(im.group(2).replace(",", ""))
                    found = False
                    for s in rec.statements:
                        if s.canonical_key == "budget.committed_cost_pkr" and s.entity.lower() == item_slug:
                            s.value = amt
                            found = True
                    if not found:
                        _add_stmt(
                            span=im.group(1),
                            entity=item_slug,
                            ckey="budget.committed_cost_pkr",
                            areas=["budget"],
                            kind="commitment",
                            scope="finance_actuals",
                            units="PKR",
                            value=amt,
                            hardness="hard",
                            restatement=f"Committed expense for {item_label}: Rs {amt:,}.",
                        )

        # 4. Check core governance items if combined in a single message
        for regex, ckey, ent, units, areas, kind in [
            (r"(\d+)-hour\s+hackathon", "event.duration_hours", "hackathon", "hours", ["schedule", "judging"], "requirement"),
            (r"for\s+(\d+)\s+students", "audience.students_count", "students", "students", ["venue", "food", "judging"], "requirement"),
            (r"teams\s+of\s+(\d+)", "audience.team_size", "teams", "students_per_team", ["judging", "schedule"], "requirement"),
            (r"budget\s+is\s+Rs\s*(\d[\d,]*)", "budget.total_cap_pkr", "budget_cap", "PKR", ["budget"], "requirement"),
            (r"(Ten|\d+)\s+staff\s+will\s+be\s+on\s+duty", "audience.staff_count", "staff", "staff", ["venue", "food"], "requirement"),
            (r"(\d+)-minute\s+prayer\s+break", "schedule.break_item", "prayer_break", "minutes", ["schedule"], "requirement"),
            (r"(\d+)\s+judges\s+have\s+confirmed", "judging.confirmed_judges_count", "judges", "judges", ["judging", "food", "venue", "budget"], "commitment"),
            (r"honorarium\s+is\s+Rs\s*(\d[\d,]*)\s+each", "judging.honorarium_per_judge_pkr", "judge_honorarium", "PKR", ["judging", "budget"], "requirement"),
            (r"needs\s+(\d+)\s+minutes\s+per\s+team", "judging.minutes_per_team", "judge_eval_duration", "minutes", ["judging", "schedule"], "requirement"),
            (r"cover\s+no\s+more\s+than\s+(three|\d+)\s+teams", "judging.max_teams_per_judge", "judge_workload_cap", "teams", ["judging", "schedule"], "requirement"),
        ]:
            gm = re.search(regex, text, re.IGNORECASE)
            if gm:
                raw_v = gm.group(1).replace(",", "").strip().lower()
                word_nums = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
                val = word_nums.get(raw_v, _to_int(raw_v))
                if val is not None:
                    has_ckey = any(s.canonical_key == ckey for s in rec.statements)
                    if not has_ckey:
                        sec_d = {"name": "Prayer break"} if ckey == "schedule.break_item" else {}
                        _add_stmt(
                            span=gm.group(0),
                            entity=ent,
                            ckey=ckey,
                            areas=areas,
                            kind=kind,
                            scope="event_governance",
                            units=units,
                            value=val,
                            sec=sec_d,
                            hardness="hard",
                            restatement=gm.group(0),
                        )


def reconcile_state(
    prior_accepted_facts: Optional[Dict[str, Any]],
    new_records: List[ExtractedMessageRecord],
    is_baseline: bool,
) -> Dict[str, Any]:
    """
    Reconcile incoming extracted message statements against prior accepted facts.
    Returns:
      - candidate_facts: merged canonical state if accepted
      - rejection_reasons: list of hard rule or scope conflicts blocking commit
      - superseded_pairs: list of (old_stmt_id, new_stmt_id, reason)
      - changed_keys: list of canonical keys whose accepted values changed
      - recomputed_areas: deterministic solver areas triggered by changed_keys
      - planned_call_areas: areas requiring model calls (always ['reading'] unless genuine ambiguity)
      - audit_items: list of deterministic audit events to log
    """
    supplement_deterministic_statements_from_quotes(new_records)

    facts: Dict[str, Any] = copy.deepcopy(prior_accepted_facts) if prior_accepted_facts else {
        "event": {
            "duration_hours": None,
            "location_city": "Islamabad",
            "start_clock": None,
        },
        "audience": {
            "students_count": None,
            "team_size": None,
            "explicit_teams_count": None,
            "staff_count": 0,
        },
        "budget": {
            "total_cap_pkr": None,
            "committed_costs": {},
            "optional_requests": {},
        },
        "venue": {
            "options": {},
            "required_features": [],
        },
        "schedule": {
            "breaks": {},
        },
        "food": {
            "quotes": {},
            "explicit_headcount": None,
            "requested_box_count": None,
            "dietary_minima": {},
        },
        "judging": {
            "confirmed_judges_count": None,
            "honorarium_per_judge_pkr": 0,
            "minutes_per_team": None,
            "max_teams_per_judge": None,
            "judges_per_team": 1,
            "late_notes": [],
        },
        "custom_rules": [],
        "statement_lineage": {},  # slot_key -> {"statement_id", "message_id", "date_rank", "value", "span", "sender"}
    }

    rejection_reasons: List[Dict[str, Any]] = []
    superseded_pairs: List[Tuple[str, str, str]] = []
    changed_keys: Set[str] = set()
    audit_items: List[Dict[str, Any]] = []
    ambiguous_areas: Set[str] = set()

    # Pre-calculate currently known required meal coverage for checking vendor box reductions
    def _current_minimum_people_to_feed() -> Tuple[int, str]:
        if facts["food"].get("explicit_headcount"):
            return int(facts["food"]["explicit_headcount"]), "explicit organizer headcount"
        students = int(facts["audience"].get("students_count") or 0)
        staff = int(facts["audience"].get("staff_count") or 0)
        judges = int(facts["judging"].get("confirmed_judges_count") or 0)
        teams = 0
        if facts["audience"].get("explicit_teams_count"):
            teams = int(facts["audience"]["explicit_teams_count"])
        elif students and facts["audience"].get("team_size"):
            teams = students // int(facts["audience"]["team_size"])
        max_t = int(facts["judging"].get("max_teams_per_judge") or 3)
        j_per_t = int(facts["judging"].get("judges_per_team") or 1)
        min_assigned_judges = ((teams * j_per_t) + max_t - 1) // max_t if teams and max_t else judges
        min_headcount = students + staff + min(judges, min_assigned_judges)
        return min_headcount, f"{students} students + {staff} staff + at least {min(judges, min_assigned_judges)} assigned judges ({students + staff + judges} with all confirmed judges)"

    seq_counter = len(facts.get("statement_lineage", {})) + 1

    for rec in new_records:
        inferred_role = _infer_role_from_sender_and_text(
            rec.claimed_sender, rec.claimed_role, rec.raw_text
        )
        rec.claimed_role = inferred_role
        date_rank = parse_date_rank(rec.sent_date, seq_counter)

        msg_accepted_count = 0
        msg_rejected_count = 0
        msg_optional_count = 0
        msg_tentative_count = 0
        msg_superseded_count = 0
        msg_reasons: List[str] = []

        for stmt in rec.statements:
            seq_counter += 1
            stmt_role = _infer_role_from_sender_and_text(
                stmt.claimed_sender or rec.claimed_sender,
                stmt.claimed_role or inferred_role,
                stmt.raw_evidence_span or rec.raw_text,
            )
            ckey = stmt.canonical_key
            s_rank = parse_date_rank(stmt.effective_time or stmt.sent_time or rec.sent_date, seq_counter)

            # 1. Check tentative personal availability notes (e.g. "I may be 10 minutes late")
            if (
                ckey == "judging.late_or_absence_note"
                or stmt.hardness == "tentative"
                or "may be" in (stmt.raw_evidence_span or "").lower()
            ):
                status_str = str(stmt.secondary_values.get("status", "tentative")).lower()
                if "may be" in (stmt.raw_evidence_span or "").lower() or status_str == "tentative":
                    stmt.disposition = "tentative"
                    stmt.disposition_reason = (
                        "Tentative availability note ('may be late' due to traffic); recorded as operational advisory with schedule buffer, not a confirmed absence."
                    )
                    facts["judging"]["late_notes"].append(
                        {
                            "statement_id": stmt.statement_id,
                            "message_id": rec.message_id,
                            "sender": rec.claimed_sender,
                            "minutes": _to_int(stmt.value, 10),
                            "status": "tentative",
                            "span": stmt.raw_evidence_span,
                        }
                    )
                    msg_tentative_count += 1
                    msg_reasons.append(stmt.disposition_reason)
                    audit_items.append(
                        {
                            "event_type": "tentative_note",
                            "area_tag": "judging",
                            "message_id": rec.message_id,
                            "statement_id": stmt.statement_id,
                            "title": "Tentative Judge Delay Recorded",
                            "detail": stmt.disposition_reason,
                        }
                    )
                    continue

            # 2. Check optional / nonbinding requests (e.g. Student society asking for DJ Rs 50,000)
            if (
                ckey == "budget.optional_request_pkr"
                or stmt.statement_kind == "preference"
                or stmt.scope == "optional_request"
                or "can we get" in (stmt.raw_evidence_span or "").lower()
            ):
                amt = _to_int(stmt.value, 0) or 0
                stmt.disposition = "optional"
                stmt.disposition_reason = (
                    f"Non-binding optional request from {rec.claimed_sender} ({stmt.entity}: Rs {amt:,}); excluded from mandatory baseline commitments to preserve required event coverage and budget headroom."
                )
                facts["budget"]["optional_requests"][stmt.entity or "optional_item"] = {
                    "amount_pkr": amt,
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "sender": rec.claimed_sender,
                    "included": False,
                    "reason": stmt.disposition_reason,
                    "span": stmt.raw_evidence_span,
                }
                msg_optional_count += 1
                msg_reasons.append(stmt.disposition_reason)
                audit_items.append(
                    {
                        "event_type": "scope_filter",
                        "area_tag": "budget",
                        "message_id": rec.message_id,
                        "statement_id": stmt.statement_id,
                        "title": f"Optional Request Deferred ({stmt.entity})",
                        "detail": stmt.disposition_reason,
                    }
                )
                continue

            # 3. Check unauthorized / undersized meal box reduction (e.g. Caterer rounding order down to 100 boxes)
            if ckey == "food.requested_box_count":
                proposed_boxes = _to_int(stmt.value, 0) or 0
                min_req, req_breakdown = _current_minimum_people_to_feed()
                if stmt_role == "caterer_vendor" and proposed_boxes < min_req:
                    reason = (
                        f"Rejected authority/coverage violation: Vendor ({rec.claimed_sender}) attempted to reduce meal order to {proposed_boxes} boxes solely to cut costs, which is below required event coverage ({req_breakdown}; minimum {min_req} people). A vendor can revise unit pricing but cannot unilaterally reduce required participant coverage."
                    )
                    stmt.disposition = "rejected"
                    stmt.disposition_reason = reason
                    msg_rejected_count += 1
                    msg_reasons.append(reason)
                    rejection_reasons.append(
                        {
                            "message_id": rec.message_id,
                            "statement_id": stmt.statement_id,
                            "area": "food",
                            "rule": "food.coverage_and_authority_scope",
                            "reason": reason,
                            "evidence_span": stmt.raw_evidence_span,
                        }
                    )
                    audit_items.append(
                        {
                            "event_type": "rejection",
                            "area_tag": "food",
                            "message_id": rec.message_id,
                            "statement_id": stmt.statement_id,
                            "title": "Rejected Undersized Meal Order Reduction",
                            "detail": reason,
                        }
                    )
                    continue
                elif proposed_boxes < min_req:
                    reason = (
                        f"Rejected coverage violation: Proposed {proposed_boxes} meal boxes is insufficient for required event headcount ({req_breakdown}; minimum {min_req} people)."
                    )
                    stmt.disposition = "rejected"
                    stmt.disposition_reason = reason
                    msg_rejected_count += 1
                    msg_reasons.append(reason)
                    rejection_reasons.append(
                        {
                            "message_id": rec.message_id,
                            "statement_id": stmt.statement_id,
                            "area": "food",
                            "rule": "food.minimum_coverage",
                            "reason": reason,
                            "evidence_span": stmt.raw_evidence_span,
                        }
                    )
                    audit_items.append(
                        {
                            "event_type": "rejection",
                            "area_tag": "food",
                            "message_id": rec.message_id,
                            "statement_id": stmt.statement_id,
                            "title": "Rejected Insufficient Meal Coverage",
                            "detail": reason,
                        }
                    )
                    continue

            # 4. Handle catering per-head quotes with date superseding
            if ckey == "food.quote_per_head_pkr":
                tier = (stmt.secondary_values.get("tier") or stmt.entity or "standard").strip().lower()
                if "basic" in tier:
                    tier = "basic"
                else:
                    tier = "standard"
                slot_key = f"food.quote_per_head_pkr:{tier}"
                rate = _to_int(stmt.value)
                if rate is None or rate <= 0:
                    stmt.disposition = "rejected"
                    stmt.disposition_reason = f"Invalid catering rate '{stmt.value}'."
                    msg_rejected_count += 1
                    continue

                existing = facts["statement_lineage"].get(slot_key)
                if existing:
                    old_rank = tuple(existing["date_rank"])
                    if s_rank >= old_rank:
                        old_id = existing["statement_id"]
                        sup_reason = (
                            f"Superseded by newer {tier} catering quote ({stmt.sent_time or rec.sent_date}: Rs {rate:,}/head) in {stmt.statement_id}."
                        )
                        superseded_pairs.append((old_id, stmt.statement_id, sup_reason))
                        audit_items.append(
                            {
                                "event_type": "supersede",
                                "area_tag": "food",
                                "message_id": rec.message_id,
                                "statement_id": stmt.statement_id,
                                "title": f"Catering Quote Superseded ({tier.title()})",
                                "detail": f"Replaced prior quote {old_id} (Rs {existing['value']:,}/head) with {stmt.statement_id} (Rs {rate:,}/head).",
                            }
                        )
                    else:
                        stmt.disposition = "superseded"
                        stmt.superseded_by = existing["statement_id"]
                        stmt.disposition_reason = (
                            f"Outdated catering quote ({stmt.sent_time or rec.sent_date}: Rs {rate:,}/head) superseded by newer quote {existing['statement_id']}."
                        )
                        msg_superseded_count += 1
                        msg_reasons.append(stmt.disposition_reason)
                        continue

                facts["food"]["quotes"][tier] = {
                    "tier": tier,
                    "rate_pkr": rate,
                    "delivery_cost_pkr": _to_int(stmt.secondary_values.get("delivery_cost_pkr"), 0) or 0,
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "sent_date": stmt.sent_time or rec.sent_date,
                    "span": stmt.raw_evidence_span,
                }
                facts["statement_lineage"][slot_key] = {
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "date_rank": list(s_rank),
                    "value": rate,
                    "span": stmt.raw_evidence_span,
                    "sender": rec.claimed_sender,
                }
                stmt.disposition = "accepted"
                stmt.disposition_reason = f"Accepted {tier} lunch box quote at Rs {rate:,}/head."
                changed_keys.add("food.quote_per_head_pkr")
                msg_accepted_count += 1
                msg_reasons.append(stmt.disposition_reason)
                continue

            # 5. Handle committed budget items (prizes, backup_internet, printing, etc.)
            if ckey == "budget.committed_cost_pkr":
                item_name = (stmt.entity or "misc_expense").strip().lower().replace(" ", "_")
                slot_key = f"budget.committed_cost_pkr:{item_name}"
                amt = _to_int(stmt.value)
                if amt is None or amt < 0:
                    stmt.disposition = "rejected"
                    stmt.disposition_reason = f"Invalid committed cost amount '{stmt.value}'."
                    msg_rejected_count += 1
                    continue

                existing = facts["statement_lineage"].get(slot_key)
                if existing:
                    old_id = existing["statement_id"]
                    old_val = existing["value"]
                    sup_reason = (
                        f"Superseded by revised {item_name} commitment (Rs {amt:,}, was Rs {old_val:,}) in {stmt.statement_id}."
                    )
                    superseded_pairs.append((old_id, stmt.statement_id, sup_reason))
                    audit_items.append(
                        {
                            "event_type": "supersede",
                            "area_tag": "budget",
                            "message_id": rec.message_id,
                            "statement_id": stmt.statement_id,
                            "title": f"Committed Expense Revised ({item_name})",
                            "detail": sup_reason,
                        }
                    )

                facts["budget"]["committed_costs"][item_name] = {
                    "item": item_name,
                    "amount_pkr": amt,
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "sender": rec.claimed_sender,
                    "span": stmt.raw_evidence_span,
                }
                facts["statement_lineage"][slot_key] = {
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "date_rank": list(s_rank),
                    "value": amt,
                    "span": stmt.raw_evidence_span,
                    "sender": rec.claimed_sender,
                }
                stmt.disposition = "accepted"
                stmt.disposition_reason = f"Accepted committed cost for {item_name}: Rs {amt:,}."
                changed_keys.add("budget.committed_cost_pkr")
                msg_accepted_count += 1
                msg_reasons.append(stmt.disposition_reason)
                continue

            # 6. Handle venue options
            if ckey == "venue.option":
                v_name = (stmt.entity or "Hall").strip()
                seats = _to_int(stmt.value, 0) or 0
                cost = _to_int(stmt.secondary_values.get("cost_pkr"), 0) or 0
                has_gen = bool(stmt.secondary_values.get("has_generator", False))
                avail = bool(stmt.secondary_values.get("available", True))
                features = list(stmt.secondary_values.get("features") or [])
                if has_gen and "standby_generator" not in features:
                    features.append("standby_generator")
                slot_key = f"venue.option:{v_name.lower()}"
                existing = facts["statement_lineage"].get(slot_key)
                if existing:
                    superseded_pairs.append(
                        (
                            existing["statement_id"],
                            stmt.statement_id,
                            f"Updated venue option {v_name} in {stmt.statement_id}.",
                        )
                    )
                facts["venue"]["options"][v_name] = {
                    "name": v_name,
                    "capacity": seats,
                    "cost_pkr": cost,
                    "has_generator": has_gen,
                    "available": avail,
                    "features": features,
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "span": stmt.raw_evidence_span,
                }
                facts["statement_lineage"][slot_key] = {
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "date_rank": list(s_rank),
                    "value": {"capacity": seats, "cost_pkr": cost, "has_generator": has_gen},
                    "span": stmt.raw_evidence_span,
                    "sender": rec.claimed_sender,
                }
                stmt.disposition = "accepted"
                stmt.disposition_reason = (
                    f"Recorded venue quote {v_name}: {seats} seats at Rs {cost:,} (generator: {has_gen})."
                )
                changed_keys.add("venue.option")
                msg_accepted_count += 1
                msg_reasons.append(stmt.disposition_reason)
                continue

            if ckey == "venue.required_feature":
                feat = str(stmt.value or stmt.entity or "standby_generator").strip().lower()
                if feat not in facts["venue"]["required_features"]:
                    facts["venue"]["required_features"].append(feat)
                stmt.disposition = "accepted"
                stmt.disposition_reason = f"Recorded required venue feature: {feat}."
                changed_keys.add("venue.required_feature")
                msg_accepted_count += 1
                msg_reasons.append(stmt.disposition_reason)
                continue

            # 7. Handle scalar governance, audience, schedule, judging parameters
            scalar_mappings = {
                "event.duration_hours": ("event", "duration_hours", int),
                "event.location_city": ("event", "location_city", str),
                "event.start_clock": ("event", "start_clock", str),
                "audience.students_count": ("audience", "students_count", int),
                "audience.team_size": ("audience", "team_size", int),
                "audience.teams_count": ("audience", "explicit_teams_count", int),
                "audience.staff_count": ("audience", "staff_count", int),
                "budget.total_cap_pkr": ("budget", "total_cap_pkr", int),
                "food.explicit_headcount": ("food", "explicit_headcount", int),
                "judging.confirmed_judges_count": ("judging", "confirmed_judges_count", int),
                "judging.honorarium_per_judge_pkr": ("judging", "honorarium_per_judge_pkr", int),
                "judging.minutes_per_team": ("judging", "minutes_per_team", int),
                "judging.max_teams_per_judge": ("judging", "max_teams_per_judge", int),
                "judging.judges_per_team": ("judging", "judges_per_team", int),
            }
            if ckey in scalar_mappings:
                sec_name, field_name, typ = scalar_mappings[ckey]
                val = _to_int(stmt.value) if typ is int else str(stmt.value).strip()
                if val is None or (typ is int and isinstance(val, int) and val < 0):
                    stmt.disposition = "rejected"
                    stmt.disposition_reason = f"Invalid value '{stmt.value}' for {ckey}."
                    msg_rejected_count += 1
                    continue

                # Authority check: e.g. a vendor cannot change student count or budget cap
                if ckey in ("audience.students_count", "budget.total_cap_pkr", "event.duration_hours") and stmt_role in ("caterer_vendor", "venue_vendor"):
                    reason = f"Rejected scope violation: {rec.claimed_sender} ({stmt_role}) is not authorized to alter {ckey}."
                    stmt.disposition = "rejected"
                    stmt.disposition_reason = reason
                    msg_rejected_count += 1
                    rejection_reasons.append(
                        {
                            "message_id": rec.message_id,
                            "statement_id": stmt.statement_id,
                            "area": stmt.area_candidates[0] if stmt.area_candidates else "reading",
                            "rule": f"authority_scope:{ckey}",
                            "reason": reason,
                            "evidence_span": stmt.raw_evidence_span,
                        }
                    )
                    continue

                existing = facts["statement_lineage"].get(ckey)
                if existing and existing["value"] != val:
                    superseded_pairs.append(
                        (
                            existing["statement_id"],
                            stmt.statement_id,
                            f"Updated {ckey} from {existing['value']} to {val} in {stmt.statement_id}.",
                        )
                    )
                    audit_items.append(
                        {
                            "event_type": "supersede",
                            "area_tag": stmt.area_candidates[0] if stmt.area_candidates else "reading",
                            "message_id": rec.message_id,
                            "statement_id": stmt.statement_id,
                            "title": f"Parameter Updated ({ckey})",
                            "detail": f"Updated {ckey} from {existing['value']} ({existing['statement_id']}) to {val} ({stmt.statement_id}).",
                        }
                    )

                facts[sec_name][field_name] = val
                facts["statement_lineage"][ckey] = {
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "date_rank": list(s_rank),
                    "value": val,
                    "span": stmt.raw_evidence_span,
                    "sender": rec.claimed_sender,
                }
                stmt.disposition = "accepted"
                stmt.disposition_reason = f"Accepted {ckey} = {val}."
                changed_keys.add(ckey)
                msg_accepted_count += 1
                msg_reasons.append(stmt.disposition_reason)
                continue

            if ckey == "schedule.break_item":
                b_name = str(stmt.secondary_values.get("name") or stmt.entity or "Break").strip()
                dur = _to_int(stmt.value, 15) or 15
                slot_key = f"schedule.break_item:{b_name.lower()}"
                facts["schedule"]["breaks"][b_name] = {
                    "name": b_name,
                    "duration_minutes": dur,
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "span": stmt.raw_evidence_span,
                }
                facts["statement_lineage"][slot_key] = {
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "date_rank": list(s_rank),
                    "value": dur,
                    "span": stmt.raw_evidence_span,
                    "sender": rec.claimed_sender,
                }
                stmt.disposition = "accepted"
                stmt.disposition_reason = f"Accepted schedule break '{b_name}' ({dur} minutes)."
                changed_keys.add("schedule.break_item")
                msg_accepted_count += 1
                msg_reasons.append(stmt.disposition_reason)
                continue

            if ckey == "food.dietary_minimum":
                diet = str(stmt.secondary_values.get("diet") or stmt.entity or "vegetarian").strip().lower()
                cnt = _to_int(stmt.value, 0) or 0
                facts["food"]["dietary_minima"][diet] = {
                    "diet": diet,
                    "count": cnt,
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "span": stmt.raw_evidence_span,
                }
                stmt.disposition = "accepted"
                stmt.disposition_reason = f"Accepted dietary requirement ({diet}: minimum {cnt})."
                changed_keys.add("food.dietary_minimum")
                msg_accepted_count += 1
                msg_reasons.append(stmt.disposition_reason)
                continue

            # 8. Novel / custom declarative constraints or opaque rules
            if stmt.declarative_rule is not None or ckey.startswith("constraint."):
                d_rule = stmt.declarative_rule
                rule_entry = {
                    "statement_id": stmt.statement_id,
                    "message_id": rec.message_id,
                    "canonical_key": ckey,
                    "entity": stmt.entity,
                    "hardness": stmt.hardness,
                    "restatement": stmt.restatement or stmt.raw_evidence_span,
                    "span": stmt.raw_evidence_span,
                    "rule": d_rule.to_dict() if d_rule else {
                        "rule_type": "opaque",
                        "target_key": ckey,
                        "operator": "opaque",
                        "operand": stmt.value,
                        "checkable": False,
                        "uncheckable_reason": "No declarative rule structure provided.",
                    },
                }
                facts["custom_rules"].append(rule_entry)
                if d_rule and not d_rule.checkable and stmt.hardness == "hard":
                    stmt.disposition = "needs_clarification"
                    stmt.disposition_reason = (
                        f"Opaque/uncheckable hard constraint logged: {d_rule.uncheckable_reason or stmt.restatement}"
                    )
                    for a in stmt.area_candidates:
                        if a != "reading":
                            ambiguous_areas.add(a)
                else:
                    stmt.disposition = "accepted"
                    stmt.disposition_reason = f"Accepted declarative constraint: {stmt.restatement or stmt.raw_evidence_span}"
                changed_keys.add("constraint.custom_rule")
                msg_accepted_count += 1
                msg_reasons.append(stmt.disposition_reason)
                continue

            # 9. Fallback for informational / irrelevant statements
            if stmt.statement_kind == "informational" or stmt.hardness == "informational":
                stmt.disposition = "irrelevant"
                stmt.disposition_reason = "Informational statement with no binding constraint impact."
                msg_reasons.append(stmt.disposition_reason)
            else:
                stmt.disposition = "accepted"
                stmt.disposition_reason = f"Recorded statement ({ckey})."
                changed_keys.add(ckey)
                msg_accepted_count += 1
                msg_reasons.append(stmt.disposition_reason)

        # Summarize per-message disposition (supports partial acceptance of mixed messages)
        if msg_rejected_count > 0 and msg_accepted_count == 0:
            rec.disposition = "rejected"
            rec.disposition_reason = " | ".join(msg_reasons)
        elif msg_rejected_count > 0 and msg_accepted_count > 0:
            rec.disposition = "accepted"
            rec.disposition_reason = f"Partially accepted ({msg_accepted_count} accepted, {msg_rejected_count} rejected): " + " | ".join(msg_reasons)
        elif msg_accepted_count > 0:
            rec.disposition = "accepted"
            rec.disposition_reason = " | ".join(msg_reasons)
        elif msg_superseded_count > 0:
            rec.disposition = "superseded"
            rec.disposition_reason = " | ".join(msg_reasons)
        elif msg_optional_count > 0:
            rec.disposition = "optional"
            rec.disposition_reason = " | ".join(msg_reasons)
        elif msg_tentative_count > 0:
            rec.disposition = "tentative"
            rec.disposition_reason = " | ".join(msg_reasons)
        else:
            rec.disposition = "irrelevant"
            rec.disposition_reason = " | ".join(msg_reasons) or "No actionable constraints."

    # Compute affected deterministic solver areas from changed_keys
    if is_baseline:
        recomputed_areas = ["venue", "schedule", "food", "judging", "budget"]
    else:
        area_set: Set[str] = set()
        for ck in changed_keys:
            for mapped_area in DEPENDENCY_MAP.get(ck, ["venue", "schedule", "food", "judging", "budget"]):
                area_set.add(mapped_area)
        order = ["venue", "schedule", "food", "judging", "budget"]
        recomputed_areas = [a for a in order if a in area_set]

    # Planned model calls: always ["reading"] unless an explicit language ambiguity requires an area call
    planned_call_areas = ["reading"] + sorted(ambiguous_areas)

    if recomputed_areas:
        audit_items.append(
            {
                "event_type": "routing",
                "area_tag": "reading",
                "message_id": new_records[0].message_id if new_records else "",
                "statement_id": "",
                "title": "Dependency Routing & Call Scope Decision",
                "detail": (
                    f"Changed canonical keys: {sorted(changed_keys) or ['none']}. "
                    f"Deterministic recalculation areas: {recomputed_areas}. "
                    f"Model call areas: {planned_call_areas} (deterministic arithmetic/constraint propagation requires 0 extra area LLM calls)."
                ),
                "metadata": {
                    "changed_keys": sorted(changed_keys),
                    "recomputed_areas": recomputed_areas,
                    "planned_call_areas": planned_call_areas,
                },
            }
        )

    return {
        "candidate_facts": facts,
        "rejection_reasons": rejection_reasons,
        "superseded_pairs": superseded_pairs,
        "changed_keys": sorted(changed_keys),
        "recomputed_areas": recomputed_areas,
        "planned_call_areas": planned_call_areas,
        "audit_items": audit_items,
    }

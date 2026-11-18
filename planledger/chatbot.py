"""Grounded 'Ask the Plan' Chatbot with dual collection separation (Student Brief Procedural Rules vs Active Runtime State)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .storage import LedgerStore

PROCEDURAL_RULES_COLLECTION: List[Dict[str, Any]] = [
    {
        "rule_id": "BRIEF-P1-FIVE-AREAS",
        "page": "Student Brief, Page 1",
        "title": "Required Five Planning Areas",
        "keywords": ["five areas", "5 areas", "venue", "schedule", "food", "judging", "budget", "konse areas", "areas"],
        "text": "The plan must cover five areas: venue, schedule, food, judging, and budget, and respect every constraint stated in the messages (Student Brief, Page 1).",
    },
    {
        "rule_id": "BRIEF-P1-SINGLE-COMMAND",
        "page": "Student Brief, Page 1",
        "title": "Single Command Ingestion & Verbatim Reading",
        "keywords": ["command", "flags", "hints", "single command", "ingest", "kese chalana", "command kya hai"],
        "text": "Read every message from a text file exactly as given and take every message in the same way through the same command with no flags, hints, or manual edits (Student Brief, Page 1).",
    },
    {
        "rule_id": "BRIEF-P1-LIMITS",
        "page": "Student Brief, Page 1",
        "title": "Hard Timing & Model Call Limits",
        "keywords": ["limit", "limits", "timeout", "deadline", "2 minutes", "1 minute", "5 model calls", "waqt", "time limit", "kitna time"],
        "text": "A full baseline run is cut off after 2 minutes (120s). An update after a new message may use at most 5 model calls and is cut off after 1 minute (60s) (Student Brief, Page 1).",
    },
    {
        "rule_id": "BRIEF-P1-P2-SCHEDULE",
        "page": "Student Brief, Pages 1–2",
        "title": "Competition Day Freeze & Judging Timetable",
        "keywords": ["freeze", "code freeze", "1:25", "1:30", "2:30", "4:30", "test run", "judge visit", "timetable", "kab freeze"],
        "text": "Build starts at 10:00. Code freeze is at 1:25 PM PKT (push code and submit repo link and commit hash; no changes after 1:25). Shared test run is at 1:30 PM PKT (whole room at once). Judges visit tables from 2:30 to 4:30 PM PKT (Student Brief, Pages 1–2).",
    },
    {
        "rule_id": "BRIEF-P2-TEST-SEQUENCE",
        "page": "Student Brief, Page 2",
        "title": "Official Test Sequence & Written Call Prediction",
        "keywords": ["volunteer", "three messages", "3 messages", "prediction", "slip", "predicting", "plausible", "wrong message", "test sequence"],
        "text": "During the 1:30 test run, a volunteer checks clean git status and commit hash, releases the unseen pack, and then releases three separate update messages one at a time (two genuine changes and one plausible but wrong in unknown order). Before one update, you write down which areas will make model calls and hand the slip to the volunteer (Student Brief, Page 2).",
    },
    {
        "rule_id": "BRIEF-P2-P3-SCORING",
        "page": "Student Brief, Pages 2–3",
        "title": "Scoring Breakdown, Bonus & Tie-Breaks",
        "keywords": ["points", "scoring", "rubric", "bonus", "tie-break", "tie break", "marks", "number", "small-model"],
        "text": "Scoring (100 pts + 10 bonus): Output 30 pts (6 checks), Catching wrong message 20 pts, System understanding 20 pts (drawn member answers 2 questions per judge, no slides), Handling change 10 pts, UI 10 pts, Predicting a change 5 pts, Token use 5 pts. Small-model bonus: 10 pts if every call uses an approved small or local laptop model. Tie-breaks in order: catching the wrong message, handling change, token use (Student Brief, Pages 2–3).",
    },
]


class GroundedChatbot:
    def __init__(self, store: LedgerStore) -> None:
        self.store = store

    def answer(self, query: str) -> Dict[str, Any]:
        q_clean = (query or "").strip()
        if not q_clean:
            return {
                "query": query,
                "collection": "none",
                "collection_label": "No Query Entered",
                "answer": "Please enter a question in English or Roman Urdu, or click one of the sample questions.",
                "citations": [],
            }

        ql = q_clean.lower()

        # Check if query is specifically about competition procedural rules from the Student Brief
        procedural_triggers = [
            "code freeze",
            "1:25",
            "1:30",
            "scoring",
            "points",
            "rubric",
            "tie-break",
            "tie break",
            "bonus",
            "volunteer",
            "prediction slip",
            "time limit",
            "2 minutes",
            "1 minute",
            "5 model calls",
            "student brief",
            "how are we judged",
            "kitne marks",
            "freeze kab",
            "predicting a change",
            "what will make model calls",
            "which areas will make model calls",
        ]
        if any(t in ql for t in procedural_triggers):
            return self._answer_procedural(q_clean, ql)

        # Otherwise query the active runtime collection (messages, plan, budget, trace, rejections)
        rev = self.store.get_latest_revision()
        runs = self.store.get_all_runs()
        calls = self.store.get_all_model_calls()
        messages = self.store.get_all_messages()
        events = self.store.get_all_audit_events()

        if not rev and not runs:
            # If no runtime state yet, check if procedural matches, else report empty state
            proc_try = self._answer_procedural(q_clean, ql, fallback_only=True)
            if proc_try:
                return proc_try
            return {
                "query": q_clean,
                "collection": "runtime_evidence",
                "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
                "answer": "No message pack has been ingested in the current session yet. Run `python -m planledger <file.txt>` to establish a baseline plan.",
                "citations": [],
            }

        # Intent 1: Rejected / wrong / superseded / optional messages (English + Roman Urdu: "galat", "reject", "kyun", "caterer", "100 boxes", "dj", "old quote")
        if any(
            w in ql
            for w in [
                "reject",
                "wrong",
                "galat",
                "conflict",
                "supersed",
                "outdated",
                "100 box",
                "rounded",
                "dj",
                "optional",
                "kyun",
                "discard",
            ]
        ):
            return self._answer_rejections_and_dispositions(
                q_clean, rev, messages, events, runs
            )

        # Intent 2: Budget / spend / printing / cost / remaining balance ("kharcha", "budget", "kitna", "paisa", "printing", "balance", "headroom")
        if any(
            w in ql
            for w in [
                "budget",
                "spend",
                "cost",
                "kharcha",
                "paisa",
                "remaining",
                "balance",
                "headroom",
                "printing",
                "prize",
                "internet",
                "total",
                "baqi",
            ]
        ):
            return self._answer_budget(q_clean, rev, messages)

        # Intent 3: Food / catering / lunch / headcount / 180 vs 177 ("khana", "lunch", "food", "caterer", "boxes", "headcount", "180", "177")
        if any(
            w in ql
            for w in [
                "food",
                "lunch",
                "khana",
                "meal",
                "box",
                "headcount",
                "180",
                "177",
                "diet",
                "delivery",
            ]
        ):
            return self._answer_food(q_clean, rev, messages)

        # Intent 4: Venue / hall / generator / seats ("venue", "hall", "generator", "room d", "seats", "jagah")
        if any(
            w in ql
            for w in [
                "venue",
                "hall",
                "room d",
                "generator",
                "seat",
                "capacity",
                "jagah",
                "power",
            ]
        ):
            return self._answer_venue(q_clean, rev, messages)

        # Intent 5: Judging / judges / teams / late / traffic ("judge", "judging", "teams", "honorarium", "late", "traffic", "srinagar")
        if any(
            w in ql
            for w in [
                "judge",
                "judging",
                "team",
                "honorarium",
                "late",
                "traffic",
                "srinagar",
                "lane",
                "round",
            ]
        ):
            return self._answer_judging(q_clean, rev, messages)

        # Intent 6: Schedule / prayer break / slots / duration ("schedule", "time", "prayer", "namaz", "break", "slot", "waqt")
        if any(
            w in ql
            for w in [
                "schedule",
                "prayer",
                "namaz",
                "break",
                "slot",
                "timeline",
                "duration",
                "4-hour",
                "clock",
            ]
        ):
            return self._answer_schedule(q_clean, rev, messages)

        # Intent 7: Trace / tokens / model calls / affected areas / revisions ("trace", "token", "model", "call", "revision", "delta", "affected", "recomputed")
        if any(
            w in ql
            for w in [
                "trace",
                "token",
                "model",
                "call",
                "revision",
                "delta",
                "change",
                "affected",
                "recomput",
                "run",
            ]
        ):
            return self._answer_trace_and_revisions(q_clean, rev, runs, calls)

        # Check procedural fallback before reporting unverified
        proc_try = self._answer_procedural(q_clean, ql, fallback_only=True)
        if proc_try:
            return proc_try

        return {
            "query": q_clean,
            "collection": "runtime_evidence",
            "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
            "answer": (
                f"Cannot verify '{q_clean}' from the currently ingested message evidence or the Student Brief procedural rules. "
                "PlanLedger never fabricates or guesses facts outside stored source messages. "
                "Try asking about venue selection, budget arithmetic, meal headcount (180 vs 177), judge allocation, rejected updates, superseded quotes, token usage, or Student Brief rules."
            ),
            "citations": [],
        }

    def _answer_procedural(
        self, query: str, ql: str, fallback_only: bool = False
    ) -> Optional[Dict[str, Any]]:
        scored = []
        for item in PROCEDURAL_RULES_COLLECTION:
            score = sum(2 for kw in item["keywords"] if kw in ql)
            for word in re.findall(r"[a-z0-9]+", ql):
                if len(word) > 3 and word in item["text"].lower():
                    score += 1
            if score > 0:
                scored.append((score, item))

        if not scored and fallback_only:
            return None

        if not scored:
            scored = [(1, r) for r in PROCEDURAL_RULES_COLLECTION[:3]]
        else:
            scored.sort(key=lambda x: -x[0])

        top_items = [x[1] for x in scored[:2]]
        ans_parts = [f"- **{it['title']} ({it['page']})**: {it['text']}" for it in top_items]
        if "predict" in ql or "model call" in ql or "volunteer" in ql:
            ans_parts.append(
                "- **PlanLedger Call Prediction Rule**: In PlanLedger, every ordinary update makes **1 model call tagged `reading`** and **0 area model calls**, because venue/schedule/food/judging/budget propagation is solved deterministically in plain Python code."
            )

        return {
            "query": query,
            "collection": "student_brief_rules",
            "collection_label": "Collection 1: Student Brief Procedural Rules (Page-Referenced)",
            "answer": "\n".join(ans_parts),
            "citations": [
                {"ref": it["rule_id"], "source": it["page"], "quote": it["text"]}
                for it in top_items
            ],
        }

    def _answer_rejections_and_dispositions(
        self,
        query: str,
        rev: Optional[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
        runs: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        rejected_msgs = [m for m in messages if m["disposition"] == "rejected"]
        superseded_stmts = [
            s
            for m in messages
            for s in (m.get("statements") or [])
            if s.get("disposition") == "superseded"
        ]
        optional_msgs = [m for m in messages if m["disposition"] == "optional"]
        tentative_msgs = [m for m in messages if m["disposition"] == "tentative"]

        lines: List[str] = []
        citations: List[Dict[str, str]] = []

        if rejected_msgs:
            lines.append("**Rejected Messages (Plan Kept Valid at Prior Revision):**")
            for m in rejected_msgs:
                lines.append(
                    f"- **{m['message_id']}** (From: {m['claimed_sender']}): *{m['disposition_reason']}*"
                )
                citations.append(
                    {
                        "ref": m["message_id"],
                        "source": f"Run #{m['run_id']} ({m['claimed_sender']})",
                        "quote": m["raw_text"],
                    }
                )
        else:
            lines.append("- **Rejected Messages**: None recorded so far.")

        if superseded_stmts:
            lines.append("\n**Superseded Statements (Replaced by Newer Valid Evidence):**")
            for s in superseded_stmts:
                lines.append(
                    f"- **{s['statement_id']}** (in {s['origin_message_id']}): {s['disposition_reason']} (Superseded by `{s.get('superseded_by', '')}`)"
                )
                citations.append(
                    {
                        "ref": s["statement_id"],
                        "source": f"Message {s['origin_message_id']} ({s.get('sent_time', '')})",
                        "quote": s.get("raw_evidence_span", ""),
                    }
                )

        if optional_msgs:
            lines.append("\n**Optional / Non-Binding Requests Deferred:**")
            for m in optional_msgs:
                lines.append(f"- **{m['message_id']}** ({m['claimed_sender']}): {m['disposition_reason']}")
                citations.append(
                    {
                        "ref": m["message_id"],
                        "source": f"Message {m['message_id']}",
                        "quote": m["raw_text"],
                    }
                )

        if tentative_msgs:
            lines.append("\n**Tentative Notes Handled via Buffers:**")
            for m in tentative_msgs:
                lines.append(f"- **{m['message_id']}** ({m['claimed_sender']}): {m['disposition_reason']}")

        if rev:
            lines.append(
                f"\nActive Valid Revision: **R{rev['revision_id']} ({rev['validation_grade']})**."
            )

        return {
            "query": query,
            "collection": "runtime_evidence",
            "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
            "answer": "\n".join(lines),
            "citations": citations[:6],
        }

    def _answer_budget(
        self,
        query: str,
        rev: Optional[Dict[str, Any]],
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not rev:
            return {
                "query": query,
                "collection": "runtime_evidence",
                "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
                "answer": "No committed plan revision exists yet.",
                "citations": [],
            }
        b = rev["plan"]["budget"]
        lines = [
            f"**Budget Summary — Revision R{rev['revision_id']} ({rev['validation_grade']}):**",
            f"- **Total Cap**: Rs {b['budget_cap_pkr']:,}",
            f"- **Total Committed Spend**: Rs {b['total_spend_pkr']:,}",
            f"- **Remaining Balance (Headroom)**: Rs {b['remaining_balance_pkr']:,}",
            f"- *(If only {rev['plan']['judging']['minimum_judges_required']} assigned judges eat [177 boxes]: Spend = Rs {b['alternate_177_boxes_spend_pkr']:,}, Remaining = Rs {b['alternate_177_boxes_remaining_pkr']:,})*",
            "\n**Itemized Breakdown:**",
        ]
        citations = []
        for li in b["line_items"]:
            lines.append(
                f"- **{li['item']}** ({li['category']}): {li['quantity']} {li['unit']} x Rs {li['rate_pkr']:,} = **Rs {li['line_total_pkr']:,}** [Ref: `{li['reference']}`]"
            )
            if li.get("reference"):
                citations.append(
                    {
                        "ref": li["reference"],
                        "source": li["category"],
                        "quote": f"{li['item']}: Rs {li['line_total_pkr']:,}",
                    }
                )
        return {
            "query": query,
            "collection": "runtime_evidence",
            "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
            "answer": "\n".join(lines),
            "citations": citations[:6],
        }

    def _answer_food(
        self,
        query: str,
        rev: Optional[Dict[str, Any]],
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not rev:
            return {
                "query": query,
                "collection": "runtime_evidence",
                "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
                "answer": "No committed plan revision exists yet.",
                "citations": [],
            }
        f = rev["plan"]["food"]
        hr = f["headcount_reconciliation"]
        lines = [
            f"**Food & Catering Plan — Revision R{rev['revision_id']}:**",
            f"- **Selected Option**: {str(f['selected_tier']).title()} Lunch Box @ **Rs {f['rate_per_head_pkr']:,}/head** (Delivery: Rs {f['delivery_cost_pkr']:,})",
            f"- **Quantity Ordered**: **{f['headcount_ordered']} boxes** (Total: **Rs {f['total_food_cost_pkr']:,}**)",
            f"- **Headcount Reconciliation (180 vs 177)**: {hr['discrepancy_explanation']}",
        ]
        citations = []
        for q in f.get("available_quotes", []):
            citations.append(
                {
                    "ref": q.get("statement_id", ""),
                    "source": f"Message {q.get('message_id', '')} ({q.get('sent_date', '')})",
                    "quote": q.get("span", ""),
                }
            )
        return {
            "query": query,
            "collection": "runtime_evidence",
            "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
            "answer": "\n".join(lines),
            "citations": citations,
        }

    def _answer_venue(
        self,
        query: str,
        rev: Optional[Dict[str, Any]],
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not rev:
            return {
                "query": query,
                "collection": "runtime_evidence",
                "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
                "answer": "No committed plan revision exists yet.",
                "citations": [],
            }
        v = rev["plan"]["venue"]
        lines = [
            f"**Venue Plan — Revision R{rev['revision_id']}:**",
            f"- **Selected Venue**: **{v['selected_venue']}** ({v['capacity']} seats, Rs {v['cost_pkr']:,}, Standby Generator: {'Yes' if v['has_generator'] else 'No'})",
            f"- **Required Simultaneous Occupancy**: {v['required_occupancy']} people ({v['occupancy_breakdown']['students']} students + {v['occupancy_breakdown']['staff']} staff + {v['occupancy_breakdown']['confirmed_judges']} confirmed judges)",
            f"- **Rationale**: {v['rationale']}",
            "\n**All Venue Options Evaluated:**",
        ]
        citations = []
        for alt in v.get("alternatives_evaluated", []):
            lines.append(
                f"- **{alt['name']}** ({alt['capacity']} seats, Rs {alt['cost_pkr']:,}): [{alt['status'].upper()}] {alt['selection_note']}"
            )
            if alt.get("statement_id"):
                citations.append(
                    {
                        "ref": alt["statement_id"],
                        "source": f"Message {alt.get('message_id', '')}",
                        "quote": f"{alt['name']}: {alt['capacity']} seats at Rs {alt['cost_pkr']:,}",
                    }
                )
        return {
            "query": query,
            "collection": "runtime_evidence",
            "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
            "answer": "\n".join(lines),
            "citations": citations,
        }

    def _answer_judging(
        self,
        query: str,
        rev: Optional[Dict[str, Any]],
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not rev:
            return {
                "query": query,
                "collection": "runtime_evidence",
                "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
                "answer": "No committed plan revision exists yet.",
                "citations": [],
            }
        j = rev["plan"]["judging"]
        lines = [
            f"**Judging Allocation — Revision R{rev['revision_id']}:**",
            f"- **Teams to Evaluate**: {j['teams_count']} teams (teams of {j['team_size']})",
            f"- **Confirmed Judges**: {j['confirmed_judges']} confirmed (@ Rs {j['honorarium_per_judge_pkr']:,} = Rs {j['total_honorarium_pkr']:,})",
            f"- **Minimum Judges Required**: {j['minimum_judges_required']} judges (max {j['max_teams_per_judge']} teams/judge, {j['minutes_per_team']} mins/team)",
            f"- **Active vs Reserve**: {j['active_judges_assigned']} primary judges run concurrent lanes across {j['rounds_count']} rounds ({j['total_judging_window_minutes']} mins total); {j['reserve_judges_on_site']} on-site reserve judges absorb any traffic delay.",
        ]
        for r in j.get("rounds", []):
            lines.append(
                f"- **Round {r['round']}** ({r.get('time_window', '')}): {r['teams_covered']} teams ({r['team_range']}) across {r['parallel_lanes']} lanes"
            )
        citations = []
        for ln in j.get("late_notes_handled", []):
            citations.append(
                {
                    "ref": ln.get("statement_id", ""),
                    "source": f"Message {ln.get('message_id', '')} ({ln.get('sender', '')})",
                    "quote": ln.get("span", ""),
                }
            )
        return {
            "query": query,
            "collection": "runtime_evidence",
            "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
            "answer": "\n".join(lines),
            "citations": citations,
        }

    def _answer_schedule(
        self,
        query: str,
        rev: Optional[Dict[str, Any]],
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not rev:
            return {
                "query": query,
                "collection": "runtime_evidence",
                "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
                "answer": "No committed plan revision exists yet.",
                "citations": [],
            }
        s = rev["plan"]["schedule"]
        lines = [
            f"**Schedule & Timeline — Revision R{rev['revision_id']}:**",
            f"- **Duration**: {s['duration_hours']} hours ({s['scheduled_minutes']} minutes scheduled)",
            f"- **Clock Status**: {s['clock_time_status']}",
            f"- **Mandatory Break**: {s['prayer_break_minutes']}-minute Prayer Break included",
            "\n**Slots:**",
        ]
        for b in s.get("blocks", []):
            lines.append(
                f"- **Slot {b['slot_index']} ({b['time_window']}, {b['duration_minutes']}m)**: {b['title']} — *{b['notes']}*"
            )
        return {
            "query": query,
            "collection": "runtime_evidence",
            "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
            "answer": "\n".join(lines),
            "citations": [],
        }

    def _answer_trace_and_revisions(
        self,
        query: str,
        rev: Optional[Dict[str, Any]],
        runs: List[Dict[str, Any]],
        calls: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        tot_in = sum(int(c["input_tokens"] or 0) for c in calls)
        tot_out = sum(int(c["output_tokens"] or 0) for c in calls)
        tot_all = sum(int(c["total_tokens"] or 0) for c in calls)
        lines = [
            f"**Session Trace & Revision Overview:**",
            f"- **Total Ingestion Runs**: {len(runs)}",
            f"- **Total Model Calls**: {len(calls)} (Cumulative Tokens: input={tot_in}, output={tot_out}, total={tot_all})",
        ]
        if rev:
            d = rev.get("delta") or {}
            lines.append(
                f"- **Current Active Revision**: R{rev['revision_id']} ({rev['validation_grade']})"
            )
            lines.append(
                f"- **Latest Revision Routing**: Planned Call Areas = `{d.get('planned_call_areas', [])}`, Observed Call Areas = `{d.get('observed_call_areas', [])}`, Recomputed Deterministic Areas = `{d.get('recomputed_areas', [])}`"
            )
            for fc in d.get("field_changes", []):
                lines.append(
                    f"  * [{fc['area'].upper()}] {fc['field']}: `{fc['before']}` -> `{fc['after']}`"
                )
        citations = [
            {
                "ref": f"Call #{c['call_number']}",
                "source": f"Run #{c['run_id']} | area={c['area_tag']} | model={c.get('reported_model') or c['requested_model']}",
                "quote": f"Outcome: {c['outcome']} | Elapsed: {c['elapsed_sec']:.2f}s | Tokens: {c.get('total_tokens')}",
            }
            for c in calls
        ]
        return {
            "query": query,
            "collection": "runtime_evidence",
            "collection_label": "Collection 2: Active Runtime Evidence & Committed Plan",
            "answer": "\n".join(lines),
            "citations": citations[:6],
        }

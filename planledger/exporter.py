"""Terminal trace formatter and durable artifact exporter for PlanLedger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .storage import LedgerStore


def format_model_call_entry(call: Dict[str, Any]) -> str:
    cnum = call["call_number"]
    area = call["area_tag"]
    req_model = call["requested_model"]
    rep_model = call.get("reported_model")
    model_str = (
        f"{rep_model} (requested: {req_model})"
        if rep_model and rep_model != req_model
        else (rep_model or f"unavailable [requested: {req_model}]")
    )
    in_tok = call.get("input_tokens")
    out_tok = call.get("output_tokens")
    tot_tok = call.get("total_tokens")
    in_s = str(in_tok) if in_tok is not None else "unavailable"
    out_s = str(out_tok) if out_tok is not None else "unavailable"
    tot_s = str(tot_tok) if tot_tok is not None else "unavailable"
    sub = call.get("subcounts") or {}
    sub_s = json.dumps(sub) if sub else "none"

    lines = [
        f"------------------------------------------------------------------------",
        f"MODEL CALL #{cnum} | AREA TAG: {area} | OUTCOME: {call['outcome'].upper()}",
        f"Provider: {call['provider']} | Model Identity: {model_str}",
        f"Attempt: #{call['attempt_number']} ({call['attempt_relation']}) | Elapsed: {call['elapsed_sec']:.3f}s",
        f"Tokens (Provider-Reported): input={in_s} | output={out_s} | total={tot_s}",
        f"Provider Subcounts: {sub_s} | Usage Note: {call.get('usage_note', '')}",
    ]
    if call.get("error_detail"):
        lines.append(f"Error / Diagnostic: {call['error_detail']}")
    lines.append("--- MODEL INPUT PAYLOAD ---")
    lines.append(call.get("input_payload", "").strip())
    lines.append("--- MODEL OUTPUT CONTENT ---")
    lines.append(call.get("output_content", "").strip() or "(empty)")
    lines.append("------------------------------------------------------------------------")
    return "\n".join(lines)


def format_plan_summary_text(
    revision: Dict[str, Any],
    run_info: Optional[Dict[str, Any]] = None,
) -> str:
    plan = revision["plan"]
    val = revision["validation"]
    delta = revision.get("delta") or {}
    rev_id = revision["revision_id"]
    grade = revision["validation_grade"]

    v = plan["venue"]
    s = plan["schedule"]
    f = plan["food"]
    j = plan["judging"]
    b = plan["budget"]

    lines = [
        "========================================================================",
        f"PLANLEDGER VALIDATED PLAN — REVISION R{rev_id} ({grade})",
        "========================================================================",
    ]
    if run_info:
        lines.append(
            f"Run #{run_info['run_id']} ({run_info['run_type'].upper()}) | Source: {run_info['filename']} | Status: {run_info['status'].upper()}"
        )
        lines.append(
            f"Planned Call Areas: {run_info.get('planned_call_areas', [])} | Observed Call Areas: {run_info.get('observed_call_areas', [])} | Recomputed Areas: {run_info.get('recomputed_areas', [])}"
        )

    lines.extend(
        [
            "",
            "[1. VENUE]",
            f"  Selected Venue      : {v['selected_venue']} ({v['location_city']})",
            f"  Capacity vs Need    : {v['capacity']} seats vs {v['required_occupancy']} simultaneous on-site ({v['occupancy_breakdown']['students']} students + {v['occupancy_breakdown']['staff']} staff + {v['occupancy_breakdown']['confirmed_judges']} confirmed judges)",
            f"  Standby Generator   : {'Yes (Compliant)' if v['has_generator'] else 'No'}",
            f"  Venue Cost          : Rs {v['cost_pkr']:,}",
            f"  Selection Rationale : {v['rationale']}",
            "  Alternatives Evaluated:",
        ]
    )
    for alt in v.get("alternatives_evaluated", []):
        lines.append(
            f"    - {alt['name']} ({alt['capacity']} seats, Rs {alt['cost_pkr']:,}, generator={alt['has_generator']}): [{alt['status'].upper()}] {alt['selection_note']}"
        )

    lines.extend(
        [
            "",
            "[2. SCHEDULE]",
            f"  Timezone & Clock    : {s['timezone']} — {s['clock_time_status']}",
            f"  Total Window        : {s['duration_hours']} hours ({s['scheduled_minutes']} / {s['total_minutes']} minutes scheduled)",
            f"  Mandatory Break     : {s['prayer_break_minutes']} minutes (Prayer Break)",
            "  Timeline Slots:",
        ]
    )
    for slot in s.get("blocks", []):
        lines.append(
            f"    Slot {slot['slot_index']}: {slot['time_window']} ({slot['duration_minutes']}m) — {slot['title']}"
        )
        lines.append(
            f"            Parallel Work: {slot['parallel_work']} | Notes: {slot['notes']}"
        )

    lines.extend(
        [
            "",
            "[3. FOOD & CATERING]",
            f"  Selected Tier       : {str(f['selected_tier']).title()} Lunch Box @ Rs {f['rate_per_head_pkr']:,}/head (Delivery: Rs {f['delivery_cost_pkr']:,})",
            f"  Quantity Ordered    : {f['headcount_ordered']} boxes (Total: Rs {f['total_food_cost_pkr']:,})",
            f"  Coverage Basis      : {f['coverage_basis']}",
            f"  Headcount Note      : {f['headcount_reconciliation']['discrepancy_explanation']}",
            "",
            "[4. JUDGING]",
            f"  Teams & Format      : {j['teams_count']} teams (teams of {j['team_size']}); {j['minutes_per_team']} mins/team; max {j['max_teams_per_judge']} teams/judge",
            f"  Judge Allocation    : {j['confirmed_judges']} confirmed judges ({j['active_judges_assigned']} primary lane evaluators + {j['reserve_judges_on_site']} on-site reserve/floater judges)",
            f"  Judging Window      : {j['rounds_count']} parallel rounds ({j['total_judging_window_minutes']} minutes total)",
            f"  Honoraria           : {j['confirmed_judges']} judges x Rs {j['honorarium_per_judge_pkr']:,} = Rs {j['total_honorarium_pkr']:,}",
        ]
    )
    for r in j.get("rounds", []):
        lines.append(
            f"    Round {r['round']} ({r.get('time_window', '')}, {r['duration_minutes']}m): {r['teams_covered']} teams in parallel ({r['team_range']})"
        )

    lines.extend(
        [
            "",
            "[5. BUDGET]",
            f"  Total Budget Cap    : Rs {b['budget_cap_pkr']:,}",
            f"  Total Committed     : Rs {b['total_spend_pkr']:,}",
            f"  Remaining Balance   : Rs {b['remaining_balance_pkr']:,}",
            f"  (Alternate 177-box spend if only {j['minimum_judges_required']} assigned judges eat: Rs {b['alternate_177_boxes_spend_pkr']:,}, remaining Rs {b['alternate_177_boxes_remaining_pkr']:,})",
            "  Itemized Ledger:",
        ]
    )
    for li in b.get("line_items", []):
        lines.append(
            f"    - [{li['category']}] {li['item']}: {li['quantity']} {li['unit']} x Rs {li['rate_pkr']:,} = Rs {li['line_total_pkr']:,} (Ref: {li['reference']})"
        )
    if b.get("optional_requests_excluded"):
        lines.append("  Excluded Non-Binding / Optional Requests:")
        for opt in b["optional_requests_excluded"]:
            lines.append(
                f"    - {opt['sender']} ({opt['message_id']}): Rs {opt['amount_pkr']:,} — {opt['reason']}"
            )

    lines.extend(["", "[INDEPENDENT VALIDATION CHECKS]"])
    for chk in val.get("checks", []):
        mark = "PASS" if chk["passed"] else "FAIL"
        lines.append(f"  [{mark}] {chk['label']}: {chk['detail']}")

    if delta.get("field_changes"):
        lines.extend(["", "[REVISION DELTA]"])
        for fc in delta["field_changes"]:
            lines.append(
                f"  - ({fc['area'].upper()}) {fc['field']}: {fc['before']} -> {fc['after']}"
            )

    return "\n".join(lines)


def format_run_trace(
    run_row: Dict[str, Any],
    calls: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    active_revision: Optional[Dict[str, Any]],
) -> str:
    """Format the complete auditable stdout trace for a single ingestion run."""
    lines = [
        "========================================================================",
        f"PLANLEDGER EXECUTION TRACE — RUN #{run_row['run_id']} ({run_row['run_type'].upper()})",
        f"Input File : {run_row['filename']} | SHA-256: {run_row['sha256'][:16]}...",
        f"Run Status : {run_row['status'].upper()} | Elapsed: {run_row['elapsed_sec']:.3f}s",
        f"Planned Call Areas  : {', '.join(run_row.get('planned_call_areas') or ['none'])}",
        f"Observed Call Areas : {', '.join(run_row.get('observed_call_areas') or ['none'])}",
        f"Recomputed Areas    : {', '.join(run_row.get('recomputed_areas') or ['none'])}",
        "========================================================================",
    ]

    if calls:
        lines.append("\n[MODEL CALL TRACE]")
        for c in calls:
            lines.append(format_model_call_entry(c))
    else:
        lines.append("\n[MODEL CALL TRACE]\nNo model calls dispatched in this run.")

    if events:
        lines.append("\n[DETERMINISTIC RECONCILIATION, ROUTING & CONFLICT EVENTS]")
        for ev in events:
            ref = f" (Msg: {ev['message_id']}, Stmt: {ev['statement_id']})" if ev.get("message_id") else ""
            lines.append(
                f"  * [{ev['event_type'].upper()} | area={ev['area_tag']}]{ref} {ev['title']}: {ev['detail']}"
            )

    if active_revision:
        lines.append("")
        lines.append(format_plan_summary_text(active_revision, run_row))

    # Calculate run token totals without estimating or double-counting
    in_total = 0
    out_total = 0
    grand_total = 0
    any_partial = False
    for c in calls:
        if (
            c.get("input_tokens") is not None
            and c.get("output_tokens") is not None
            and c.get("total_tokens") is not None
            and c.get("usage_complete", True)
        ):
            in_total += int(c["input_tokens"])
            out_total += int(c["output_tokens"])
            grand_total += int(c["total_tokens"])
        else:
            any_partial = True
            if c.get("input_tokens") is not None:
                in_total += int(c["input_tokens"])
            if c.get("output_tokens") is not None:
                out_total += int(c["output_tokens"])
            if c.get("total_tokens") is not None:
                grand_total += int(c["total_tokens"])

    partial_tag = " (PARTIAL - one or more calls omitted provider token counts)" if any_partial else ""
    lines.extend(
        [
            "",
            "========================================================================",
            f"RUN #{run_row['run_id']} TOKEN SUMMARY{partial_tag}",
            f"Model Calls in Run : {len(calls)}",
            f"Run Input Tokens   : {in_total}",
            f"Run Output Tokens  : {out_total}",
            f"TOTAL TOKENS (RUN) : {grand_total}{partial_tag}",
            "========================================================================",
        ]
    )
    return "\n".join(lines)


def write_durable_exports(store: LedgerStore, exports_dir: Path) -> Dict[str, Path]:
    """Write cumulative readable combined export, separate plan/trace texts, and JSON bundle."""
    exports_dir.mkdir(parents=True, exist_ok=True)
    runs = store.get_all_runs()
    calls = store.get_all_model_calls()
    events = store.get_all_audit_events()
    messages = store.get_all_messages()
    revisions = store.get_all_revisions()
    latest_rev = revisions[-1] if revisions else None

    # Calculate cumulative session token totals
    cum_in = 0
    cum_out = 0
    cum_total = 0
    cum_partial = False
    for c in calls:
        if (
            c.get("input_tokens") is not None
            and c.get("output_tokens") is not None
            and c.get("total_tokens") is not None
            and c.get("usage_complete", True)
        ):
            cum_in += int(c["input_tokens"])
            cum_out += int(c["output_tokens"])
            cum_total += int(c["total_tokens"])
        else:
            cum_partial = True
            if c.get("input_tokens") is not None:
                cum_in += int(c["input_tokens"])
            if c.get("output_tokens") is not None:
                cum_out += int(c["output_tokens"])
            if c.get("total_tokens") is not None:
                cum_total += int(c["total_tokens"])

    partial_suffix = " (PARTIAL)" if cum_partial else ""

    # 1. Final Plan text
    plan_text = (
        format_plan_summary_text(latest_rev, runs[-1] if runs else None)
        if latest_rev
        else "No active validated plan revision committed yet."
    )
    plan_file = exports_dir / "final_plan.txt"
    plan_file.write_text(plan_text + "\n", encoding="utf-8")

    # 2. Full cumulative trace text
    trace_sections: List[str] = [
        "========================================================================",
        "PLANLEDGER CUMULATIVE SESSION AUDIT & MODEL CALL TRACE",
        "========================================================================",
    ]
    for r in runs:
        r_calls = [c for c in calls if c["run_id"] == r["run_id"]]
        r_events = [e for e in events if e["run_id"] == r["run_id"]]
        rev_after = (
            store.get_revision(int(r["active_revision_after"]))
            if r.get("active_revision_after")
            else None
        )
        trace_sections.append(format_run_trace(r, r_calls, r_events, rev_after))

    # Rejected / Superseded / Optional message summary
    trace_sections.extend(
        [
            "",
            "========================================================================",
            "REJECTED, SUPERSEDED, OPTIONAL & TENTATIVE MESSAGE LEDGER",
            "========================================================================",
        ]
    )
    non_accepted = [m for m in messages if m.get("disposition") != "accepted"]
    if non_accepted:
        for m in non_accepted:
            trace_sections.append(
                f"- [{m['disposition'].upper()}] {m['message_id']} (Run #{m['run_id']}, From: {m['claimed_sender']}, Date: {m['sent_date']})"
            )
            trace_sections.append(f"  Raw Text : {m['raw_text']}")
            trace_sections.append(f"  Reason   : {m['disposition_reason']}")
    else:
        trace_sections.append("None recorded.")

    trace_sections.extend(
        [
            "",
            "========================================================================",
            f"CUMULATIVE SESSION TOKEN TOTALS{partial_suffix}",
            f"Total Ingestion Runs     : {len(runs)}",
            f"Total Model Calls        : {len(calls)}",
            f"Cumulative Input Tokens  : {cum_in}",
            f"Cumulative Output Tokens : {cum_out}",
            f"TOTAL TOKENS (SESSION)   : {cum_total}{partial_suffix}",
            "========================================================================",
        ]
    )
    full_trace_text = "\n".join(trace_sections)
    trace_file = exports_dir / "full_trace.txt"
    trace_file.write_text(full_trace_text + "\n", encoding="utf-8")

    # 3. Combined final submission file (final plan + rejected explanations + complete trace + total tokens)
    combined_text = (
        "########################################################################\n"
        "# PLANLEDGER FINAL SUBMISSION ARTIFACT (PLAN + REJECTIONS + FULL TRACE)\n"
        "########################################################################\n\n"
        + plan_text
        + "\n\n"
        + full_trace_text
        + "\n"
    )
    combined_file = exports_dir / "planledger_final_submission.txt"
    combined_file.write_text(combined_text, encoding="utf-8")

    # 4. JSON export
    json_bundle = {
        "latest_revision": latest_rev,
        "revisions": revisions,
        "runs": runs,
        "model_calls": calls,
        "audit_events": events,
        "messages": messages,
        "cumulative_tokens": {
            "total_calls": len(calls),
            "input_tokens": cum_in,
            "output_tokens": cum_out,
            "total_tokens": cum_total,
            "is_partial": cum_partial,
        },
    }
    json_file = exports_dir / "planledger_state.json"
    json_file.write_text(
        json.dumps(json_bundle, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "combined": combined_file,
        "plan": plan_file,
        "trace": trace_file,
        "json": json_file,
    }

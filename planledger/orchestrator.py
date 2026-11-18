"""Unified orchestration pipeline for PlanLedger (shared by CLI and loopback UI)."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .config import AppConfig
from .exporter import format_run_trace, write_durable_exports
from .provider import ProviderAdapter, ProviderResponse
from .reader import execute_reading_with_bounded_retry
from .reconciler import reconcile_state
from .solver import compute_revision_delta, solve_and_validate
from .storage import LedgerStore


class PlanLedgerOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        custom_transport: Optional[
            Callable[[str, str, str, float, AppConfig], ProviderResponse]
        ] = None,
    ) -> None:
        self.config = config
        self.store = LedgerStore(config.db_path)
        self.adapter = ProviderAdapter(config, custom_transport=custom_transport)

    def ingest_file(self, file_path: Path) -> Dict[str, Any]:
        """
        Ingest a raw text file through the single unified pipeline.
        Automatically detects baseline vs incremental update from durable session state.
        """
        t_start = time.monotonic()
        if not file_path.is_file():
            raise FileNotFoundError(f"Input file not found: {file_path}")

        raw_bytes = file_path.read_bytes()
        return self.ingest_bytes(
            raw_bytes=raw_bytes,
            filename=file_path.name,
            t_start=t_start,
        )

    def ingest_bytes(
        self,
        raw_bytes: bytes,
        filename: str,
        t_start: Optional[float] = None,
    ) -> Dict[str, Any]:
        t0 = t_start if t_start is not None else time.monotonic()
        sha256 = hashlib.sha256(raw_bytes).hexdigest()

        # Decode UTF-8 with explicit error logging while preserving original bytes
        encoding_errors = ""
        try:
            raw_text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            encoding_errors = f"UTF-8 decode error at byte {exc.start}: {exc.reason}"
            raw_text = raw_bytes.decode("utf-8", errors="replace")

        has_base = self.store.has_baseline()
        run_type = "update" if has_base else "baseline"
        hard_limit = (
            self.config.update_hard_limit_sec
            if has_base
            else self.config.baseline_hard_limit_sec
        )
        budget_limit = (
            min(hard_limit - 1.5, self.config.update_budget_sec)
            if has_base
            else min(hard_limit - 2.0, self.config.baseline_budget_sec)
        )
        deadline_monotonic = t0 + budget_limit

        prior_rev = self.store.get_latest_revision()
        prior_rev_id = prior_rev["revision_id"] if prior_rev else None

        # Check for duplicate file upload
        existing_dup = self.store.find_run_by_hash(sha256)
        if existing_dup is not None:
            run_id = self.store.start_run(
                run_type=run_type,
                filename=filename,
                sha256=sha256,
                raw_bytes=raw_bytes,
                raw_text=raw_text,
                encoding_errors=encoding_errors,
                is_duplicate_of=int(existing_dup["run_id"]),
            )
            dup_msg = (
                f"Duplicate input detected: SHA-256 ({sha256[:16]}...) matches Run #{existing_dup['run_id']} "
                f"({existing_dup['filename']}). Logged safely without mutating active revision R{prior_rev_id} or wasting model calls."
            )
            self.store.record_audit_event(
                run_id=run_id,
                event_type="duplicate_upload",
                area_tag="reading",
                title="Duplicate Input Safely Ignored",
                detail=dup_msg,
            )
            elapsed = time.monotonic() - t0
            run_row = {
                "run_id": run_id,
                "run_type": run_type,
                "filename": filename,
                "sha256": sha256,
                "elapsed_sec": elapsed,
                "status": "duplicate_ignored",
                "status_reason": dup_msg,
                "planned_call_areas": [],
                "observed_call_areas": [],
                "recomputed_areas": [],
                "active_revision_after": prior_rev_id,
            }
            events = self.store.get_all_audit_events(run_id=run_id)
            trace_text = format_run_trace(run_row, [], events, prior_rev)
            self.store.finish_run(
                run_id=run_id,
                elapsed_sec=elapsed,
                status="duplicate_ignored",
                status_reason=dup_msg,
                planned_call_areas=[],
                observed_call_areas=[],
                recomputed_areas=[],
                active_revision_after=prior_rev_id,
                run_trace_text=trace_text,
                summary={"duplicate_of": existing_dup["run_id"]},
            )
            write_durable_exports(self.store, self.config.exports_dir)
            return {
                "run_id": run_id,
                "status": "duplicate_ignored",
                "trace_text": trace_text,
                "active_revision": prior_rev,
            }

        run_id = self.store.start_run(
            run_type=run_type,
            filename=filename,
            sha256=sha256,
            raw_bytes=raw_bytes,
            raw_text=raw_text,
            encoding_errors=encoding_errors,
        )

        # Reject empty or binary-corrupted input immediately
        if encoding_errors or not raw_text.strip():
            err_reason = encoding_errors or "Input file is empty or contains only whitespace."
            self.store.record_audit_event(
                run_id=run_id,
                event_type="invalid_input",
                area_tag="reading",
                title="Unreadable or Empty Input File",
                detail=err_reason,
            )
            elapsed = time.monotonic() - t0
            run_row = {
                "run_id": run_id,
                "run_type": run_type,
                "filename": filename,
                "sha256": sha256,
                "elapsed_sec": elapsed,
                "status": "failed_invalid_input",
                "status_reason": err_reason,
                "planned_call_areas": ["reading"],
                "observed_call_areas": [],
                "recomputed_areas": [],
                "active_revision_after": prior_rev_id,
            }
            events = self.store.get_all_audit_events(run_id=run_id)
            trace_text = format_run_trace(run_row, [], events, prior_rev)
            self.store.finish_run(
                run_id=run_id,
                elapsed_sec=elapsed,
                status="failed_invalid_input",
                status_reason=err_reason,
                planned_call_areas=["reading"],
                observed_call_areas=[],
                recomputed_areas=[],
                active_revision_after=prior_rev_id,
                run_trace_text=trace_text,
                summary={"error": err_reason},
            )
            write_durable_exports(self.store, self.config.exports_dir)
            return {
                "run_id": run_id,
                "status": "failed_invalid_input",
                "trace_text": trace_text,
                "active_revision": prior_rev,
            }

        # Build compact prior summary for reading call if this is an update
        prior_summary: Optional[Dict[str, Any]] = None
        if prior_rev:
            p = prior_rev["plan"]
            prior_summary = {
                "active_revision": f"R{prior_rev['revision_id']}",
                "venue": p["venue"]["selected_venue"],
                "students": p["venue"]["occupancy_breakdown"]["students"],
                "staff": p["venue"]["occupancy_breakdown"]["staff"],
                "confirmed_judges": p["judging"]["confirmed_judges"],
                "food_boxes": p["food"]["headcount_ordered"],
                "food_tier": p["food"]["selected_tier"],
                "food_rate_pkr": p["food"]["rate_per_head_pkr"],
                "committed_costs": prior_rev["accepted_facts"]["budget"]["committed_costs"],
                "total_spend_pkr": p["budget"]["total_spend_pkr"],
                "budget_cap_pkr": p["budget"]["budget_cap_pkr"],
            }

        max_attempts = min(2, self.config.max_update_calls if has_base else self.config.max_baseline_calls)
        extracted_records, calls_made, read_err = execute_reading_with_bounded_retry(
            adapter=self.adapter,
            store=self.store,
            run_id=run_id,
            filename=filename,
            raw_text=raw_text,
            prior_summary=prior_summary,
            deadline_monotonic=deadline_monotonic,
            max_attempts=max_attempts,
        )
        observed_call_areas = [c["area_tag"] for c in calls_made]

        if extracted_records is None:
            self.store.record_audit_event(
                run_id=run_id,
                event_type="extraction_failure",
                area_tag="reading",
                title="Reading Extraction Failed — Prior Revision Retained",
                detail=f"Reading call failed ({read_err}). Active revision R{prior_rev_id} preserved intact.",
            )
            elapsed = time.monotonic() - t0
            run_row = {
                "run_id": run_id,
                "run_type": run_type,
                "filename": filename,
                "sha256": sha256,
                "elapsed_sec": elapsed,
                "status": "failed_extraction",
                "status_reason": read_err,
                "planned_call_areas": ["reading"],
                "observed_call_areas": observed_call_areas,
                "recomputed_areas": [],
                "active_revision_after": prior_rev_id,
            }
            events = self.store.get_all_audit_events(run_id=run_id)
            trace_text = format_run_trace(run_row, calls_made, events, prior_rev)
            self.store.finish_run(
                run_id=run_id,
                elapsed_sec=elapsed,
                status="failed_extraction",
                status_reason=read_err,
                planned_call_areas=["reading"],
                observed_call_areas=observed_call_areas,
                recomputed_areas=[],
                active_revision_after=prior_rev_id,
                run_trace_text=trace_text,
                summary={"error": read_err},
            )
            write_durable_exports(self.store, self.config.exports_dir)
            return {
                "run_id": run_id,
                "status": "failed_extraction",
                "trace_text": trace_text,
                "active_revision": prior_rev,
            }

        # Reconcile extracted statements against prior accepted facts
        prior_facts = prior_rev["accepted_facts"] if prior_rev else None
        rec_result = reconcile_state(
            prior_accepted_facts=prior_facts,
            new_records=extracted_records,
            is_baseline=not has_base,
        )

        candidate_facts = rec_result["candidate_facts"]
        rejection_reasons = rec_result["rejection_reasons"]
        superseded_pairs = rec_result["superseded_pairs"]
        changed_keys = rec_result["changed_keys"]
        recomputed_areas = rec_result["recomputed_areas"]
        planned_call_areas = rec_result["planned_call_areas"]

        # Persist messages, statements, and deterministic reconciliation events
        self.store.save_messages_and_statements(
            run_id=run_id,
            messages=[r.to_dict() for r in extracted_records],
        )
        for item in rec_result["audit_items"]:
            self.store.record_audit_event(
                run_id=run_id,
                event_type=item["event_type"],
                area_tag=item["area_tag"],
                title=item["title"],
                detail=item["detail"],
                message_id=item.get("message_id", ""),
                statement_id=item.get("statement_id", ""),
                metadata=item.get("metadata"),
            )

        # If an update was rejected at reconciliation (e.g. undersized meal order or authority scope violation)
        # and there are no accepted changes, retain the prior valid revision untouched!
        if has_base and rejection_reasons and not changed_keys:
            rej_summary = " | ".join(r["reason"] for r in rejection_reasons)
            self.store.record_audit_event(
                run_id=run_id,
                event_type="atomic_rollback",
                area_tag=rejection_reasons[0]["area"],
                title="Conflicting Update Rejected — Prior Valid Plan Retained",
                detail=f"Retained revision R{prior_rev_id} unchanged because incoming update conflicted with governing constraints: {rej_summary}",
                message_id=rejection_reasons[0]["message_id"],
                statement_id=rejection_reasons[0]["statement_id"],
            )
            elapsed = time.monotonic() - t0
            run_row = {
                "run_id": run_id,
                "run_type": run_type,
                "filename": filename,
                "sha256": sha256,
                "elapsed_sec": elapsed,
                "status": "rejected_conflict",
                "status_reason": rej_summary,
                "planned_call_areas": planned_call_areas,
                "observed_call_areas": observed_call_areas,
                "recomputed_areas": [],
                "active_revision_after": prior_rev_id,
            }
            events = self.store.get_all_audit_events(run_id=run_id)
            trace_text = format_run_trace(run_row, calls_made, events, prior_rev)
            self.store.finish_run(
                run_id=run_id,
                elapsed_sec=elapsed,
                status="rejected_conflict",
                status_reason=rej_summary,
                planned_call_areas=planned_call_areas,
                observed_call_areas=observed_call_areas,
                recomputed_areas=[],
                active_revision_after=prior_rev_id,
                run_trace_text=trace_text,
                summary={"rejection_reasons": rejection_reasons},
            )
            write_durable_exports(self.store, self.config.exports_dir)
            return {
                "run_id": run_id,
                "status": "rejected_conflict",
                "trace_text": trace_text,
                "active_revision": prior_rev,
            }

        # Solve and validate candidate facts
        candidate_plan, candidate_val = solve_and_validate(
            facts=candidate_facts,
            prior_plan=prior_rev["plan"] if prior_rev else None,
            recomputed_areas=recomputed_areas,
        )

        # Check monotonic deadline before committing
        if time.monotonic() > t0 + hard_limit:
            timeout_msg = f"Hard deadline ({hard_limit:.1f}s) exceeded before commit; retaining prior revision R{prior_rev_id}."
            self.store.record_audit_event(
                run_id=run_id,
                event_type="timeout_rollback",
                area_tag="reading",
                title="Deadline Exceeded — Commit Blocked",
                detail=timeout_msg,
            )
            elapsed = time.monotonic() - t0
            run_row = {
                "run_id": run_id,
                "run_type": run_type,
                "filename": filename,
                "sha256": sha256,
                "elapsed_sec": elapsed,
                "status": "failed_timeout",
                "status_reason": timeout_msg,
                "planned_call_areas": planned_call_areas,
                "observed_call_areas": observed_call_areas,
                "recomputed_areas": recomputed_areas,
                "active_revision_after": prior_rev_id,
            }
            events = self.store.get_all_audit_events(run_id=run_id)
            trace_text = format_run_trace(run_row, calls_made, events, prior_rev)
            self.store.finish_run(
                run_id=run_id,
                elapsed_sec=elapsed,
                status="failed_timeout",
                status_reason=timeout_msg,
                planned_call_areas=planned_call_areas,
                observed_call_areas=observed_call_areas,
                recomputed_areas=recomputed_areas,
                active_revision_after=prior_rev_id,
                run_trace_text=trace_text,
                summary={"error": timeout_msg},
            )
            write_durable_exports(self.store, self.config.exports_dir)
            return {
                "run_id": run_id,
                "status": "failed_timeout",
                "trace_text": trace_text,
                "active_revision": prior_rev,
            }

        # If candidate validation fails on an update, rollback to prior valid revision!
        if has_base and not candidate_val["all_passed"]:
            failed_checks = [
                f"{c['label']}: {c['detail']}"
                for c in candidate_val["checks"]
                if not c["passed"]
            ]
            if candidate_val.get("uncheckable_hard_rules"):
                failed_checks.extend(candidate_val["uncheckable_hard_rules"])
            fail_reason = (
                "Candidate plan failed independent validation ("
                + "; ".join(failed_checks)
                + f"). Retained prior valid revision R{prior_rev_id}."
            )
            # Mark candidate statements from this run as rejected in DB
            for rec in extracted_records:
                rec.disposition = "rejected"
                rec.disposition_reason = fail_reason
                for st in rec.statements:
                    if st.disposition == "accepted":
                        st.disposition = "rejected"
                        st.disposition_reason = fail_reason
            self.store.save_messages_and_statements(
                run_id=run_id,
                messages=[r.to_dict() for r in extracted_records],
            )
            self.store.record_audit_event(
                run_id=run_id,
                event_type="validation_rejection",
                area_tag=recomputed_areas[0] if recomputed_areas else "budget",
                title="Update Rejected by Independent Validator — Prior Valid Plan Retained",
                detail=fail_reason,
                message_id=extracted_records[0].message_id if extracted_records else "",
            )
            elapsed = time.monotonic() - t0
            run_row = {
                "run_id": run_id,
                "run_type": run_type,
                "filename": filename,
                "sha256": sha256,
                "elapsed_sec": elapsed,
                "status": "rejected_conflict",
                "status_reason": fail_reason,
                "planned_call_areas": planned_call_areas,
                "observed_call_areas": observed_call_areas,
                "recomputed_areas": recomputed_areas,
                "active_revision_after": prior_rev_id,
            }
            events = self.store.get_all_audit_events(run_id=run_id)
            trace_text = format_run_trace(run_row, calls_made, events, prior_rev)
            self.store.finish_run(
                run_id=run_id,
                elapsed_sec=elapsed,
                status="rejected_conflict",
                status_reason=fail_reason,
                planned_call_areas=planned_call_areas,
                observed_call_areas=observed_call_areas,
                recomputed_areas=recomputed_areas,
                active_revision_after=prior_rev_id,
                run_trace_text=trace_text,
                summary={"failed_checks": failed_checks},
            )
            write_durable_exports(self.store, self.config.exports_dir)
            return {
                "run_id": run_id,
                "status": "rejected_conflict",
                "trace_text": trace_text,
                "active_revision": prior_rev,
            }

        # Apply superseding updates in DB now that commit is confirmed
        for old_sid, new_sid, sup_reason in superseded_pairs:
            self.store.update_statement_superseded(old_sid, new_sid, sup_reason)

        # Compute revision delta and commit new revision
        is_provisional = not candidate_val["all_passed"]
        grade = "VALID" if candidate_val["all_passed"] else "PROVISIONAL"
        delta = compute_revision_delta(
            old_plan=prior_rev["plan"] if prior_rev else None,
            new_plan=candidate_plan,
            changed_keys=changed_keys,
            recomputed_areas=recomputed_areas,
            observed_call_areas=observed_call_areas,
            planned_call_areas=planned_call_areas,
        )

        new_rev_id = self.store.commit_revision(
            run_id=run_id,
            validation_grade=grade,
            is_provisional=is_provisional,
            accepted_facts=candidate_facts,
            plan=candidate_plan,
            validation=candidate_val,
            delta=delta,
        )
        new_rev = self.store.get_revision(new_rev_id)

        status_str = "provisional" if is_provisional else "accepted"
        status_reason = (
            f"Committed revision R{new_rev_id} ({grade})."
            if not is_provisional
            else f"Baseline facts infeasible; committed provisional best-effort draft R{new_rev_id}."
        )
        self.store.record_audit_event(
            run_id=run_id,
            event_type="commit",
            area_tag="reading",
            title=f"Committed Plan Revision R{new_rev_id} ({grade})",
            detail=status_reason,
        )

        elapsed = time.monotonic() - t0
        run_row = {
            "run_id": run_id,
            "run_type": run_type,
            "filename": filename,
            "sha256": sha256,
            "elapsed_sec": elapsed,
            "status": status_str,
            "status_reason": status_reason,
            "planned_call_areas": planned_call_areas,
            "observed_call_areas": observed_call_areas,
            "recomputed_areas": recomputed_areas,
            "active_revision_after": new_rev_id,
        }
        events = self.store.get_all_audit_events(run_id=run_id)
        trace_text = format_run_trace(run_row, calls_made, events, new_rev)
        self.store.finish_run(
            run_id=run_id,
            elapsed_sec=elapsed,
            status=status_str,
            status_reason=status_reason,
            planned_call_areas=planned_call_areas,
            observed_call_areas=observed_call_areas,
            recomputed_areas=recomputed_areas,
            active_revision_after=new_rev_id,
            run_trace_text=trace_text,
            summary={"revision_id": new_rev_id, "grade": grade},
        )
        write_durable_exports(self.store, self.config.exports_dir)
        return {
            "run_id": run_id,
            "status": status_str,
            "trace_text": trace_text,
            "active_revision": new_rev,
        }

"""Data models and schema validation for PlanLedger."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

ALLOWED_AREAS = {"reading", "venue", "schedule", "food", "judging", "budget"}
ALLOWED_STATEMENT_KINDS = {
    "requirement",
    "commitment",
    "quote",
    "preference",
    "informational",
    "possible_revision",
}
ALLOWED_HARDNESS = {"hard", "soft", "informational", "tentative"}
ALLOWED_DISPOSITIONS = {
    "accepted",
    "superseded",
    "irrelevant",
    "optional",
    "tentative",
    "rejected",
    "needs_clarification",
}
ALLOWED_RULE_TYPES = {
    "quantity",
    "range",
    "required_feature",
    "time_window",
    "order",
    "eligibility",
    "capacity",
    "dependency",
    "exclusion",
    "conditional_obligation",
    "opaque",
}
ALLOWED_OPERATORS = {
    "==",
    "!=",
    "<=",
    ">=",
    "<",
    ">",
    "in",
    "not_in",
    "requires",
    "excludes",
    "before",
    "after",
    "between",
    "opaque",
}


@dataclass
class DeclarativeRule:
    rule_type: str  # from ALLOWED_RULE_TYPES
    target_key: str
    operator: str  # from ALLOWED_OPERATORS
    operand: Any
    unit: str = ""
    condition: str = ""
    checkable: bool = True
    uncheckable_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeclarativeRule":
        if not isinstance(data, dict):
            return cls(
                rule_type="opaque",
                target_key="unknown",
                operator="opaque",
                operand=str(data),
                checkable=False,
                uncheckable_reason="Malformed declarative rule object",
            )
        rtype = str(data.get("rule_type", "opaque")).strip().lower()
        op = str(data.get("operator", "==")).strip().lower()
        checkable = bool(data.get("checkable", True))
        reason = str(data.get("uncheckable_reason", "")).strip()
        if rtype not in ALLOWED_RULE_TYPES:
            rtype = "opaque"
            checkable = False
            reason = reason or f"Unsupported rule_type: {data.get('rule_type')}"
        if op not in ALLOWED_OPERATORS:
            op = "opaque"
            checkable = False
            reason = reason or f"Unsupported operator: {data.get('operator')}"
        return cls(
            rule_type=rtype,
            target_key=str(data.get("target_key", "")).strip(),
            operator=op,
            operand=data.get("operand"),
            unit=str(data.get("unit", "")).strip(),
            condition=str(data.get("condition", "")).strip(),
            checkable=checkable,
            uncheckable_reason=reason,
        )


@dataclass
class CandidateStatement:
    statement_id: str
    origin_message_id: str
    raw_evidence_span: str
    quote_verified: bool
    claimed_sender: str
    claimed_role: str
    sent_time: str
    effective_time: str
    entity: str
    canonical_key: str
    area_candidates: List[str]
    statement_kind: str
    scope: str
    units: str
    value: Any
    secondary_values: Dict[str, Any] = field(default_factory=dict)
    conditions: str = ""
    hardness: str = "hard"
    replacement_target: str = ""
    ambiguity: str = ""
    restatement: str = ""
    declarative_rule: Optional[DeclarativeRule] = None
    disposition: str = "needs_clarification"
    disposition_reason: str = ""
    superseded_by: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.declarative_rule:
            d["declarative_rule"] = self.declarative_rule.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CandidateStatement":
        rule_raw = data.get("declarative_rule")
        rule = DeclarativeRule.from_dict(rule_raw) if isinstance(rule_raw, dict) else None
        areas = [
            str(a).strip().lower()
            for a in (data.get("area_candidates") or [])
            if str(a).strip().lower() in ALLOWED_AREAS
        ]
        if not areas:
            areas = ["reading"]
        kind = str(data.get("statement_kind", "informational")).strip().lower()
        if kind not in ALLOWED_STATEMENT_KINDS:
            kind = "informational"
        hardness = str(data.get("hardness", "hard")).strip().lower()
        if hardness not in ALLOWED_HARDNESS:
            hardness = "hard"
        sec = data.get("secondary_values")
        if not isinstance(sec, dict):
            sec = {}
        return cls(
            statement_id=str(data.get("statement_id", "")).strip(),
            origin_message_id=str(data.get("origin_message_id", "")).strip(),
            raw_evidence_span=str(data.get("raw_evidence_span", "")).strip(),
            quote_verified=bool(data.get("quote_verified", False)),
            claimed_sender=str(data.get("claimed_sender", "")).strip(),
            claimed_role=str(data.get("claimed_role", "")).strip(),
            sent_time=str(data.get("sent_time", "")).strip(),
            effective_time=str(data.get("effective_time", "")).strip(),
            entity=str(data.get("entity", "")).strip(),
            canonical_key=str(data.get("canonical_key", "")).strip(),
            area_candidates=areas,
            statement_kind=kind,
            scope=str(data.get("scope", "")).strip(),
            units=str(data.get("units", "")).strip(),
            value=data.get("value"),
            secondary_values=sec,
            conditions=str(data.get("conditions", "")).strip(),
            hardness=hardness,
            replacement_target=str(data.get("replacement_target", "")).strip(),
            ambiguity=str(data.get("ambiguity", "")).strip(),
            restatement=str(data.get("restatement", "")).strip(),
            declarative_rule=rule,
            disposition=str(data.get("disposition", "needs_clarification")).strip(),
            disposition_reason=str(data.get("disposition_reason", "")).strip(),
            superseded_by=str(data.get("superseded_by", "")).strip(),
        )


@dataclass
class ExtractedMessageRecord:
    message_id: str
    run_id: int
    raw_text: str
    line_start: int
    line_end: int
    claimed_sender: str
    claimed_role: str
    sent_date: str
    channel: str
    language: str
    summary: str
    disposition: str = "accepted"
    disposition_reason: str = ""
    statements: List[CandidateStatement] = field(default_factory=list)
    unstructured_clauses: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "run_id": self.run_id,
            "raw_text": self.raw_text,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "claimed_sender": self.claimed_sender,
            "claimed_role": self.claimed_role,
            "sent_date": self.sent_date,
            "channel": self.channel,
            "language": self.language,
            "summary": self.summary,
            "disposition": self.disposition,
            "disposition_reason": self.disposition_reason,
            "statements": [s.to_dict() for s in self.statements],
            "unstructured_clauses": list(self.unstructured_clauses),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExtractedMessageRecord":
        stmts = [
            CandidateStatement.from_dict(s)
            for s in (data.get("statements") or [])
            if isinstance(s, dict)
        ]
        clauses = [str(c) for c in (data.get("unstructured_clauses") or []) if c]
        return cls(
            message_id=str(data.get("message_id", "")).strip(),
            run_id=int(data.get("run_id", 1)),
            raw_text=str(data.get("raw_text", "")),
            line_start=int(data.get("line_start", 1)),
            line_end=int(data.get("line_end", 1)),
            claimed_sender=str(data.get("claimed_sender", "")).strip(),
            claimed_role=str(data.get("claimed_role", "")).strip(),
            sent_date=str(data.get("sent_date", "")).strip(),
            channel=str(data.get("channel", "")).strip(),
            language=str(data.get("language", "en")).strip(),
            summary=str(data.get("summary", "")).strip(),
            disposition=str(data.get("disposition", "accepted")).strip(),
            disposition_reason=str(data.get("disposition_reason", "")).strip(),
            statements=stmts,
            unstructured_clauses=clauses,
        )


@dataclass
class ModelCallTrace:
    call_number: int
    run_id: int
    area_tag: str  # reading, venue, schedule, food, judging, budget
    requested_model: str
    reported_model: Optional[str]
    provider: str
    attempt_number: int
    attempt_relation: str  # primary, retry_1, fallback, repair
    input_payload: str
    output_content: str
    outcome: str  # ok, malformed_json, timeout, provider_error
    elapsed_sec: float
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    subcounts: Dict[str, Any] = field(default_factory=dict)
    usage_complete: bool = True
    usage_note: str = ""
    error_detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

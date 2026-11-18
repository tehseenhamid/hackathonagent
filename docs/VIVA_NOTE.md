# PlanLedger: one-page viva note

**What it is.** One command, `python -m planledger <file>`, reads messages and keeps a valid event plan. Model for language, code for arithmetic.

**Flow.** file bytes stored (sha256) -> one reading call (JSON, exact quotes) -> quote verification -> reconcile (authority, date, tentative, optional, irrelevant, superseded) -> planner enumerates venue x menu x judge count -> validator re-checks each candidate -> commit atomically as a new revision, or keep the last one.

**Why only one model call per update.** Venue, schedule, food, judging and budget are deterministic once facts are known. An accepted change recomputes only the areas that depend on it (dependency map). Fewer calls means fewer tokens and a faster update.

**The wrong message.** Plausible-but-wrong messages are caught by facts, not by guessing: the candidate plan fails a named check, then a culprit search finds the smallest set of statements to hold back. If the culprit has governing authority the plan waits for clarification; otherwise the statement is rejected. The trace prints the rule and the retained revision.

**What breaks without each part.** No quote check: invented facts. No reconcile: stale or unauthorised messages win. No validator: invalid plans commit. No atomic store: a failed update corrupts the plan. No dependency map: every update costs five calls.

**Things I decided.** Only deployed judges are paid and fed (reserves are not). Menu: first-quoted item unless it breaks a rule, saving shown. Start time unknown means offsets, not made-up clock times. Unapproved extras (requests nobody with authority approved) are not budgeted.

**Limits I can state honestly.** No real-model run was verified in the build sandbox. Token numbers are provider-reported only. Rules the checker cannot verify are listed as unverified, not passed.

**Likely questions.** "Why not an agent per area?" Agents add cost; code is exact. "What if the model returns bad JSON?" One counted repair, then a clean failure with the last plan kept. "What if two messages conflict?" Authority first, then date; conditional and tentative statements are never applied.

# PlanLedger

Local, evidence-first planner for the Mind to Machine hackathon (GDGoC CUI Wah). It reads a pack of messages from a text file, plans a 4-hour physical event (venue, schedule, food, judging, budget), and keeps that plan valid as updates arrive. Standard library only, Python 3.11+.

## The one command

```
python -m planledger incoming.txt
```

The same command takes the first pack and every later update. No flags, hints or edits. It prints the full trace (numbered model calls, model name, one area tag, input, output, provider-reported tokens, total tokens), the plan, and any rejected message with the rule it would have broken.

## Setup (2 minutes, before the test)

1. Copy `.env.example` to `.env` (untracked) and fill in provider, base URL, key and model. Any OpenAI-compatible endpoint works (including local servers); `anthropic` is also supported.
2. `python -m planledger.probe` sends one tiny fixed prompt (no pack data) and prints latency, the model id and token fields the provider reports. Run it before the judged session, never during it.
3. `python -m planledger.web` starts the read-only dashboard at http://127.0.0.1:8765/ . It only reads the database, never calls a model, and refreshes itself when a run commits.

An editor or chat subscription does not give the app API access: the app needs its own provider settings. If none are set, `python -m planledger` says so and fails honestly instead of faking a result.

## How it works (and why each part exists)

| Part | Job | Without it |
|---|---|---|
| Reader (one model call) | Turns messages into candidate statements with an exact quote each. Closed vocabulary, strict JSON, quotes verified against the original text. | Free text could invent facts. |
| Reconcile (code) | Decides who has authority, what is newer, what is tentative, optional, irrelevant or superseded. | An old or unauthorised message could win. |
| Planner (code) | Enumerates venue, menu and judge-count choices, builds schedule and judging, does all arithmetic. | Models make arithmetic mistakes and cost tokens. |
| Validator (code) | Independently re-checks the six scored checks plus consistency on every candidate plan. | A wrong plan could be committed. |
| Culprit search (code) | If an update makes the plan invalid, finds the smallest set of statements to hold back and names the rule. | The wrong message would break the plan. |
| Atomic store (SQLite) | A rejected or failed run never changes the last valid revision. | Half-applied updates. |
| Dependency map | After an update only affected areas are recomputed; by default the only model call is `reading`. | Needless calls and tokens. |
| Dashboard and Ask the Plan | Plan, changes and rejected messages, schedule and judging, budget, trace, evidence. Ask the Plan is deterministic (no model) and says which source each answer came from. | Hard to inspect. |

Message text is data only. It is wrapped as a pack in the prompt, never obeyed as instructions, and rendered in the browser with `textContent` only.

## Limits built in

Full run cut off before 2 minutes; update capped at 5 model calls and under 1 minute (defaults are lower, see `planledger/config.py`). There are no hidden retries: one counted repair attempt when the reading output fails strict validation. Token counts are exactly what the provider reports; if a provider reports none the trace says "unavailable" and the total is marked partial. Nothing is estimated.

## Outputs

`.planledger-data/` (git-ignored) holds the database and `exports/`: `final_plan_and_trace.txt` (upload this on the form), `plan_latest.txt`, `trace_run<N>.txt`, `session.json`. `python -m planledger.export` rebuilds them from the database. `python -m planledger.routing` prints the update routing guide used for the written prediction.

## Tests

```
python3 -m unittest discover -s tests -t .
```

Offline. The tests use a scripted fake model (`tests/support.py`, test-only, clearly labelled, invented usage numbers) and an independent synthetic pack. **No real-model run has been verified yet**: real latency, JSON compliance and token counts must be measured with `probe` and one dry run on your own provider before the code freeze.

## Rules this repo follows

No pack facts, sample values or messages in code or prompts. No fake metrics. No slides. The repo is never pushed by the tool: you push, and you submit the commit hash. Code freeze is 1:25 PM.

## Questions for the help desk (do not guess)

1. Are all confirmed judges fed and paid, or only the ones deployed? (Default here: only deployed.)
2. May a cheaper menu item be chosen when two are quoted? (Default: keep the first-quoted/preferred item and list the saving.)
3. Which models are on the small-model list for the 10-point bonus?

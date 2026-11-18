"""Harmless provider check, run BEFORE the judged session (never during it).

    python -m planledger.probe

Sends one tiny fixed prompt with no pack data, prints what the provider reports (model id,
latency, usage fields) and whether the reading prompt's JSON mode works. It writes nothing
to the PlanLedger database, so it cannot affect the call count or the trace of a judged run.
Its own token use is real and is printed; it is not part of any judged total.
"""
from __future__ import annotations

import json
import sys
import time

from .config import Config
from .providers import ProviderError, make_adapter


def main(argv=None) -> int:
    cfg = Config.load()
    problem = cfg.provider_problem()
    if problem:
        print("Provider is not ready: " + problem)
        print("Set PLANLEDGER_PROVIDER, PLANLEDGER_BASE_URL (OpenAI-compatible only), PLANLEDGER_API_KEY and PLANLEDGER_MODEL in the environment or an untracked .env file.")
        return 2
    print(f"provider={cfg.provider}  model(configured)={cfg.model}  base_url={cfg.base_url or '(default)'}")
    ad = make_adapter(cfg)
    system = "Reply with one JSON object only."
    user = 'Return {"ok": true, "n": 3}.'
    for mode in (True, False):
        t0 = time.monotonic()
        try:
            r = ad.complete(system, user, 30.0, 60, mode)
        except ProviderError as e:
            print(f"json_mode={mode}: FAILED ({e.kind}): {e.detail}")
            continue
        dt = time.monotonic() - t0
        ok = True
        try:
            json.loads(r.text)
        except Exception:
            ok = False
        print(f"json_mode={mode}: {dt:.2f}s  provider_model={r.resolved_model or 'not reported'}  usage={r.usage}  valid_json={ok}")
        print("  output:", (r.text or "")[:120].replace("\n", " "))
    print("\nIf usage is None the provider reports no token counts: the trace will then say 'unavailable' and the session total will be marked partial.")
    print("If valid_json is False with json_mode=True, set PLANLEDGER_JSON_MODE=0 (see README) and re-probe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

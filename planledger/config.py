"""Runtime configuration. Everything comes from environment variables or an
optional untracked `.env` file in the working directory. No secrets are ever
printed or stored in the trace."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

HARD_BASELINE_S = 120.0     # organizer limit for a full run
HARD_UPDATE_S = 60.0        # organizer limit for an update
HARD_UPDATE_CALLS = 5       # organizer limit on model calls per update


def load_env_file(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


@dataclass
class Config:
    data_dir: Path
    provider: str            # "openai" (any OpenAI-compatible endpoint) | "anthropic" | "" (unconfigured)
    base_url: str
    api_key: str
    model: str
    baseline_budget_s: float
    update_budget_s: float
    reserve_s: float
    per_call_cap_s: float
    max_update_calls: int
    max_baseline_calls: int
    max_read_attempts: int
    max_output_tokens: int
    json_mode: bool
    token_param: str         # max_tokens | max_completion_tokens
    judge_policy: str        # minimum | all_confirmed
    lunch_min: int
    min_build_min: int
    solver_timeout_s: float
    allow_area_calls: bool

    @classmethod
    def load(cls, env: dict | None = None, cwd: Path | None = None) -> "Config":
        merged: dict = {}
        cwd = cwd or Path.cwd()
        merged.update(load_env_file(cwd / ".env"))
        merged.update(env if env is not None else os.environ)
        g = lambda k, d="": str(merged.get(k, d))  # noqa: E731

        def f(k, d):
            try:
                return float(g(k, d))
            except ValueError:
                return float(d)

        def i(k, d):
            try:
                return int(g(k, d))
            except ValueError:
                return int(d)

        provider = g("PLANLEDGER_PROVIDER").strip().lower()
        if provider in ("openai_compat", "openai-compatible", "ollama", "groq", "gemini", "openrouter"):
            provider = "openai"
        base = g("PLANLEDGER_BASE_URL").strip().rstrip("/")
        if provider == "anthropic" and not base:
            base = "https://api.anthropic.com"
        cfg = cls(
            data_dir=Path(g("PLANLEDGER_DATA_DIR", ".planledger-data")).expanduser(),
            provider=provider,
            base_url=base,
            api_key=g("PLANLEDGER_API_KEY").strip(),
            model=g("PLANLEDGER_MODEL").strip(),
            baseline_budget_s=min(f("PLANLEDGER_BASELINE_BUDGET_S", 100), HARD_BASELINE_S - 5),
            update_budget_s=min(f("PLANLEDGER_UPDATE_BUDGET_S", 50), HARD_UPDATE_S - 5),
            reserve_s=f("PLANLEDGER_RESERVE_S", 4),
            per_call_cap_s=f("PLANLEDGER_PER_CALL_CAP_S", 90),
            max_update_calls=min(i("PLANLEDGER_MAX_UPDATE_CALLS", 3), HARD_UPDATE_CALLS),
            max_baseline_calls=i("PLANLEDGER_MAX_BASELINE_CALLS", 4),
            max_read_attempts=max(1, i("PLANLEDGER_MAX_READ_ATTEMPTS", 3)),
            max_output_tokens=i("PLANLEDGER_MAX_OUTPUT_TOKENS", 3500),
            json_mode=g("PLANLEDGER_JSON_MODE", "1") not in ("0", "false", "no"),
            token_param=g("PLANLEDGER_TOKEN_PARAM", "max_tokens"),
            judge_policy=g("PLANLEDGER_JUDGE_POLICY", "minimum"),
            lunch_min=i("PLANLEDGER_LUNCH_MIN", 30),
            min_build_min=i("PLANLEDGER_MIN_BUILD_MIN", 60),
            solver_timeout_s=f("PLANLEDGER_SOLVER_TIMEOUT_S", 5),
            allow_area_calls=g("PLANLEDGER_AREA_CALLS", "1") not in ("0", "false", "no"),
        )
        return cfg

    def provider_problem(self) -> str | None:
        """Human-readable reason the provider is unusable, or None if it looks configured."""
        if not self.provider:
            return "PLANLEDGER_PROVIDER is not set (use 'openai' for any OpenAI-compatible endpoint, or 'anthropic')."
        if self.provider not in ("openai", "anthropic"):
            return f"PLANLEDGER_PROVIDER='{self.provider}' is not supported (use 'openai' or 'anthropic')."
        if not self.model:
            return "PLANLEDGER_MODEL is not set."
        if self.provider == "openai" and not self.base_url:
            return "PLANLEDGER_BASE_URL is not set (for example http://localhost:11434/v1 for a local Ollama model)."
        if self.provider == "anthropic" and not self.api_key:
            return "PLANLEDGER_API_KEY is not set."
        return None

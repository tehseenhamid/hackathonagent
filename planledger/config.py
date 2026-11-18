"""Configuration and environment management for PlanLedger."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def load_dotenv(env_path: Optional[Path] = None) -> None:
    """Load simple KEY=VALUE pairs from a local .env file without external dependencies."""
    path = env_path or Path.cwd() / ".env"
    if not path.is_file():
        return
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


@dataclass(frozen=True)
class AppConfig:
    provider: str
    model: str
    ollama_base_url: str
    gemini_api_key: str
    openai_api_key: str
    openai_base_url: str
    data_dir: Path
    host: str
    port: int
    baseline_hard_limit_sec: float = 120.0
    update_hard_limit_sec: float = 60.0
    baseline_budget_sec: float = 110.0
    update_budget_sec: float = 52.0
    max_update_calls: int = 5
    max_baseline_calls: int = 8

    @classmethod
    def from_env(cls, env_path: Optional[Path] = None) -> "AppConfig":
        load_dotenv(env_path)
        provider = os.environ.get("PLANLEDGER_PROVIDER", "ollama").strip().lower()
        default_model = "qwen2.5:7b"
        if provider == "gemini":
            default_model = "gemini-2.5-flash"
        elif provider == "openai_compat":
            default_model = "gpt-4o-mini"
        model = os.environ.get("PLANLEDGER_MODEL", default_model).strip() or default_model
        ollama_base_url = (
            os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip().rstrip("/")
        )
        gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        openai_base_url = (
            os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        )
        data_dir_str = os.environ.get("PLANLEDGER_DATA_DIR", "runtime/default").strip()
        data_dir = Path(data_dir_str).resolve()
        host = os.environ.get("PLANLEDGER_HOST", "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(os.environ.get("PLANLEDGER_PORT", "8765").strip())
        except ValueError:
            port = 8765
        try:
            baseline_budget = float(
                os.environ.get("PLANLEDGER_BASELINE_BUDGET_SEC", "110").strip()
            )
        except ValueError:
            baseline_budget = 110.0
        try:
            update_budget = float(
                os.environ.get("PLANLEDGER_UPDATE_BUDGET_SEC", "52").strip()
            )
        except ValueError:
            update_budget = 52.0

        return cls(
            provider=provider,
            model=model,
            ollama_base_url=ollama_base_url,
            gemini_api_key=gemini_api_key,
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url,
            data_dir=data_dir,
            host=host,
            port=port,
            baseline_budget_sec=min(118.0, max(5.0, baseline_budget)),
            update_budget_sec=min(58.0, max(5.0, update_budget)),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "planledger.db"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    def provider_status(self) -> dict:
        if self.provider == "ollama":
            return {
                "configured": bool(self.model and self.ollama_base_url),
                "provider": "ollama",
                "requested_model": self.model,
                "endpoint": self.ollama_base_url,
                "reason": "Ready (local Ollama endpoint)"
                if self.model
                else "Missing PLANLEDGER_MODEL",
            }
        if self.provider == "gemini":
            ok = bool(self.gemini_api_key and self.model)
            return {
                "configured": ok,
                "provider": "gemini",
                "requested_model": self.model,
                "endpoint": "https://generativelanguage.googleapis.com",
                "reason": "Ready (Gemini API key configured)"
                if ok
                else "Missing GEMINI_API_KEY in environment",
            }
        if self.provider == "openai_compat":
            ok = bool(self.openai_api_key and self.model)
            return {
                "configured": ok,
                "provider": "openai_compat",
                "requested_model": self.model,
                "endpoint": self.openai_base_url,
                "reason": "Ready (OpenAI-compatible key configured)"
                if ok
                else "Missing OPENAI_API_KEY in environment",
            }
        if self.provider == "unconfigured":
            return {
                "configured": False,
                "provider": "unconfigured",
                "requested_model": self.model,
                "endpoint": "none",
                "reason": "Provider explicitly set to unconfigured",
            }
        return {
            "configured": False,
            "provider": self.provider,
            "requested_model": self.model,
            "endpoint": "unknown",
            "reason": f"Unsupported provider '{self.provider}'",
        }

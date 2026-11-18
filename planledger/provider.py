"""Provider adapter for PlanLedger with monotonic deadline enforcement and exact usage reporting."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .config import AppConfig
from .models import ALLOWED_AREAS


@dataclass
class ProviderResponse:
    requested_model: str
    reported_model: Optional[str]
    provider: str
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


class ProviderAdapter:
    """Thin adapter supporting ollama, gemini, openai_compat, and injectable test callable."""

    def __init__(
        self,
        config: AppConfig,
        custom_transport: Optional[
            Callable[[str, str, str, float, AppConfig], ProviderResponse]
        ] = None,
    ) -> None:
        self.config = config
        self.custom_transport = custom_transport

    def call_json(
        self,
        area_tag: str,
        system_prompt: str,
        user_payload: str,
        deadline_monotonic: float,
    ) -> ProviderResponse:
        if area_tag not in ALLOWED_AREAS:
            raise ValueError(f"Invalid area_tag '{area_tag}'. Must be one of {sorted(ALLOWED_AREAS)}")

        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0.25:
            return ProviderResponse(
                requested_model=self.config.model,
                reported_model=None,
                provider=self.config.provider,
                output_content="",
                outcome="timeout",
                elapsed_sec=0.0,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                usage_complete=False,
                usage_note="Call blocked before dispatch: monotonic run deadline exhausted.",
                error_detail="Run deadline exceeded before request dispatch.",
            )

        req_timeout = max(0.5, remaining - 0.15)
        if self.custom_transport is not None:
            return self.custom_transport(
                area_tag, system_prompt, user_payload, req_timeout, self.config
            )

        status = self.config.provider_status()
        if not status["configured"]:
            return ProviderResponse(
                requested_model=self.config.model,
                reported_model=None,
                provider=self.config.provider,
                output_content="",
                outcome="provider_error",
                elapsed_sec=0.0,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                usage_complete=False,
                usage_note=f"Provider not configured: {status['reason']}",
                error_detail=status["reason"],
            )

        if self.config.provider == "ollama":
            return self._call_ollama(system_prompt, user_payload, req_timeout)
        if self.config.provider == "gemini":
            return self._call_gemini(system_prompt, user_payload, req_timeout)
        if self.config.provider == "openai_compat":
            return self._call_openai_compat(system_prompt, user_payload, req_timeout)

        return ProviderResponse(
            requested_model=self.config.model,
            reported_model=None,
            provider=self.config.provider,
            output_content="",
            outcome="provider_error",
            elapsed_sec=0.0,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            usage_complete=False,
            usage_note=f"Unsupported provider '{self.config.provider}'",
            error_detail=f"Unsupported provider '{self.config.provider}'",
        )

    def _call_ollama(
        self, system_prompt: str, user_payload: str, timeout_sec: float
    ) -> ProviderResponse:
        url = f"{self.config.ollama_base_url}/api/chat"
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0, "num_predict": 4096},
        }
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw_resp = resp.read().decode("utf-8", errors="replace")
            elapsed = time.monotonic() - t0
            data = json.loads(raw_resp)
        except (TimeoutError, socket.timeout) as exc:
            elapsed = time.monotonic() - t0
            return ProviderResponse(
                requested_model=self.config.model,
                reported_model=None,
                provider="ollama",
                output_content="",
                outcome="timeout",
                elapsed_sec=elapsed,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                usage_complete=False,
                usage_note="Unavailable: request timed out before provider returned usage metadata.",
                error_detail=f"Timeout after {elapsed:.2f}s: {exc}",
            )
        except urllib.error.URLError as exc:
            elapsed = time.monotonic() - t0
            if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
                return ProviderResponse(
                    requested_model=self.config.model,
                    reported_model=None,
                    provider="ollama",
                    output_content="",
                    outcome="timeout",
                    elapsed_sec=elapsed,
                    input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    usage_complete=False,
                    usage_note="Unavailable: request timed out before provider returned usage metadata.",
                    error_detail=f"Timeout after {elapsed:.2f}s: {exc}",
                )
            return ProviderResponse(
                requested_model=self.config.model,
                reported_model=None,
                provider="ollama",
                output_content="",
                outcome="provider_error",
                elapsed_sec=elapsed,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                usage_complete=False,
                usage_note="Unavailable: provider connection or HTTP error prevented usage report.",
                error_detail=f"Ollama connection error: {exc}",
            )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            return ProviderResponse(
                requested_model=self.config.model,
                reported_model=None,
                provider="ollama",
                output_content="",
                outcome="provider_error",
                elapsed_sec=elapsed,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                usage_complete=False,
                usage_note="Unavailable: unexpected error reading provider response.",
                error_detail=str(exc),
            )

        reported_model = data.get("model") if isinstance(data.get("model"), str) else None
        msg = data.get("message") or {}
        content = msg.get("content", "") if isinstance(msg, dict) else ""

        in_tok = data.get("prompt_eval_count")
        out_tok = data.get("eval_count")
        in_tok = int(in_tok) if isinstance(in_tok, int) else None
        out_tok = int(out_tok) if isinstance(out_tok, int) else None

        if in_tok is not None and out_tok is not None:
            tot_tok: Optional[int] = in_tok + out_tok
            usage_complete = True
            usage_note = "Reported by Ollama (prompt_eval_count + eval_count)."
        else:
            tot_tok = None
            usage_complete = False
            missing = []
            if in_tok is None:
                missing.append("prompt_eval_count")
            if out_tok is None:
                missing.append("eval_count")
            usage_note = f"Partial/Unavailable: Ollama omitted {', '.join(missing)}; no values estimated."

        subcounts: Dict[str, Any] = {}
        for k in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
            if k in data and isinstance(data[k], int):
                subcounts[k] = data[k]

        return ProviderResponse(
            requested_model=self.config.model,
            reported_model=reported_model,
            provider="ollama",
            output_content=content,
            outcome="ok",
            elapsed_sec=elapsed,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            subcounts=subcounts,
            usage_complete=usage_complete,
            usage_note=usage_note,
        )

    def _call_gemini(
        self, system_prompt: str, user_payload: str, timeout_sec: float
    ) -> ProviderResponse:
        model_id = self.config.model
        if model_id.startswith("models/"):
            model_path = model_id
        else:
            model_path = f"models/{model_id}"
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent"
            f"?key={self.config.gemini_api_key}"
        )
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_payload}]}],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json",
            },
        }
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw_resp = resp.read().decode("utf-8", errors="replace")
            elapsed = time.monotonic() - t0
            data = json.loads(raw_resp)
        except (TimeoutError, socket.timeout) as exc:
            elapsed = time.monotonic() - t0
            return ProviderResponse(
                requested_model=self.config.model,
                reported_model=None,
                provider="gemini",
                output_content="",
                outcome="timeout",
                elapsed_sec=elapsed,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                usage_complete=False,
                usage_note="Unavailable: Gemini request timed out before response.",
                error_detail=f"Timeout after {elapsed:.2f}s: {exc}",
            )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            return ProviderResponse(
                requested_model=self.config.model,
                reported_model=None,
                provider="gemini",
                output_content="",
                outcome="provider_error",
                elapsed_sec=elapsed,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                usage_complete=False,
                usage_note="Unavailable: Gemini request failed before usage metadata returned.",
                error_detail=f"Gemini request error (key redacted): {type(exc).__name__}",
            )

        reported_model = (
            data.get("modelVersion")
            if isinstance(data.get("modelVersion"), str)
            else None
        )
        candidates = data.get("candidates") or []
        content = ""
        if candidates and isinstance(candidates[0], dict):
            parts = (candidates[0].get("content") or {}).get("parts") or []
            content = "".join(
                p.get("text", "") for p in parts if isinstance(p, dict)
            )

        usage = data.get("usageMetadata") or {}
        in_tok = usage.get("promptTokenCount")
        out_tok = usage.get("candidatesTokenCount")
        tot_tok = usage.get("totalTokenCount")
        in_tok = int(in_tok) if isinstance(in_tok, int) else None
        out_tok = int(out_tok) if isinstance(out_tok, int) else None
        tot_tok = int(tot_tok) if isinstance(tot_tok, int) else None

        subcounts: Dict[str, Any] = {}
        for k in ("cachedContentTokenCount", "thoughtsTokenCount"):
            if k in usage and isinstance(usage[k], int):
                subcounts[k] = usage[k]

        if in_tok is not None and out_tok is not None and tot_tok is not None:
            usage_complete = True
            usage_note = "Reported by Gemini usageMetadata (totalTokenCount already includes subcounts)."
        else:
            usage_complete = False
            usage_note = "Partial/Unavailable: Gemini omitted one or more token fields."

        return ProviderResponse(
            requested_model=self.config.model,
            reported_model=reported_model,
            provider="gemini",
            output_content=content,
            outcome="ok",
            elapsed_sec=elapsed,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            subcounts=subcounts,
            usage_complete=usage_complete,
            usage_note=usage_note,
        )

    def _call_openai_compat(
        self, system_prompt: str, user_payload: str, timeout_sec: float
    ) -> ProviderResponse:
        url = f"{self.config.openai_base_url}/chat/completions"
        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.config.openai_api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw_resp = resp.read().decode("utf-8", errors="replace")
            elapsed = time.monotonic() - t0
            data = json.loads(raw_resp)
        except (TimeoutError, socket.timeout) as exc:
            elapsed = time.monotonic() - t0
            return ProviderResponse(
                requested_model=self.config.model,
                reported_model=None,
                provider="openai_compat",
                output_content="",
                outcome="timeout",
                elapsed_sec=elapsed,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                usage_complete=False,
                usage_note="Unavailable: OpenAI-compatible request timed out.",
                error_detail=f"Timeout after {elapsed:.2f}s: {exc}",
            )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            return ProviderResponse(
                requested_model=self.config.model,
                reported_model=None,
                provider="openai_compat",
                output_content="",
                outcome="provider_error",
                elapsed_sec=elapsed,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                usage_complete=False,
                usage_note="Unavailable: OpenAI-compatible request failed.",
                error_detail=f"OpenAI-compat error (key redacted): {type(exc).__name__}",
            )

        reported_model = data.get("model") if isinstance(data.get("model"), str) else None
        choices = data.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            content = msg.get("content", "") if isinstance(msg, dict) else ""

        usage = data.get("usage") or {}
        in_tok = usage.get("prompt_tokens")
        out_tok = usage.get("completion_tokens")
        tot_tok = usage.get("total_tokens")
        in_tok = int(in_tok) if isinstance(in_tok, int) else None
        out_tok = int(out_tok) if isinstance(out_tok, int) else None
        tot_tok = int(tot_tok) if isinstance(tot_tok, int) else None

        subcounts: Dict[str, Any] = {}
        if isinstance(usage.get("prompt_tokens_details"), dict):
            subcounts["prompt_tokens_details"] = usage["prompt_tokens_details"]
        if isinstance(usage.get("completion_tokens_details"), dict):
            subcounts["completion_tokens_details"] = usage["completion_tokens_details"]

        usage_complete = (
            in_tok is not None and out_tok is not None and tot_tok is not None
        )
        usage_note = (
            "Reported by provider usage object."
            if usage_complete
            else "Partial/Unavailable: provider omitted token counts."
        )

        return ProviderResponse(
            requested_model=self.config.model,
            reported_model=reported_model,
            provider="openai_compat",
            output_content=content,
            outcome="ok",
            elapsed_sec=elapsed,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            subcounts=subcounts,
            usage_complete=usage_complete,
            usage_note=usage_note,
        )

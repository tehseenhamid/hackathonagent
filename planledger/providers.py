"""Thin model adapters. One request = one attempt: no hidden client retries, the
caller owns retry policy and counts every attempt. Secrets never leave this
module (error text is redacted). Usage fields are returned exactly as the
provider reports them; anything not reported stays None ("unavailable")."""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.parse
from dataclasses import dataclass, field


class ProviderError(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind          # timeout | http | network | malformed | config
        self.detail = detail


@dataclass
class ModelResult:
    text: str
    resolved_model: str | None
    usage: dict | None            # {"input":int|None,"output":int|None,"total":int|None,"sub":{...}}
    usage_note: str | None = None
    finish: str | None = None
    raw_usage: dict | None = None


def _redact(s: str, secret: str) -> str:
    if secret:
        s = s.replace(secret, "[redacted]")
    return s[:400]


def _post(url: str, headers: dict, body: dict, timeout: float, secret: str):
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ProviderError("config", "base URL must start with http:// or https://")
    try:
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        path = u.path + (("?" + u.query) if u.query else "")
        payload = json.dumps(body).encode("utf-8")
        conn.request("POST", path, body=payload, headers={**headers, "Content-Type": "application/json",
                                                          "Content-Length": str(len(payload))})
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        conn.close()
    except (socket.timeout, TimeoutError):
        raise ProviderError("timeout", f"no complete response within {timeout:.1f}s")
    except (OSError, http.client.HTTPException) as e:
        raise ProviderError("network", _redact(f"{type(e).__name__}: {e}", secret))
    if status >= 400:
        raise ProviderError("http", _redact(f"HTTP {status}: {data.decode('utf-8', 'replace')}", secret))
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        raise ProviderError("malformed", "provider returned a non-JSON body")


class OpenAICompatAdapter:
    """Any /chat/completions endpoint: OpenAI, Groq, OpenRouter, Gemini (OpenAI mode), Ollama, LM Studio, vLLM."""
    name = "openai"

    def __init__(self, cfg):
        self.cfg = cfg

    def complete(self, system: str, user: str, timeout_s: float, max_tokens: int, json_mode: bool) -> ModelResult:
        c = self.cfg
        body = {
            "model": c.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "stream": False,
            c.token_param: max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Accept": "application/json"}
        if c.api_key:
            headers["Authorization"] = "Bearer " + c.api_key
        j = _post(c.base_url + "/chat/completions", headers, body, timeout_s, c.api_key)
        try:
            ch = j["choices"][0]
            text = ch["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError("malformed", "response has no choices[0].message.content")
        u = j.get("usage")
        usage, note, raw = None, None, None
        if isinstance(u, dict):
            raw = u
            pin, pout, ptot = u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens")
            sub = {}
            d = u.get("completion_tokens_details") or {}
            if isinstance(d, dict) and d.get("reasoning_tokens") is not None:
                sub["reasoning_tokens (included in output)"] = d["reasoning_tokens"]
            d = u.get("prompt_tokens_details") or {}
            if isinstance(d, dict) and d.get("cached_tokens") is not None:
                sub["cached_tokens (included in input)"] = d["cached_tokens"]
            usage = {"input": pin, "output": pout, "total": ptot, "sub": sub}
            if pin is None and pout is None and ptot is None:
                usage, note = None, "provider returned a usage object without token counts"
        else:
            note = "provider response contained no usage object"
        return ModelResult(text=text, resolved_model=j.get("model"), usage=usage, usage_note=note,
                           finish=ch.get("finish_reason"), raw_usage=raw)


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, cfg):
        self.cfg = cfg

    def complete(self, system: str, user: str, timeout_s: float, max_tokens: int, json_mode: bool) -> ModelResult:
        c = self.cfg
        body = {"model": c.model, "max_tokens": max_tokens, "temperature": 0, "system": system,
                "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": c.api_key, "anthropic-version": "2023-06-01", "Accept": "application/json"}
        j = _post(c.base_url + "/v1/messages", headers, body, timeout_s, c.api_key)
        try:
            text = "".join(b.get("text", "") for b in j["content"] if b.get("type") == "text")
        except (KeyError, TypeError):
            raise ProviderError("malformed", "response has no content blocks")
        u = j.get("usage")
        usage, note, raw = None, None, None
        if isinstance(u, dict):
            raw = u
            pin, pout = u.get("input_tokens"), u.get("output_tokens")
            sub = {}
            for k in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                if u.get(k):
                    sub[k + " (separate from input_tokens)"] = u[k]
            total = (pin + pout) if isinstance(pin, int) and isinstance(pout, int) else None
            usage = {"input": pin, "output": pout, "total": total, "sub": sub}
            note = "total = reported input_tokens + output_tokens (provider reports no single total)" if total is not None else None
            if pin is None and pout is None:
                usage, note = None, "provider returned a usage object without token counts"
        else:
            note = "provider response contained no usage object"
        return ModelResult(text=text, resolved_model=j.get("model"), usage=usage, usage_note=note,
                           finish=j.get("stop_reason"), raw_usage=raw)


def make_adapter(cfg):
    if cfg.provider == "openai":
        return OpenAICompatAdapter(cfg)
    if cfg.provider == "anthropic":
        return AnthropicAdapter(cfg)
    raise ProviderError("config", cfg.provider_problem() or "provider not configured")

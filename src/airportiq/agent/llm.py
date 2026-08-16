"""Pluggable LLM provider.

The model is a runtime choice, not a hardcoded dependency. Set it in .env or override per
call. Supports OpenAI, Groq (free, fast), Google Gemini (generous free tier) and Anthropic
behind one interface, because a reviewer may not have the same key you do — and a demo that
only runs with your provider is a demo that does not run.

    LLM_PROVIDER=openai
    LLM_MODEL=gpt-4.1-mini

Note on the architecture: this module exists ONLY to phrase answers. It is never imported by
`airportiq.scoring`, and `tests/test_purity.py` fails the build if it ever is. The model never
sees a scoring formula and never produces a number that reaches the user unchecked — the
narrate step emits placeholders like {{kpi.SFO.load_factor}} which the server substitutes.
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.request

_ENDPOINTS = {
    "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY", "gpt-4.1-mini"),
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY",
             "llama-3.3-70b-versatile"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
               "GOOGLE_API_KEY", "gemini-2.0-flash"),
    "anthropic": ("https://api.anthropic.com/v1/messages", "ANTHROPIC_API_KEY",
                  "claude-sonnet-4-20250514"),
}


def _load_env() -> dict[str, str]:
    """Read .env without a dependency. Real deployments would use the environment."""
    env = dict(os.environ)
    path = pathlib.Path(__file__).resolve().parents[3] / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def available_providers() -> list[str]:
    """Which providers have a key present. Used to fail helpfully rather than cryptically."""
    env = _load_env()
    return [p for p, (_, key_name, _) in _ENDPOINTS.items() if env.get(key_name)]


def complete_with_tools(messages: list[dict], tools: list | None = None,
                        *, provider: str | None = None, model: str | None = None,
                        temperature: float = 0.0, max_tokens: int = 1200) -> dict:
    """A chat completion that may request tool calls.

    Returns {"content": str|None, "tool_calls": list|None} so callers do not have to know
    the provider's response shape. Only the OpenAI-compatible providers are wired for tools
    here; Anthropic uses a different tool protocol and is left to the plain `complete` path
    rather than half-implemented.
    """
    env = _load_env()
    provider = (provider or env.get("LLM_PROVIDER") or "").lower()
    if not provider:
        found = [p for p in available_providers() if p != "anthropic"]
        if not found:
            raise RuntimeError("No OpenAI-compatible LLM key found for tool calling. "
                               "Set OPENAI_API_KEY, GROQ_API_KEY or GOOGLE_API_KEY in .env.")
        provider = found[0]
    if provider == "anthropic":
        raise RuntimeError("Tool calling here targets the OpenAI-compatible protocol. "
                           "Use openai, groq or gemini, or extend this for Anthropic's format.")

    url, key_name, default_model = _ENDPOINTS[provider]
    api_key = env.get(key_name)
    if not api_key:
        raise RuntimeError(f"{provider} selected but {key_name} is not set in .env")

    payload: dict = {
        "model": model or env.get("LLM_MODEL") or default_model,
        "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())

    msg = data["choices"][0]["message"]
    return {"content": msg.get("content"), "tool_calls": msg.get("tool_calls")}


def complete(prompt: str, *, system: str = "", provider: str | None = None,
             model: str | None = None, temperature: float = 0.0,
             max_tokens: int = 900) -> str:
    """One completion. temperature defaults to 0 because reproducibility matters more than
    variety when a reviewer is diffing two runs."""
    env = _load_env()
    provider = (provider or env.get("LLM_PROVIDER") or "").lower()

    if not provider:
        found = available_providers()
        if not found:
            raise RuntimeError(
                "No LLM key found. Put one of OPENAI_API_KEY / GROQ_API_KEY / "
                "GOOGLE_API_KEY / ANTHROPIC_API_KEY in .env (see .env.example)."
            )
        provider = found[0]

    if provider not in _ENDPOINTS:
        raise ValueError(f"unknown provider {provider!r}; have {sorted(_ENDPOINTS)}")

    url, key_name, default_model = _ENDPOINTS[provider]
    api_key = env.get(key_name)
    if not api_key:
        raise RuntimeError(f"{provider} selected but {key_name} is not set in .env")
    model = model or env.get("LLM_MODEL") or default_model

    if provider == "anthropic":
        payload = {
            "model": model, "max_tokens": max_tokens, "temperature": temperature,
            "system": system or "You are a precise aviation analyst.",
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
    else:
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
        payload = {"model": model, "messages": msgs,
                   "temperature": temperature, "max_tokens": max_tokens}
        headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}

    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())

    if provider == "anthropic":
        return "".join(b.get("text", "") for b in data.get("content", []))
    return data["choices"][0]["message"]["content"]

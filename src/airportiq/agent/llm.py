"""LLM provider factory: any model behind one interface, with no SDK dependency.

The model is a runtime choice, not a hardcoded dependency. A provider is four facts —
endpoint, key variable, default model, wire style — held in a registry:

    PROVIDERS       built-in: openai, groq, gemini (OpenAI wire style) and anthropic
                    (native Messages API, so Claude models can drive the tool-calling
                    agent too)
    LLM_BASE_URL    ANY OpenAI-compatible endpoint plugs in from .env alone — OpenRouter,
                    Mistral, DeepSeek, Together, Azure, vLLM, local Ollama — no code:
                        LLM_BASE_URL=http://localhost:11434/v1
                        LLM_API_KEY=            (empty is fine for local servers)
                        LLM_MODEL=llama3
    register_provider()  one line to add a named provider in code

Two wire styles, one contract. Everything downstream (react.py, answer.py) speaks the
OpenAI shapes: tool schemas as {"type": "function", ...}, tool calls as {"id", "function":
{"name", "arguments"}}. The Anthropic adapter translates to and from the native Messages
API at this boundary — content blocks, tool_use/tool_result, SSE events — so callers never
know which wire format ran. Current Claude models also reject sampling parameters, so the
anthropic style never sends temperature.

Note on the architecture: this module exists ONLY to phrase answers and pick tools. It is
never imported by `airportiq.scoring`, and `tests/test_purity.py` fails the build if it
ever is. The narrate step still cannot emit a digit that reaches the user unchecked.
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.request
from dataclasses import dataclass

ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class Provider:
    name: str
    url: str                 # the full completions/messages endpoint
    key_env: str
    default_model: str
    style: str = "openai"    # "openai" | "anthropic"


PROVIDERS: dict[str, Provider] = {
    "openai": Provider("openai", "https://api.openai.com/v1/chat/completions",
                       "OPENAI_API_KEY", "gpt-4.1-mini"),
    "groq": Provider("groq", "https://api.groq.com/openai/v1/chat/completions",
                     "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "gemini": Provider("gemini",
                       "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                       "GOOGLE_API_KEY", "gemini-2.0-flash"),
    "anthropic": Provider("anthropic", "https://api.anthropic.com/v1/messages",
                          "ANTHROPIC_API_KEY", "claude-opus-5", style="anthropic"),
}

# Auto-pick order when no provider is named. Deterministic, and documented in the error
# message below so a user can predict which key wins.
_PICK_ORDER = ("openai", "groq", "gemini", "anthropic")


def register_provider(name: str, url: str, key_env: str, default_model: str,
                      style: str = "openai") -> None:
    """Add a named provider at runtime. One line per LLM."""
    PROVIDERS[name] = Provider(name, url, key_env, default_model, style)


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
    return [n for n, p in PROVIDERS.items() if env.get(p.key_env)]


def _custom_provider(env: dict) -> Provider | None:
    """Synthesize a provider from LLM_BASE_URL — the zero-code path for any
    OpenAI-compatible endpoint."""
    base = env.get("LLM_BASE_URL")
    if not base:
        return None
    if not env.get("LLM_MODEL"):
        raise RuntimeError("LLM_BASE_URL is set but LLM_MODEL is not. A custom endpoint "
                           "has no sensible default model — set LLM_MODEL in .env.")
    return Provider("custom", base.rstrip("/") + "/chat/completions",
                    "LLM_API_KEY", env["LLM_MODEL"])


def _resolve(env: dict, provider: str | None = None) -> tuple[Provider, str, str]:
    """Environment -> (provider, api_key, model). Pure over `env`, so it is testable.

    Order: explicit name > LLM_PROVIDER > LLM_BASE_URL custom > first key present.
    """
    name = (provider or env.get("LLM_PROVIDER") or "").lower()

    if name and name != "custom":
        p = PROVIDERS.get(name)
        if p is None:
            raise RuntimeError(f"unknown provider {name!r}; have {sorted(PROVIDERS)} "
                               f"or a custom one via LLM_BASE_URL")
    else:
        p = _custom_provider(env)
        if p is None:
            if name == "custom":
                raise RuntimeError("LLM_PROVIDER=custom requires LLM_BASE_URL in .env")
            p = next((PROVIDERS[n] for n in _PICK_ORDER if env.get(PROVIDERS[n].key_env)),
                     None)
        if p is None:
            raise RuntimeError(
                "No LLM configured. Set one of OPENAI_API_KEY / GROQ_API_KEY / "
                "GOOGLE_API_KEY / ANTHROPIC_API_KEY in .env (see .env.example), or plug "
                "in any OpenAI-compatible endpoint with LLM_BASE_URL + LLM_MODEL "
                "(+ LLM_API_KEY if it needs one)."
            )

    key = env.get(p.key_env, "")
    if not key and p.name != "custom":       # local custom endpoints may be keyless
        raise RuntimeError(f"{p.name} selected but {p.key_env} is not set in .env")
    return p, key, (env.get("LLM_MODEL") or p.default_model)


# --------------------------------------------------------------------- HTTP plumbing

def _headers(p: Provider, api_key: str) -> dict:
    if p.style == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json"}
    h = {"content-type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _post(p: Provider, api_key: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(p.url, data=json.dumps(payload).encode(),
                                 headers=_headers(p, api_key), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _sse_events(resp):
    """Parse an SSE body into JSON events. Shared by both wire styles."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":                 # OpenAI's end sentinel; Anthropic has none
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


# ------------------------------------------------------- Anthropic <-> OpenAI shapes

def _anthropic_payload(messages: list[dict], tools: list | None, *,
                       model: str, max_tokens: int, stream: bool = False) -> dict:
    """OpenAI-shaped conversation -> native Messages API payload.

    Three translations: system messages lift into the `system` param; assistant
    tool_calls become tool_use content blocks (arguments parsed from JSON string);
    role='tool' results become tool_result blocks, with CONSECUTIVE results merged
    into one user message because Anthropic requires alternating roles.
    NEVER sends temperature — current Claude models reject sampling parameters.
    """
    system_parts: list[str] = []
    out: list[dict] = []

    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(m["content"])
        elif role == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                args = c["function"].get("arguments") or "{}"
                blocks.append({"type": "tool_use", "id": c["id"],
                               "name": c["function"]["name"],
                               "input": json.loads(args) if isinstance(args, str) else args})
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id"),
                     "content": m.get("content") or ""}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            out.append({"role": "user", "content": m.get("content") or ""})

    payload: dict = {"model": model, "max_tokens": max_tokens, "messages": out}
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if tools:
        payload["tools"] = [{"name": t["function"]["name"],
                             "description": t["function"].get("description", ""),
                             "input_schema": t["function"]["parameters"]}
                            for t in tools]
        payload["tool_choice"] = {"type": "auto"}
    if stream:
        payload["stream"] = True
    return payload


def _from_anthropic(data: dict) -> dict:
    """Native response -> the normalized {"content", "tool_calls"} shape callers expect.
    Tool arguments are re-serialized to a JSON string, matching the OpenAI wire format."""
    text_parts: list[str] = []
    calls: list[dict] = []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"id": block["id"], "type": "function",
                          "function": {"name": block["name"],
                                       "arguments": json.dumps(block.get("input") or {})}})
    return {"content": "".join(text_parts) or None, "tool_calls": calls or None}


def _assemble_anthropic_stream(events):
    """Native SSE events -> ("delta", text)* then one ("final", normalized dict).

    Text deltas forward live; tool_use inputs dribble in as input_json_delta fragments
    and are assembled per content-block index, exactly as the OpenAI adapter assembles
    its per-index tool-call fragments.
    """
    text_parts: list[str] = []
    blocks: dict[int, dict] = {}

    for ev in events:
        kind = ev.get("type")
        if kind == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") == "tool_use":
                blocks[ev.get("index", 0)] = {"id": cb.get("id", ""),
                                              "name": cb.get("name", ""), "args": ""}
        elif kind == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                text_parts.append(delta["text"])
                yield ("delta", delta["text"])
            elif delta.get("type") == "input_json_delta":
                slot = blocks.get(ev.get("index", 0))
                if slot is not None:
                    slot["args"] += delta.get("partial_json", "")

    calls = [{"id": b["id"], "type": "function",
              "function": {"name": b["name"], "arguments": b["args"] or "{}"}}
             for _, b in sorted(blocks.items())]
    yield ("final", {"content": "".join(text_parts) or None,
                     "tool_calls": calls or None})


# ------------------------------------------------------------------ OpenAI wire style

def _openai_payload(messages: list[dict], tools: list | None, *, model: str,
                    temperature: float, max_tokens: int, stream: bool = False) -> dict:
    payload: dict = {"model": model, "messages": messages,
                     "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream"] = True
    return payload


def _assemble_openai_stream(events):
    """OpenAI SSE chunks -> the same ("delta", text)* + ("final", dict) contract.

    The fiddly part is tool calls: they stream as fragments addressed by `index`, with
    the function name usually in the first fragment and the JSON arguments dribbling
    across many later ones.
    """
    content_parts: list[str] = []
    calls: dict[int, dict] = {}

    for chunk in events:
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}

        piece = delta.get("content")
        if piece:
            content_parts.append(piece)
            yield ("delta", piece)

        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = calls.setdefault(idx, {"id": "", "type": "function",
                                          "function": {"name": "", "arguments": ""}})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]

    yield ("final", {"content": "".join(content_parts) or None,
                     "tool_calls": [calls[i] for i in sorted(calls)] or None})


# ----------------------------------------------------------------------- public API

def complete_with_tools(messages: list[dict], tools: list | None = None,
                        *, provider: str | None = None, model: str | None = None,
                        temperature: float = 0.0, max_tokens: int = 1200) -> dict:
    """A chat completion that may request tool calls.

    Returns {"content": str|None, "tool_calls": list|None} so callers never know which
    provider or wire format ran.
    """
    p, key, resolved_model = _resolve(_load_env(), provider)
    model = model or resolved_model

    if p.style == "anthropic":
        payload = _anthropic_payload(messages, tools, model=model, max_tokens=max_tokens)
        return _from_anthropic(_post(p, key, payload, timeout=120))

    payload = _openai_payload(messages, tools, model=model,
                              temperature=temperature, max_tokens=max_tokens)
    data = _post(p, key, payload, timeout=120)
    msg = data["choices"][0]["message"]
    return {"content": msg.get("content"), "tool_calls": msg.get("tool_calls")}


def stream_with_tools(messages: list[dict], tools: list | None = None,
                      *, provider: str | None = None, model: str | None = None,
                      temperature: float = 0.0, max_tokens: int = 1200):
    """Same call as complete_with_tools, yielded incrementally.

    Yields ("delta", text) as prose arrives, then exactly one ("final", {...}) carrying
    the assembled message so the caller can treat it identically to the non-streaming
    path. Reassembly happens here rather than in the loop, so react.py stays a
    control-flow module and does not grow a wire-format parser.
    """
    p, key, resolved_model = _resolve(_load_env(), provider)
    model = model or resolved_model

    if p.style == "anthropic":
        payload = _anthropic_payload(messages, tools, model=model,
                                     max_tokens=max_tokens, stream=True)
        assemble = _assemble_anthropic_stream
    else:
        payload = _openai_payload(messages, tools, model=model, temperature=temperature,
                                  max_tokens=max_tokens, stream=True)
        assemble = _assemble_openai_stream

    req = urllib.request.Request(p.url, data=json.dumps(payload).encode(),
                                 headers=_headers(p, key), method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        yield from assemble(_sse_events(resp))


def complete(prompt: str, *, system: str = "", provider: str | None = None,
             model: str | None = None, temperature: float = 0.0,
             max_tokens: int = 900) -> str:
    """One completion. temperature defaults to 0 because reproducibility matters more
    than variety when a reviewer is diffing two runs (anthropic never sends it at all)."""
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    out = complete_with_tools(messages, tools=None, provider=provider, model=model,
                              temperature=temperature, max_tokens=max_tokens)
    return out.get("content") or ""

"""The LLM factory: any provider, one interface, no SDKs.

Two things are under test. RESOLUTION: which provider a given environment selects,
including the env-driven custom entry that lets any OpenAI-compatible endpoint plug in
with zero code. TRANSLATION: the pure functions that convert between the OpenAI shapes
the rest of the codebase speaks and Anthropic's native Messages API, so Claude models
can drive the tool-calling agent. All pure — no network anywhere in this file.
"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from airportiq.agent import llm


# ------------------------------------------------------------------- resolution

def test_registry_covers_the_four_built_in_providers():
    assert {"openai", "groq", "gemini", "anthropic"} <= set(llm.PROVIDERS)
    assert llm.PROVIDERS["anthropic"].style == "anthropic"
    assert llm.PROVIDERS["openai"].style == "openai"


def test_resolve_honours_an_explicit_provider():
    p, key, model = llm._resolve({"GROQ_API_KEY": "gk"}, provider="groq")
    assert p.name == "groq" and key == "gk"
    assert model == p.default_model


def test_resolve_env_model_overrides_the_default():
    _, _, model = llm._resolve({"GROQ_API_KEY": "gk", "LLM_MODEL": "llama-x"},
                               provider="groq")
    assert model == "llama-x"


def test_resolve_auto_picks_the_first_provider_with_a_key():
    p, _, _ = llm._resolve({"GROQ_API_KEY": "gk", "ANTHROPIC_API_KEY": "ak"})
    assert p.name in ("groq", "anthropic")
    only_anthropic, _, _ = llm._resolve({"ANTHROPIC_API_KEY": "ak"})
    assert only_anthropic.name == "anthropic", \
        "anthropic must be usable for the agent now that it has a native adapter"


def test_resolve_base_url_creates_a_custom_openai_style_provider():
    """Any OpenAI-compatible endpoint plugs in from .env alone — OpenRouter, Ollama,
    vLLM, Mistral — no code change."""
    env = {"LLM_BASE_URL": "http://localhost:11434/v1",
           "LLM_API_KEY": "sk-x", "LLM_MODEL": "llama3"}
    p, key, model = llm._resolve(env)
    assert p.style == "openai"
    assert p.url == "http://localhost:11434/v1/chat/completions"
    assert key == "sk-x" and model == "llama3"


def test_resolve_custom_provider_requires_a_model():
    try:
        llm._resolve({"LLM_BASE_URL": "http://localhost:1234/v1"})
        assert False, "a custom endpoint with no LLM_MODEL must refuse, not guess"
    except RuntimeError as e:
        assert "LLM_MODEL" in str(e)


def test_resolve_errors_name_the_options():
    try:
        llm._resolve({})
        assert False, "no keys at all must be a helpful error"
    except RuntimeError as e:
        msg = str(e)
        assert "OPENAI_API_KEY" in msg and "ANTHROPIC_API_KEY" in msg
        assert "LLM_BASE_URL" in msg, "the zero-code custom option must be advertised"


def test_register_provider_extends_the_registry():
    llm.register_provider("mistral", "https://api.mistral.ai/v1/chat/completions",
                          "MISTRAL_API_KEY", "mistral-large-latest")
    try:
        p, key, _ = llm._resolve({"MISTRAL_API_KEY": "mk"}, provider="mistral")
        assert p.style == "openai" and key == "mk"
    finally:
        del llm.PROVIDERS["mistral"]


# ---------------------------------------------------- OpenAI -> Anthropic payload

_TOOLS = [{"type": "function", "function": {
    "name": "get_airport_metrics",
    "description": "All KPIs for one airport.",
    "parameters": {"type": "object", "properties": {"airport": {"type": "string"}},
                   "required": ["airport"]}}}]


def test_anthropic_payload_translates_system_and_tools():
    messages = [{"role": "system", "content": "You are an analyst."},
                {"role": "user", "content": "How constrained is SFO?"}]
    p = llm._anthropic_payload(messages, _TOOLS, model="claude-opus-5", max_tokens=500)
    assert p["system"] == "You are an analyst."
    assert all(m["role"] != "system" for m in p["messages"])
    assert p["tools"] == [{"name": "get_airport_metrics",
                           "description": "All KPIs for one airport.",
                           "input_schema": _TOOLS[0]["function"]["parameters"]}]
    assert p["tool_choice"] == {"type": "auto"}
    assert "temperature" not in p, \
        "current Claude models reject sampling parameters — never send one"
    assert p["max_tokens"] == 500


def test_anthropic_payload_translates_tool_calls_and_results():
    """An OpenAI-shaped agent turn must round-trip: assistant tool_calls become
    tool_use blocks, and role='tool' results become tool_result blocks in ONE user
    message (Anthropic requires alternating roles)."""
    messages = [
        {"role": "user", "content": "Compare SFO and SNA."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_airport_metrics",
                          "arguments": '{"airport": "SFO"}'}},
            {"id": "call_2", "type": "function",
             "function": {"name": "get_airport_metrics",
                          "arguments": '{"airport": "SNA"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"composite": 75}'},
        {"role": "tool", "tool_call_id": "call_2", "content": '{"composite": 66}'},
    ]
    p = llm._anthropic_payload(messages, _TOOLS, model="claude-opus-5", max_tokens=500)
    assistant = p["messages"][1]
    uses = [b for b in assistant["content"] if b["type"] == "tool_use"]
    assert [u["id"] for u in uses] == ["call_1", "call_2"]
    assert uses[0]["input"] == {"airport": "SFO"}, "arguments must be parsed JSON"

    results = p["messages"][2]
    assert results["role"] == "user"
    blocks = [b for b in results["content"] if b["type"] == "tool_result"]
    assert [b["tool_use_id"] for b in blocks] == ["call_1", "call_2"], \
        "consecutive tool results must merge into one user message"
    assert len(p["messages"]) == 3


# ---------------------------------------------------- Anthropic -> OpenAI response

def test_anthropic_response_normalises_to_the_openai_shape():
    data = {"content": [
        {"type": "text", "text": "Checking SFO."},
        {"type": "tool_use", "id": "toolu_1", "name": "get_airport_metrics",
         "input": {"airport": "SFO"}},
    ], "stop_reason": "tool_use"}
    out = llm._from_anthropic(data)
    assert out["content"] == "Checking SFO."
    call = out["tool_calls"][0]
    assert call["id"] == "toolu_1"
    assert call["function"]["name"] == "get_airport_metrics"
    assert json.loads(call["function"]["arguments"]) == {"airport": "SFO"}, \
        "arguments must be a JSON string, matching what the OpenAI path returns"


def test_anthropic_response_without_tool_calls_has_none():
    out = llm._from_anthropic({"content": [{"type": "text", "text": "Done."}],
                               "stop_reason": "end_turn"})
    assert out["content"] == "Done." and out["tool_calls"] is None


# ------------------------------------------------------------- streaming assembly

def test_anthropic_stream_yields_deltas_then_a_normalised_final():
    events = [
        {"type": "message_start", "message": {}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "SFO is "}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "constrained."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_9",
                           "name": "get_delay_breakdown"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"airpo'}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": 'rt": "SFO"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_stop"},
    ]
    out = list(llm._assemble_anthropic_stream(iter(events)))
    deltas = [text for kind, text in out if kind == "delta"]
    assert "".join(deltas) == "SFO is constrained."
    kind, final = out[-1]
    assert kind == "final"
    assert final["content"] == "SFO is constrained."
    call = final["tool_calls"][0]
    assert call["id"] == "toolu_9"
    assert call["function"]["name"] == "get_delay_breakdown"
    assert json.loads(call["function"]["arguments"]) == {"airport": "SFO"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print(f"  ok  {name}")
    print("all llm factory tests passed")

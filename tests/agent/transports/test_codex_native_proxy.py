"""Native web search respects explicitly verified Responses proxies."""

def test_openai_native_swaps_on_explicitly_allowed_proxy(monkeypatch):
    from agent.transports import get_transport
    transport = get_transport("codex_responses")
    import agent.transports.codex as codex_mod

    monkeypatch.setattr(codex_mod, "_openai_prefers_native_web_search", lambda: True)
    monkeypatch.setattr(codex_mod, "_openai_native_proxy_allowed", lambda params: params.get("base_url") == "https://proxy.example/v1")
    tools = [{"type": "function", "function": {
        "name": "web_search", "description": "Search the web.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}]
    allowed = transport.build_kwargs(model="proxy-model", messages=[], tools=tools,
                                     base_url="https://proxy.example/v1", is_codex_backend=False)
    assert {"type": "web_search"} in allowed["tools"]
    denied = transport.build_kwargs(model="proxy-model", messages=[], tools=tools,
                                    base_url="https://other.example/v1", is_codex_backend=False)
    assert not any(t.get("type") == "web_search" for t in denied["tools"])
    assert any(t.get("name") == "web_search" for t in denied["tools"])

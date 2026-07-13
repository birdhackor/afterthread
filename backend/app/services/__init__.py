"""Service layer: the LLM client and the AI memory workflows.

Kept separate from ``app.routers`` so the network/AI concern (constructing an
OpenAI-compatible client, prompting, parsing, sanitizing) is isolated from HTTP
concerns, and so tests can monkeypatch at the service boundary
(``generate_json`` or the three workflow functions) without touching real
network I/O.
"""

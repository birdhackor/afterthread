"""Service layer: the LLM client and the AI memory workflows.

Kept separate from ``afterthread.routers`` so the network/AI concern (constructing an
OpenAI-compatible client, prompting, parsing, sanitizing) is isolated from HTTP
concerns, and so tests can monkeypatch at the service boundary
(``generate_structured`` or the three workflow functions) without touching real
network I/O.
"""

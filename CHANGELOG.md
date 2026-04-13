# Changelog

## [1.0.0] — 2026-04-13 — MVP

### Added

- **Tunables**: `send_to_viking` now accepts `top_k` (1–20) and `score_threshold` (0–1) parameters. Search endpoint is Valve-overridable via `search_endpoint`.
- **Async streaming**: All tools are now `async`. New `query_openviking_stream` yields text chunks via `httpx.AsyncClient` for large context reads.
- **Write-back**: New `write_to_viking(uri, content, session_id)` tool. Sends content to OpenViking's session API (`/session/add_message` + `/session/commit`). Auto-generates session IDs from `webui_{user_id}_{chat_id}`.
- **Audit logging**: Dedicated `viking_bridge.audit` logger emits structured lines: `user=, action=, uri=, time=` for every tool call.
- **Per-user ACL**: `user_mapping` Valve (JSON dict) maps OpenWebUI user IDs to OpenViking user IDs. Each request sends the resolved `X-OpenViking-User` header.
- **Metrics**: In-process counters (`tool_calls_total`, `tool_errors_total`, `tool_latency_seconds_*`). Access via `get_metrics()`.
- **Pydantic v2 models**: `VikingSearch` and `VikingWrite` for validated tool schemas.

### Changed

- HTTP client migrated from `requests` to `httpx` (async-native).
- `_format_find_results` now accepts `score_threshold` to filter low-scoring results before formatting.
- Version bumped to `1.0.0`.

### Migration notes

- **Dependency**: Add `httpx` to your environment. `requests` is no longer imported by `viking_bridge.py` (still used by `sync_knowledge.py` and `healthcheck_bridge.py`).
- **Async**: All tool methods are now coroutines. OpenWebUI handles this natively (`if asyncio.iscoroutinefunction(tool): await tool(...)`).
- **Backward compat**: `send_to_viking(prompt)` still works — `top_k` and `score_threshold` default to previous behavior (5 results, no threshold).

## [0.3.0] — 2026-04-12 — POC

- Initial POC: `query_openviking` (L0/L1/L2 chain), `send_to_viking` (semantic search).
- Valve-based auth, sync `requests` client.
- `sync_knowledge.py` for offline Knowledge fallback.
- `healthcheck_bridge.py` for end-to-end validation.

"""
title: OpenViking Bridge
author: OpenViking Bridge Contributors
version: 1.0.0
license: MIT
description: Query, search, and write to OpenViking with async streaming, tunables, sessions, and audit logging.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger("viking_bridge")
audit_log = logging.getLogger("viking_bridge.audit")

# ---------------------------------------------------------------------------
# OpenViking API endpoints (relative to base URL)
# ---------------------------------------------------------------------------
_EP_CONTENT_READ = "/api/v1/content/read"
_EP_CONTENT_OVERVIEW = "/api/v1/content/overview"
_EP_CONTENT_ABSTRACT = "/api/v1/content/abstract"
_EP_SEARCH_FIND = "/api/v1/search/find"
_EP_SESSION_ADD = "/api/v1/session/add_message"
_EP_SESSION_COMMIT = "/api/v1/session/commit"

_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Prometheus-style metrics (in-process counters, no extra server)
# ---------------------------------------------------------------------------
_metrics: dict[str, float] = {
    "tool_calls_total": 0,
    "tool_errors_total": 0,
    "tool_latency_seconds_sum": 0,
    "tool_latency_seconds_count": 0,
}


def _record_call(duration: float, error: bool = False) -> None:
    _metrics["tool_calls_total"] += 1
    _metrics["tool_latency_seconds_sum"] += duration
    _metrics["tool_latency_seconds_count"] += 1
    if error:
        _metrics["tool_errors_total"] += 1


def get_metrics() -> dict[str, float]:
    """Return a snapshot of in-process tool metrics."""
    m = dict(_metrics)
    count = m["tool_latency_seconds_count"]
    m["tool_latency_seconds_avg"] = (
        m["tool_latency_seconds_sum"] / count if count > 0 else 0
    )
    return m


def _safe_strip(value: Optional[str]) -> str:
    """Safely strip whitespace from a string, handling None values."""
    return value.strip() if value else ""


# ---------------------------------------------------------------------------
# Pydantic models — tunables for tool schemas
# ---------------------------------------------------------------------------


class VikingSearch(BaseModel):
    """Parameters for semantic search in OpenViking."""

    prompt: str = Field(..., description="Natural-language query to search for.")
    top_k: int = Field(5, ge=1, le=20, description="Max results to return.")
    score_threshold: float = Field(
        0.0, ge=0, le=1, description="Minimum similarity score (0 = no filter)."
    )


class VikingWrite(BaseModel):
    """Parameters for writing content back to OpenViking."""

    uri: str = Field(..., description="Viking URI to write to.")
    content: str = Field(..., description="Content to write.")
    session_id: Optional[str] = Field(
        None, description="Session ID for tracking. Auto-generated if omitted."
    )


# ---------------------------------------------------------------------------
# Main Tool class
# ---------------------------------------------------------------------------


class Tools:
    """OpenWebUI Tool — register under Workspace → Tools."""

    class Valves(BaseModel):
        openviking_base_url: str = Field(
            default="http://localhost:1933",
            description="OpenViking server URL",
        )
        openviking_api_key: str = Field(
            default="",
            description="OpenViking API key",
        )
        openviking_account: str = Field(
            default="default",
            description="OpenViking multi-tenant account ID",
        )
        openviking_user: str = Field(
            default="",
            description="OpenViking multi-tenant user ID",
        )
        # Per-user mapping: OpenWebUI user email/id -> OpenViking user ID
        user_mapping: str = Field(
            default="",
            description=(
                "JSON dict mapping OpenWebUI user IDs to OpenViking user IDs. "
                'Example: {"alice@co.com": "viking_alice", "bob": "viking_bob"}. '
                "Empty = use openviking_user for everyone."
            ),
        )
        search_endpoint: str = Field(
            default=_EP_SEARCH_FIND,
            description="Override the search endpoint path (default: /api/v1/search/find).",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._client: Optional[httpx.AsyncClient] = None
        self._sessions: dict[str, list[dict]] = {}  # session_id -> messages
        self._user_map_cache: Optional[dict[str, str]] = None

    # ------------------------------------------------------------------
    # Async HTTP client
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "X-Api-Key": self.valves.openviking_api_key,
                "Content-Type": "application/json",
            }
            if self.valves.openviking_account:
                headers["X-OpenViking-Account"] = self.valves.openviking_account
            if self.valves.openviking_user:
                headers["X-OpenViking-User"] = self.valves.openviking_user
            self._client = httpx.AsyncClient(
                base_url=str(self.valves.openviking_base_url or "").rstrip("/"),
                headers=headers,
                timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
            )
        return self._client

    def _reset_client(self) -> None:
        """Force client rebuild when valves change."""
        if self._client and not self._client.is_closed:
            try:
                asyncio.create_task(self._client.aclose())
            except RuntimeError:
                # No running event loop, close synchronously
                self._close_client_sync()
        self._client = None
        self._user_map_cache = None

    def _close_client_sync(self) -> None:
        """Synchronously close the client when no event loop is available."""
        if self._client and not self._client.is_closed:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._client.aclose())
            loop.close()

    def _resolve_viking_user(self, webui_user_id: str = "") -> Optional[str]:
        """Map an OpenWebUI user ID to an OpenViking user ID via Valve config."""
        if not self.valves.user_mapping:
            return None
        if self._user_map_cache is None:
            import json

            try:
                self._user_map_cache = json.loads(self.valves.user_mapping)
            except (json.JSONDecodeError, TypeError):
                log.warning("Invalid user_mapping JSON in Valves; ignoring.")
                self._user_map_cache = {}
        return self._user_map_cache.get(webui_user_id)

    def _audit(
        self, action: str, uri: str, user_id: str = "", extra: str = ""
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        audit_log.info(
            "user=%s action=%s uri=%s time=%s %s",
            user_id or "-",
            action,
            uri,
            ts,
            extra,
        )

    # ------------------------------------------------------------------
    # Core async request helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_body: dict | None = None,
        user_id: str = "",
    ) -> dict:
        if not self.valves.openviking_api_key:
            raise _VikingError(
                "OpenViking API key is not configured. Set it in Tool Valves."
            )

        client = self._get_client()

        # Per-request user override
        headers = {}
        viking_user = self._resolve_viking_user(user_id)
        if viking_user:
            headers["X-OpenViking-User"] = viking_user

        try:
            resp = await client.request(
                method, endpoint, params=params, json=json_body, headers=headers
            )
        except httpx.ConnectError:
            log.error(
                "Connection failed to OpenViking at %s",
                self.valves.openviking_base_url,
            )
            raise _VikingError("OpenViking server is unreachable.")
        except httpx.TimeoutException:
            log.error("Request timed out: %s %s", method, endpoint)
            raise _VikingError("OpenViking request timed out.")

        if resp.status_code in (401, 403):
            log.error(
                "Auth failed: %s %s -> %d", method, endpoint, resp.status_code
            )
            raise _VikingError("OpenViking authentication failed. Check API key.")

        if resp.status_code >= 400:
            detail = _safe_detail(resp)
            log.error(
                "HTTP %d from %s %s: %s",
                resp.status_code,
                method,
                endpoint,
                detail,
            )
            raise _VikingError(
                f"OpenViking returned HTTP {resp.status_code}: {detail}"
            )

        try:
            data = resp.json()
        except (ValueError, TypeError):
            log.error("Malformed JSON from %s %s", method, endpoint)
            raise _VikingError("OpenViking returned invalid JSON.")

        if not isinstance(data, dict):
            raise _VikingError("Unexpected response format from OpenViking.")

        if data.get("status") != "ok":
            err = data.get("error", "unknown error")
            raise _VikingError(f"OpenViking error: {err}")

        return data

    async def _get(
        self, endpoint: str, params: dict | None = None, user_id: str = ""
    ) -> dict:
        return await self._request("GET", endpoint, params=params, user_id=user_id)

    async def _post(
        self, endpoint: str, json_body: dict | None = None, user_id: str = ""
    ) -> dict:
        return await self._request(
            "POST", endpoint, json_body=json_body, user_id=user_id
        )

    # ------------------------------------------------------------------
    # Streaming read helper
    # ------------------------------------------------------------------

    async def _stream_get(
        self, endpoint: str, params: dict | None = None, user_id: str = ""
    ) -> AsyncIterator[str]:
        """Yield text chunks from a streaming GET request."""
        if not self.valves.openviking_api_key:
            yield "Error: OpenViking API key is not configured."
            return

        client = self._get_client()
        headers = {}
        viking_user = self._resolve_viking_user(user_id)
        if viking_user:
            headers["X-OpenViking-User"] = viking_user

        url = endpoint
        try:
            async with client.stream(
                "GET", url, params=params, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    detail = _safe_detail(resp)
                    yield f"Error: OpenViking returned HTTP {resp.status_code}: {detail}"
                    return
                async for chunk in resp.aiter_text():
                    yield chunk
        except httpx.ConnectError:
            yield "Error: OpenViking server is unreachable."
        except httpx.TimeoutException:
            yield "Error: OpenViking request timed out."

    # ------------------------------------------------------------------
    # Tools exposed to OpenWebUI
    # ------------------------------------------------------------------

    async def query_openviking(
        self,
        context_id: str,
        __user__: dict | None = None,
    ) -> str:
        """
        Fetch a specific context entry from OpenViking by its Viking URI.

        :param context_id: A Viking URI, e.g. "viking://resources/example_app/AUTH_QUICK_REFERENCE"
        :return: The text content of the entry.
        """
        t0 = time.monotonic()
        user_id = (__user__ or {}).get("id", "")

        if not context_id or not _safe_strip(context_id):
            return "Error: context_id (Viking URI) is required."
        uri = _safe_strip(context_id)

        self._audit("query", uri, user_id)

        try:
            data = await self._get(_EP_CONTENT_READ, params={"uri": uri}, user_id=user_id)
            result = data.get("result")
            if result and isinstance(result, str) and _safe_strip(result):
                _record_call(time.monotonic() - t0)
                return result

            # Empty L2 — try overview (L1)
            data = await self._get(
                _EP_CONTENT_OVERVIEW, params={"uri": uri}, user_id=user_id
            )
            result = data.get("result")
            if result and isinstance(result, str) and _safe_strip(result):
                _record_call(time.monotonic() - t0)
                return result

            # Empty overview — try abstract (L0)
            data = await self._get(
                _EP_CONTENT_ABSTRACT, params={"uri": uri}, user_id=user_id
            )
            result = data.get("result")
            if result and isinstance(result, str) and _safe_strip(result):
                _record_call(time.monotonic() - t0)
                return result

            _record_call(time.monotonic() - t0)
            return "No content found for the given URI."
        except _VikingError as exc:
            _record_call(time.monotonic() - t0, error=True)
            return f"Error: {exc}"

    async def send_to_viking(
        self,
        prompt: str,
        top_k: int = 5,
        score_threshold: float = 0.0,
        __user__: dict | None = None,
    ) -> str:
        """
        Perform a semantic search in OpenViking using the given prompt.
        Returns the top matching context entries with URIs and abstracts.

        :param prompt: The natural-language query to search for.
        :param top_k: Maximum number of results (1-20, default 5).
        :param score_threshold: Minimum similarity score to include (0-1, default 0 = no filter).
        :return: Formatted search results.
        """
        t0 = time.monotonic()
        user_id = (__user__ or {}).get("id", "")

        # Validate via Pydantic
        try:
            search = VikingSearch(
                prompt=prompt or "",
                top_k=max(1, min(20, top_k)),
                score_threshold=max(0.0, min(1.0, score_threshold)),
            )
        except Exception:
            return "Error: invalid search parameters."

        if not _safe_strip(search.prompt):
            return "Error: prompt is required."

        self._audit(
            "search",
            self.valves.search_endpoint,
            user_id,
            f"top_k={search.top_k} threshold={search.score_threshold}",
        )

        try:
            data = await self._post(
                self.valves.search_endpoint,
                json_body={
                    "query": _safe_strip(search.prompt),
                    "limit": search.top_k,
                },
                user_id=user_id,
            )
            result = data.get("result")
            if not isinstance(result, dict):
                _record_call(time.monotonic() - t0)
                return "Unexpected search result format."

            formatted = _format_find_results(result, search.score_threshold)
            _record_call(time.monotonic() - t0)
            return formatted
        except _VikingError as exc:
            _record_call(time.monotonic() - t0, error=True)
            return f"Error: {exc}"

    async def write_to_viking(
        self,
        uri: str,
        content: str,
        session_id: Optional[str] = None,
        __user__: dict | None = None,
    ) -> str:
        """
        Write content back to OpenViking under a tracked session.

        :param uri: Viking URI to write to.
        :param content: The content to write.
        :param session_id: Optional session ID. Auto-generated from user+chat if omitted.
        :return: Confirmation or error message.
        """
        t0 = time.monotonic()
        user_id = (__user__ or {}).get("id", "")
        chat_id = (__user__ or {}).get("chat_id", "")

        try:
            write = VikingWrite(uri=uri or "", content=content or "", session_id=session_id)
        except Exception:
            return "Error: invalid write parameters."

        if not _safe_strip(write.uri):
            return "Error: uri is required."
        if not _safe_strip(write.content):
            return "Error: content is required."

        # Resolve or generate session ID
        sid = write.session_id or f"webui_{user_id}_{chat_id}" if (user_id or chat_id) else None

        self._audit("write", write.uri, user_id, f"session={sid or 'none'}")

        try:
            # Add message to session
            body: dict = {
                "uri": _safe_strip(write.uri),
                "content": _safe_strip(write.content),
                "role": "user",
            }
            if sid:
                body["session_id"] = sid

            data = await self._post(_EP_SESSION_ADD, json_body=body, user_id=user_id)

            # Track locally
            if sid:
                self._sessions.setdefault(sid, []).append(
                    {"uri": write.uri, "role": "user", "content": write.content[:100]}
                )

            # Commit the session
            commit_body: dict = {"uri": _safe_strip(write.uri)}
            if sid:
                commit_body["session_id"] = sid

            await self._post(_EP_SESSION_COMMIT, json_body=commit_body, user_id=user_id)

            msg_count = len(self._sessions.get(sid, [])) if sid else 0
            _record_call(time.monotonic() - t0)
            return (
                f"Written to `{write.uri}`. "
                f"Session: `{sid or 'stateless'}` ({msg_count} messages tracked)."
            )
        except _VikingError as exc:
            _record_call(time.monotonic() - t0, error=True)
            return f"Error: {exc}"

    async def query_openviking_stream(
        self,
        context_id: str,
        __user__: dict | None = None,
    ) -> AsyncIterator[str]:
        """
        Stream content from OpenViking by Viking URI.
        Yields text chunks as they arrive — ideal for large context entries.

        :param context_id: A Viking URI.
        :return: Async iterator of text chunks.
        """
        user_id = (__user__ or {}).get("id", "")
        if not _safe_strip(context_id):
            yield "Error: context_id (Viking URI) is required."
            return

        uri = _safe_strip(context_id)
        self._audit("query_stream", uri, user_id)

        async for chunk in self._stream_get(
            _EP_CONTENT_READ, params={"uri": uri}, user_id=user_id
        ):
            yield chunk


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _VikingError(Exception):
    pass


def _safe_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("detail", body.get("error", resp.text[:200])))
    except Exception:
        pass
    return resp.text[:200] if resp.text else "(empty body)"


def _format_find_results(result: dict, score_threshold: float = 0.0) -> str:
    parts: list[str] = []
    total = result.get("total", 0)
    for section in ("resources", "memories", "skills"):
        items = result.get(section, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            score = item.get("score", 0)
            if score_threshold > 0 and score < score_threshold:
                continue
            uri = item.get("uri", "unknown")
            abstract = item.get("abstract", "")
            parts.append(f"**[{score:.2f}]** `{uri}`\n{abstract}\n")
    if not parts:
        return f"No results found (total={total})."
    header = f"Found {total} result(s):\n\n"
    return header + "\n---\n".join(parts)

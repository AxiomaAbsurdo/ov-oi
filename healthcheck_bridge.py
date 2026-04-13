#!/usr/bin/env python3
"""
End-to-end health check for the OpenWebUI + Ollama + OpenViking Bridge stack.

What it verifies:
1) Ollama is reachable.
2) OpenWebUI is reachable.
3) OpenWebUI auth works (API token or auto-generated local JWT).
4) The OpenViking bridge tool exists and has valves configured.
5) OpenViking is reachable with the same valves configured in OpenWebUI.
6) OpenWebUI can execute send_to_viking via the model.
7) OpenWebUI can execute query_openviking via the model.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

import requests


CONNECT_TIMEOUT = 5
READ_TIMEOUT = 45
STREAM_READ_TIMEOUT = 240


class CheckError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_hs256_jwt(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_part = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_part = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_part}.{payload_part}.{_b64url(signature)}"


def _url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def _pass(message: str) -> None:
    print(f"[PASS] {message}")


def _info(message: str) -> None:
    print(f"[INFO] {message}")


def _fail(message: str) -> None:
    raise CheckError(message)


def _discover_webui_db_path() -> Path | None:
    env_db = os.environ.get("OPENWEBUI_DB_PATH", "").strip()
    if env_db:
        p = Path(env_db)
        if p.exists():
            return p

    env_data_dir = os.environ.get("OPENWEBUI_DATA_DIR", "").strip()
    if env_data_dir:
        p = Path(env_data_dir) / "webui.db"
        if p.exists():
            return p

    home = Path.home()
    candidates = [
        home / ".open-webui" / "webui.db",
        home / ".local" / "share" / "open-webui" / "webui.db",
        home / ".local" / "share" / "open_webui" / "webui.db",
        home / ".config" / "open-webui" / "webui.db",
    ]

    candidates.extend(home.glob("personal-settings/lib/python*/site-packages/open_webui/data/webui.db"))

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _discover_admin_user_id(db_path: Path) -> str | None:
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT id FROM user WHERE role='admin' ORDER BY last_active_at DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                return str(row[0])

            row = conn.execute("SELECT id FROM user ORDER BY last_active_at DESC LIMIT 1").fetchone()
            if row and row[0]:
                return str(row[0])
        finally:
            conn.close()
    except Exception:
        return None
    return None


def _discover_webui_secret() -> str | None:
    for env_name in ("OPENWEBUI_SECRET_KEY", "WEBUI_SECRET_KEY", "WEBUI_JWT_SECRET_KEY"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value

    secret_file = Path.home() / ".webui_secret_key"
    if secret_file.exists():
        value = secret_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    return None


def _auth_candidates() -> Iterable[tuple[str, str]]:
    # 1) Explicit token (preferred)
    for env_name in ("OPENWEBUI_API_TOKEN", "OPENWEBUI_TOKEN", "OPENWEBUI_JWT"):
        token = os.environ.get(env_name, "").strip()
        if token:
            yield (f"{env_name}", token)

    # 2) Local JWT auto-generation fallback
    user_id = os.environ.get("OPENWEBUI_USER_ID", "").strip()
    db_path = _discover_webui_db_path()
    if not user_id and db_path:
        user_id = _discover_admin_user_id(db_path) or ""

    secret = _discover_webui_secret() or ""
    if user_id and secret:
        token = _make_hs256_jwt({"id": user_id}, secret)
        yield ("local_jwt_autodiscovery", token)


def _authenticate_openwebui(session: requests.Session, base_url: str) -> tuple[dict, str]:
    tools_url = _url(base_url, "/api/v1/tools/list")

    for source, token in _auth_candidates():
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = session.get(tools_url, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except requests.RequestException:
            continue

        if resp.status_code == 200:
            return headers, source

    _fail(
        "OpenWebUI authentication failed. Set OPENWEBUI_API_TOKEN or provide local auth context "
        "(OPENWEBUI_USER_ID + WEBUI_SECRET_KEY, or ~/.webui_secret_key + readable webui.db)."
    )


def _check_ollama(session: requests.Session, base_url: str) -> None:
    url = _url(base_url, "/api/version")
    try:
        resp = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    except requests.RequestException as exc:
        _fail(f"Ollama unreachable at {url}: {exc}")
    if resp.status_code != 200:
        _fail(f"Ollama check failed at {url}: HTTP {resp.status_code}")

    loaded_count = None
    try:
        ps_resp = session.get(_url(base_url, "/api/ps"), timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        if ps_resp.status_code == 200:
            ps_data = ps_resp.json()
            models = ps_data.get("models", []) if isinstance(ps_data, dict) else []
            if isinstance(models, list):
                loaded_count = len(models)
    except requests.RequestException:
        loaded_count = None

    if loaded_count is None:
        _pass(f"Ollama reachable at {base_url}")
    else:
        _pass(f"Ollama reachable at {base_url} (loaded models: {loaded_count})")


def _check_openwebui(session: requests.Session, base_url: str) -> None:
    url = _url(base_url, "/")
    try:
        resp = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    except requests.RequestException as exc:
        _fail(f"OpenWebUI unreachable at {url}: {exc}")
    if resp.status_code >= 500:
        _fail(f"OpenWebUI health failed at {url}: HTTP {resp.status_code}")
    _pass(f"OpenWebUI reachable at {base_url}")


def _load_tool_and_valves(
    session: requests.Session,
    openwebui_base_url: str,
    auth_headers: dict,
    tool_id: str,
) -> dict:
    list_url = _url(openwebui_base_url, "/api/v1/tools/list")
    resp = session.get(list_url, headers=auth_headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    if resp.status_code != 200:
        _fail(f"Failed to list OpenWebUI tools: HTTP {resp.status_code} {resp.text[:200]}")

    try:
        tools = resp.json()
    except Exception:
        _fail("Failed to parse OpenWebUI tools list response as JSON.")

    if not isinstance(tools, list):
        _fail("Unexpected OpenWebUI tools list format.")

    if not any(isinstance(t, dict) and t.get("id") == tool_id for t in tools):
        _fail(f"Tool '{tool_id}' not found in OpenWebUI.")
    _pass(f"Tool '{tool_id}' is registered in OpenWebUI")

    valves_url = _url(openwebui_base_url, f"/api/v1/tools/id/{tool_id}/valves")
    resp = session.get(valves_url, headers=auth_headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    if resp.status_code != 200:
        _fail(f"Failed to load valves for '{tool_id}': HTTP {resp.status_code} {resp.text[:200]}")

    try:
        valves = resp.json()
    except Exception:
        _fail("Failed to parse tool valves response as JSON.")

    if not isinstance(valves, dict):
        _fail("Unexpected tool valves format.")

    required = ["openviking_base_url", "openviking_api_key"]
    missing = [k for k in required if not str(valves.get(k, "")).strip()]
    if missing:
        _fail(f"Tool valves missing required values: {', '.join(missing)}")

    _pass("Tool valves are configured")
    return valves


def _check_openviking_with_valves(
    session: requests.Session,
    valves: dict,
    target_uri: str,
) -> None:
    base_url = str(valves.get("openviking_base_url", "")).rstrip("/")
    api_key = str(valves.get("openviking_api_key", "")).strip()
    account = str(valves.get("openviking_account", "")).strip()
    user = str(valves.get("openviking_user", "")).strip()

    headers = {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
    }
    if account:
        headers["X-OpenViking-Account"] = account
    if user:
        headers["X-OpenViking-User"] = user

    url = _url(base_url, "/api/v1/fs/ls")
    params = {
        "uri": target_uri,
        "simple": "false",
        "recursive": "false",
    }

    try:
        resp = session.get(url, headers=headers, params=params, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    except requests.RequestException as exc:
        _fail(f"OpenViking request failed at {url}: {exc}")

    if resp.status_code != 200:
        _fail(f"OpenViking responded with HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except Exception:
        _fail("OpenViking returned non-JSON response.")

    if not isinstance(data, dict) or data.get("status") != "ok":
        _fail(f"OpenViking returned unexpected envelope: {str(data)[:200]}")

    entries = data.get("result", [])
    count = len(entries) if isinstance(entries, list) else 0
    _pass(f"OpenViking reachable with tool valves (entries listed: {count})")


_URI_RE = re.compile(r"viking://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")


def _extract_uris(text: str) -> list[str]:
    uris = []
    for match in _URI_RE.findall(text):
        cleaned = match.strip().strip("`\"'").rstrip(",.;)")
        if cleaned and cleaned not in uris:
            uris.append(cleaned)
    return uris


def _collect_source_names_and_uris(source_items: list[dict]) -> tuple[list[str], list[str]]:
    names: list[str] = []
    uris: list[str] = []

    for item in source_items:
        if not isinstance(item, dict):
            continue

        source = item.get("source", {})
        if isinstance(source, dict):
            name = str(source.get("name", "")).strip()
            if name:
                names.append(name)

        documents = item.get("document", [])
        if isinstance(documents, list):
            for doc in documents:
                if isinstance(doc, str):
                    for uri in _extract_uris(doc):
                        if uri not in uris:
                            uris.append(uri)

        metadata_items = item.get("metadata", [])
        if isinstance(metadata_items, list):
            for md in metadata_items:
                if isinstance(md, dict):
                    for uri in _extract_uris(json.dumps(md, ensure_ascii=True)):
                        if uri not in uris:
                            uris.append(uri)

    return names, uris


def _run_stream_tool_probe(
    session: requests.Session,
    openwebui_base_url: str,
    auth_headers: dict,
    model: str,
    tool_id: str,
    prompt: str,
    expected_function: str,
) -> tuple[list[str], list[str]]:
    url = _url(openwebui_base_url, "/api/v1/chat/completions")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "tool_ids": [tool_id],
    }

    headers = {
        **auth_headers,
        "Content-Type": "application/json",
    }

    try:
        with session.post(
            url,
            headers=headers,
            json=payload,
            stream=True,
            timeout=(CONNECT_TIMEOUT, STREAM_READ_TIMEOUT),
        ) as resp:
            if resp.status_code != 200:
                _fail(f"Tool probe request failed: HTTP {resp.status_code} {resp.text[:200]}")

            source_items: list[dict] = []

            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue

                event_payload = line[6:].strip()
                if event_payload == "[DONE]":
                    break

                try:
                    event = json.loads(event_payload)
                except json.JSONDecodeError:
                    continue

                if isinstance(event, dict) and isinstance(event.get("sources"), list):
                    for item in event["sources"]:
                        if isinstance(item, dict):
                            source_items.append(item)

                    names, uris = _collect_source_names_and_uris(source_items)
                    if any(name == f"{tool_id}/{expected_function}" for name in names):
                        return names, uris

            names, uris = _collect_source_names_and_uris(source_items)
            return names, uris
    except requests.ReadTimeout:
        _fail(
            "Tool probe timed out waiting for model output. "
            "Ollama may be busy/stuck on another generation. "
            "Retry after clearing active runs or use a faster model with --model."
        )
    except requests.RequestException as exc:
        _fail(f"Tool probe request failed: {exc}")


def _run_end_to_end_tool_checks(
    session: requests.Session,
    openwebui_base_url: str,
    auth_headers: dict,
    model: str,
    tool_id: str,
) -> None:
    search_prompt = (
        'Use the tool function "send_to_viking" exactly once with prompt "authentication flow". '
        "After the tool result arrives, answer with exactly: OK"
    )
    source_names, uris = _run_stream_tool_probe(
        session=session,
        openwebui_base_url=openwebui_base_url,
        auth_headers=auth_headers,
        model=model,
        tool_id=tool_id,
        prompt=search_prompt,
        expected_function="send_to_viking",
    )

    if not any(name == f"{tool_id}/send_to_viking" for name in source_names):
        _fail(
            "OpenWebUI chat did not execute send_to_viking. "
            "Ensure the tool is enabled in the chat/model and tool calls are allowed."
        )

    _pass("OpenWebUI executed send_to_viking through the model")

    if not uris:
        _fail("send_to_viking returned no Viking URIs, cannot continue query_openviking probe.")

    target_uri = uris[0]
    _info(f"Using URI from search for read probe: {target_uri}")

    read_prompt = (
        f'Use the tool function "query_openviking" exactly once with context_id "{target_uri}". '
        "After the tool result arrives, answer with exactly: OK"
    )
    source_names, _ = _run_stream_tool_probe(
        session=session,
        openwebui_base_url=openwebui_base_url,
        auth_headers=auth_headers,
        model=model,
        tool_id=tool_id,
        prompt=read_prompt,
        expected_function="query_openviking",
    )

    if not any(name == f"{tool_id}/query_openviking" for name in source_names):
        _fail(
            "OpenWebUI chat did not execute query_openviking. "
            "Ensure the tool stays selected and the prompt is not overridden by other system instructions."
        )

    _pass("OpenWebUI executed query_openviking through the model")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check OpenViking bridge integration end-to-end.",
    )
    parser.add_argument(
        "--openwebui-base-url",
        default=os.environ.get("OPENWEBUI_BASE_URL", "http://localhost:8080"),
        help="OpenWebUI base URL",
    )
    parser.add_argument(
        "--ollama-base-url",
        default=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        help="Ollama base URL",
    )
    parser.add_argument(
        "--tool-id",
        default=os.environ.get("OPENWEBUI_TOOL_ID", "openviking_bridge"),
        help="OpenWebUI tool ID for the bridge",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENWEBUI_MODEL", "qwen3.5:27b"),
        help="Model ID to run the E2E tool probes",
    )
    parser.add_argument(
        "--target-uri",
        default=os.environ.get("OPENVIKING_TARGET_URI", "viking://resources/"),
        help="OpenViking URI used for direct fs/ls connectivity check",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session = requests.Session()

    try:
        _info("Checking base services")
        _check_ollama(session, args.ollama_base_url)
        _check_openwebui(session, args.openwebui_base_url)

        auth_headers, auth_source = _authenticate_openwebui(session, args.openwebui_base_url)
        _pass(f"OpenWebUI auth works ({auth_source})")

        valves = _load_tool_and_valves(
            session=session,
            openwebui_base_url=args.openwebui_base_url,
            auth_headers=auth_headers,
            tool_id=args.tool_id,
        )

        _check_openviking_with_valves(session, valves, args.target_uri)

        _info(f"Running tool probes with model '{args.model}'")
        _run_end_to_end_tool_checks(
            session=session,
            openwebui_base_url=args.openwebui_base_url,
            auth_headers=auth_headers,
            model=args.model,
            tool_id=args.tool_id,
        )

        _pass("End-to-end integration is healthy")
        return 0
    except CheckError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

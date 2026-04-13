#!/usr/bin/env python3
"""
Sync OpenViking context entries into an OpenWebUI Knowledge base.

Reads all leaf entries from OpenViking, writes each as a Markdown file,
and uploads to the specified OpenWebUI Knowledge collection.

Required env vars:
    OPENVIKING_BASE_URL
    OPENVIKING_API_KEY
    OPENWEBUI_BASE_URL
    OPENWEBUI_API_TOKEN
    OPENWEBUI_KNOWLEDGE_ID

Optional env vars:
    OPENVIKING_ACCOUNT   (default: "default")
    OPENVIKING_USER      (default: "")
    OPENVIKING_TARGET_URI (default: "viking://resources/")
    SYNC_TEMP_DIR        (default: system temp)
"""

import logging
import os
import sys
import tempfile
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sync_knowledge")

_CONNECT_TIMEOUT = 5
_READ_TIMEOUT = 30
_TIMEOUT = (_CONNECT_TIMEOUT, _READ_TIMEOUT)

# OpenViking endpoints
_EP_FS_LS = "/api/v1/fs/ls"
_EP_CONTENT_ABSTRACT = "/api/v1/content/abstract"
_EP_CONTENT_OVERVIEW = "/api/v1/content/overview"


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return val


def ov_session(base_url: str, api_key: str, account: str, user: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
    })
    if account:
        s.headers["X-OpenViking-Account"] = account
    if user:
        s.headers["X-OpenViking-User"] = user
    return s


def ov_get(session: requests.Session, base_url: str, endpoint: str, params: dict) -> dict:
    url = f"{base_url}{endpoint}"
    resp = session.get(url, params=params, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("status") != "ok":
        raise RuntimeError(f"OpenViking error: {data}")
    return data


def list_entries(session: requests.Session, base_url: str, target_uri: str) -> list[dict]:
    """List all entries under target_uri recursively."""
    entries: list[dict] = []
    # Paginate via abs_limit + offset isn't exposed in the ls API,
    # so we use recursive=true with a high node_limit and abs_limit.
    params = {
        "uri": target_uri,
        "simple": "false",
        "recursive": "true",
        "output": "original",
        "abs_limit": "5000",
        "node_limit": "5000",
        "show_all_hidden": "false",
    }
    data = ov_get(session, base_url, _EP_FS_LS, params)
    result = data.get("result", [])
    if not isinstance(result, list):
        return entries
    for item in result:
        if not isinstance(item, dict):
            continue
        entries.append(item)
    log.info("Listed %d entries from %s", len(entries), target_uri)
    return entries


def fetch_abstract(session: requests.Session, base_url: str, uri: str) -> str:
    """Fetch the L0 abstract for a URI. Returns empty string on failure."""
    try:
        data = ov_get(session, base_url, _EP_CONTENT_ABSTRACT, {"uri": uri})
        text = data.get("result", "")
        return text if isinstance(text, str) else ""
    except Exception as exc:
        log.warning("Failed to fetch abstract for %s: %s", uri, exc)
        return ""


def fetch_overview(session: requests.Session, base_url: str, uri: str) -> str:
    """Fetch the L1 overview for a URI. Returns empty string on failure."""
    try:
        data = ov_get(session, base_url, _EP_CONTENT_OVERVIEW, {"uri": uri})
        text = data.get("result", "")
        return text if isinstance(text, str) else ""
    except Exception as exc:
        log.warning("Failed to fetch overview for %s: %s", uri, exc)
        return ""


def uri_to_filename(uri: str) -> str:
    """Convert a Viking URI to a safe filename."""
    # viking://resources/example_app/FOO/BAR -> viking_context_FOO_BAR.md
    clean = uri.replace("viking://", "").replace("/", "_").replace(".", "_")
    clean = clean.strip("_")
    return f"viking_context_{clean}.md"


def write_entry_file(tmp_dir: Path, uri: str, name: str, abstract: str, overview: str) -> Path:
    """Write a single entry as a Markdown file."""
    fname = uri_to_filename(uri)
    path = tmp_dir / fname
    lines = [
        f"# {name}",
        "",
        f"**Viking URI:** `{uri}`",
        "",
    ]
    if abstract:
        lines += ["## Abstract", "", abstract, ""]
    if overview:
        lines += ["## Overview", "", overview, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def upload_to_openwebui(
    owui_base: str,
    owui_token: str,
    knowledge_id: str,
    file_path: Path,
) -> bool:
    """Upload a file to an OpenWebUI Knowledge base. Returns True on success."""
    url = f"{owui_base.rstrip('/')}/api/v1/knowledge/{knowledge_id}/file/add"
    headers = {"Authorization": f"Bearer {owui_token}"}
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                url,
                headers=headers,
                files={"file": (file_path.name, f, "text/markdown")},
                timeout=_TIMEOUT,
            )
        if resp.status_code < 300:
            return True
        log.warning(
            "Upload failed for %s: HTTP %d %s",
            file_path.name, resp.status_code, resp.text[:200],
        )
        return False
    except Exception as exc:
        log.warning("Upload error for %s: %s", file_path.name, exc)
        return False


def main() -> None:
    ov_base = require_env("OPENVIKING_BASE_URL").rstrip("/")
    ov_key = require_env("OPENVIKING_API_KEY")
    owui_base = require_env("OPENWEBUI_BASE_URL")
    owui_token = require_env("OPENWEBUI_API_TOKEN")
    knowledge_id = require_env("OPENWEBUI_KNOWLEDGE_ID")

    ov_account = os.environ.get("OPENVIKING_ACCOUNT", "default")
    ov_user = os.environ.get("OPENVIKING_USER", "")
    target_uri = os.environ.get("OPENVIKING_TARGET_URI", "viking://resources/")

    session = ov_session(ov_base, ov_key, ov_account, ov_user)

    entries = list_entries(session, ov_base, target_uri)
    if not entries:
        log.error("No entries found under %s — nothing to sync.", target_uri)
        sys.exit(1)

    # Filter to directories (they have abstracts/overviews)
    dirs = [e for e in entries if e.get("isDir", False)]
    log.info("Processing %d directory entries for sync.", len(dirs))

    tmp_dir = Path(os.environ.get("SYNC_TEMP_DIR", tempfile.mkdtemp(prefix="ov_sync_")))
    tmp_dir.mkdir(parents=True, exist_ok=True)

    ok_count = 0
    fail_count = 0

    for entry in dirs:
        uri = entry.get("uri", "")
        name = entry.get("name", "unknown")
        if not uri:
            continue

        abstract = fetch_abstract(session, ov_base, uri)
        overview = fetch_overview(session, ov_base, uri)

        if not abstract and not overview:
            log.debug("Skipping %s — no abstract or overview.", uri)
            continue

        fpath = write_entry_file(tmp_dir, uri, name, abstract, overview)
        success = upload_to_openwebui(owui_base, owui_token, knowledge_id, fpath)
        if success:
            ok_count += 1
        else:
            fail_count += 1

    log.info("Sync complete: %d uploaded, %d failed.", ok_count, fail_count)
    if fail_count > 0 and ok_count == 0:
        log.error("All uploads failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()

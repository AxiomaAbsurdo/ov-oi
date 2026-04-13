<p align="center">
  <img src="https://gemini.google.com/share/4ece03b9490d" alt="ov-oi-v1" />
</p>

# ov-oi-v1 — OpenViking ↔ OpenWebUI Bridge

An async, production-ready bridge that lets [OpenWebUI](https://github.com/open-webui/open-webui) talk to [OpenViking](https://github.com/volcengine/OpenViking) **live, during a chat** — with streaming, write-back, tunables, and audit logging.

No extra services. No middleware. Just a native OpenWebUI Tool plus a sync script for when the network has a bad day. Simpler than raw MCP (`:2033/mcp`) for OpenWebUI-only shops.

---

## Why this exists

Local stacks built on Ollama + OpenWebUI are great at reasoning and terrible at remembering. You can upload static docs, sure — but a week later your repo has moved on and the model is quoting yesterday's truth with full confidence.

`ov-oi-v1` fixes that by giving the model four verbs at inference time:

1. **Find things** semantically in OpenViking — with tunable `top_k` and `score_threshold`.
2. **Read things** by URI, deterministically — with async streaming for large entries.
3. **Write things** back to OpenViking under tracked sessions.
4. **Stream things** chunk-by-chunk for large context reads.

One tool file, one optional sync script, full audit trail. The model gets fresh context, you get answers grounded in source — not vibes.

---

## Architecture at a glance

```
        ┌────────────────────────────┐
        │        OpenWebUI           │
        │   (chat + model runtime)   │
        └─────────────┬──────────────┘
                      │
                      │ async tool calls
                      ▼
        ┌────────────────────────────┐
        │     viking_bridge.py       │   ← native OpenWebUI Tool (async)
        │  send_to_viking(prompt)    │   ← search + tunables
        │  query_openviking(uri)     │   ← read (L2→L1→L0 fallback)
        │  write_to_viking(uri,text) │   ← write-back + sessions
        │  query_openviking_stream() │   ← streaming read
        └─────────────┬──────────────┘
                      │ httpx + X-Api-Key + audit log
                      ▼
        ┌────────────────────────────┐
        │        OpenViking          │
        │  /content/read             │
        │  /content/overview   (L1)  │
        │  /content/abstract   (L0)  │
        │  /search/find              │
        │  /session/add_message      │   ← write-back
        │  /session/commit           │
        │  /fs/ls                    │
        └────────────────────────────┘

        ┌────────────────────────────┐
        │   OpenWebUI Knowledge KB   │  ← offline fallback
        └─────────────▲──────────────┘
                      │
              sync_knowledge.py
                      │
                      ▼
                  OpenViking
```

Two paths, one purpose: the **Tool** is the live wire, the **Knowledge sync** is the safety net.

---

## Design choices (and why we made them)

**Live context beats stale context.** The Tool calls OpenViking *during* the chat turn. No nightly snapshots, no "well, last time I checked...". If OpenViking knows it, the model can reach it.

**Four verbs, minimal ceremony.** `send_to_viking` discovers, `query_openviking` reads, `write_to_viking` writes back, `query_openviking_stream` streams large reads. Each earned its place through validated use cases.

**Progressive read fallback.** When you ask for a URI, `query_openviking` tries `read → overview → abstract` in order. Directory URIs and partially-indexed nodes still return *something useful* instead of an empty string and a shrug.

**Tenant-safe by default.** Every request carries `X-OpenViking-Account` and `X-OpenViking-User` headers. Multi-tenant setups stay isolated without you thinking about it.

**Config lives in Valves, not env vars.** OpenWebUI's Valves UI handles the tool's runtime config. Rotating a key is a click, not a redeploy.

**Resilience without complexity.** `sync_knowledge.py` mirrors OpenViking summaries into an OpenWebUI Knowledge collection. When OpenViking is down or slow, the model still has *something* to ground on.

**Loud, predictable failures.** HTTP errors, auth failures, timeouts, and malformed payloads all become explicit tool errors. Models behave better when their tools either work or clearly don't — silent half-success is the worst case.

---

## Requirements

- Python **3.12+**
- `httpx` and `pydantic` v2 (`pip install httpx pydantic`)
- `pytest`, `pytest-asyncio` — only if you want to run the test suite
- A reachable **OpenViking** server (default `http://localhost:1933`)
- **OpenWebUI v0.8.x** or newer

---

## Configuration

### `viking_bridge.py` — configured through Valves

After you register the tool in OpenWebUI, click the **gear icon** on its card and fill in:

| Valve | Default | What it does |
|---|---|---|
| `openviking_base_url` | `http://localhost:1933` | Where your OpenViking lives |
| `openviking_api_key` | *(empty)* | Your OpenViking API key |
| `openviking_account` | `default` | Tenant/account ID |
| `openviking_user` | *(empty)* | Default tenant/user ID |
| `user_mapping` | *(empty)* | JSON dict: OpenWebUI user → OpenViking user (per-user ACL) |
| `search_endpoint` | `/api/v1/search/find` | Override the search endpoint path |

No environment variables needed for the tool itself. That's the whole point.

### `sync_knowledge.py` — environment variables

Required:

| Variable | Description |
|---|---|
| `OPENVIKING_BASE_URL` | OpenViking server URL |
| `OPENVIKING_API_KEY` | OpenViking API key |
| `OPENWEBUI_BASE_URL` | OpenWebUI URL, e.g. `http://localhost:3000` |
| `OPENWEBUI_API_TOKEN` | OpenWebUI **admin** API token |
| `OPENWEBUI_KNOWLEDGE_ID` | Target Knowledge collection ID in OpenWebUI |

Optional:

| Variable | Default | Description |
|---|---|---|
| `OPENVIKING_ACCOUNT` | `default` | Tenant account |
| `OPENVIKING_USER` | *(empty)* | Tenant user |
| `OPENVIKING_TARGET_URI` | `viking://resources/` | Root URI to mirror |
| `SYNC_TEMP_DIR` | system temp | Where intermediate Markdown lands |

---

## Setup

### 1. Register the Tool

1. OpenWebUI → **Workspace** → **Tools** → **+**
2. Paste the full contents of `viking_bridge.py`
3. Save
4. Click the **gear icon** on the tool card → fill in the Valves
5. In each model you want to enable it on: **Model settings → Tools → toggle on**

### 2. Create a Knowledge collection (for the fallback)

1. OpenWebUI → **Knowledge** → **Create Collection**
2. Name it something obvious like `OpenViking Context`
3. Copy the Knowledge ID from the URL
4. Use it as `OPENWEBUI_KNOWLEDGE_ID` below

### 3. Run the sync script

```bash
export OPENVIKING_BASE_URL="http://localhost:1933"
export OPENVIKING_API_KEY="ov_live_xxxxxxxxxxxx"
export OPENVIKING_ACCOUNT="default"
export OPENVIKING_USER="example_user"
export OPENVIKING_TARGET_URI="viking://resources/example_app/"

export OPENWEBUI_BASE_URL="http://localhost:3000"
export OPENWEBUI_API_TOKEN="sk-openwebui-xxxxxxxxxxxx"
export OPENWEBUI_KNOWLEDGE_ID="kb_01HZX..."

python sync_knowledge.py
```

Run it on a cron, a CI schedule, or a git hook — whatever fits. Every run keeps the fallback honest.

### 4. Attach the skill

Drop `viking_skill.md` into the model's system prompt (or attach it as a system-level instruction in the model settings). It tells the model **when** to call which verb, which is half the battle.

---

## Dead-simple usage examples

Once the tool is wired up, you don't call anything yourself. You just talk to the model and it picks the right verb. Examples of prompts that trigger each path:

### Semantic discovery → `send_to_viking`

> *"How does the authentication flow work in example_app?"*

> *"What CORS settings does the backend ship with?"*

> *"Find the order-status update implementation."*

The model rephrases your question into a search, hits `/search/find`, and gets back a ranked list of URIs to follow up on.

### Direct read → `query_openviking`

> *"Read `viking://resources/example_app/AUTH_QUICK_REFERENCE`."*

> *"Show me `viking://resources/example_app/CORS_AND_ENV_IMPLEMENTATION_GUIDE`."*

The model takes the URI as-is and pulls content with the `read → overview → abstract` fallback.

### Combined flow (the common case)

> *"Find the rate-limiting code and explain how the token bucket is sized."*

Under the hood:

1. `send_to_viking("rate limiting token bucket")` → returns 3 candidate URIs
2. `query_openviking("viking://.../rate_limit.go")` → returns the source
3. Model reads it, explains it, cites the URI

### Minimal manual call (for debugging)

If you want to test the tool directly from a Python REPL inside OpenWebUI:

```python
# inside an OpenWebUI tool runner / Function context
result = send_to_viking("CORS configuration")
print(result)   # -> list of {uri, score, snippet}

doc = query_openviking("viking://resources/example_app/CORS_AND_ENV_IMPLEMENTATION_GUIDE")
print(doc[:500])
```

### Write-back → `write_to_viking`

> *"Summarize the rate-limiting design and save it to `viking://resources/example_app/RATE_LIMIT_SUMMARY`."*

Under the hood:
1. `send_to_viking("rate limiting")` → finds the URIs
2. `query_openviking("viking://.../rate_limit.go")` → reads the source
3. `write_to_viking("viking://.../RATE_LIMIT_SUMMARY", "...")` → writes the summary back
4. Session tracked as `webui_{user_id}_{chat_id}`

### Knowledge fallback

If OpenViking is unreachable, the model falls back to searching the synced Knowledge collection for files named `viking_context_<path>.md`. Same content, slightly stale — better than nothing.

---

## Pros vs. MCP

| | ov-oi-v1 Bridge | Raw MCP (`:2033/mcp`) |
|---|---|---|
| **Setup** | Paste one file, fill Valves | MCP server config, tool registration |
| **Auth** | Valves UI, per-user mapping | MCP auth protocol |
| **Streaming** | Built-in async chunks | MCP streaming transport |
| **Write-back** | `write_to_viking` tool | MCP resource write |
| **Audit** | Structured logger | MCP logging (if configured) |
| **Dependency** | `httpx` + `pydantic` | MCP SDK |
| **Best for** | OpenWebUI-only shops | Multi-client / multi-IDE setups |

---

## Running the tests

```bash
pip install pytest pytest-asyncio httpx
pytest test_viking_bridge.py -v
```

Tests cover the success path for both verbs plus error handling for HTTP failures, auth rejection, timeouts, and malformed envelopes.

---

## One-command health check

From the repo root:

```bash
python healthcheck_bridge.py
```

This walks the whole runtime chain end-to-end:

- ✅ Ollama is reachable
- ✅ OpenWebUI is reachable and authenticated
- ✅ The `openviking_bridge` tool exists in OpenWebUI and has Valves set
- ✅ OpenViking is reachable using those same Valves
- ✅ The selected model can actually invoke `send_to_viking` and `query_openviking` end-to-end

**Auth options for the health check:**

- *Preferred:* set `OPENWEBUI_API_TOKEN`
- *Fallback (local installs only):* the script can mint a JWT itself if it can read `~/.webui_secret_key` and find `webui.db`

**Defaults:**

| Setting | Default |
|---|---|
| OpenWebUI | `http://localhost:8080` |
| Ollama | `http://localhost:11434` |
| Tool ID | `openviking_bridge` |
| Model | `qwen3.5:27b` |

**Overrides** (CLI flag *or* environment variable):

- `--openwebui-base-url` / `OPENWEBUI_BASE_URL`
- `--ollama-base-url` / `OLLAMA_BASE_URL`
- `--tool-id` / `OPENWEBUI_TOOL_ID`
- `--model` / `OPENWEBUI_MODEL`
- `--target-uri` / `OPENVIKING_TARGET_URI`

---

## Repeatable regression workflow

For the stable CLI + UI canary process — prompts, pass/fail criteria, triage steps — see [`TESTING_WORKFLOW.md`](./TESTING_WORKFLOW.md).

---

## Docker

If you run OpenWebUI in Docker, drop `docker-compose.override.yml` next to your main `docker-compose.yml` and set the API key in `.env` or your shell:

```bash
export OPENVIKING_API_KEY="ov_live_xxxxxxxxxxxx"
docker compose up -d
```

The override uses `host.docker.internal` so the container can reach OpenViking running on the host.

---

## OpenViking API contract

The bridge assumes these endpoints exist and behave as documented in OpenViking server **v0.1.x**:

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/content/read?uri=` | `GET` | Read full (L2) content by Viking URI |
| `/api/v1/content/overview?uri=` | `GET` | Read L1 overview |
| `/api/v1/content/abstract?uri=` | `GET` | Read L0 abstract |
| `/api/v1/search/find` | `POST` | Semantic search — body: `{"query": "...", "limit": N}` |
| `/api/v1/session/add_message` | `POST` | Write content to a session — body: `{"uri": "...", "content": "...", "session_id": "..."}` |
| `/api/v1/session/commit` | `POST` | Commit a session — body: `{"uri": "...", "session_id": "..."}` |
| `/api/v1/fs/ls?uri=` | `GET` | List directory contents |

**Response envelope** (every endpoint):

```json
{
  "status": "ok",
  "result": { "...": "..." },
  "error": null,
  "telemetry": { "...": "..." }
}
```

**Headers sent on every request:**

- `X-Api-Key: <your key>`
- `X-OpenViking-Account: <account>` *(optional, for multi-tenant)*
- `X-OpenViking-User: <user>` *(optional, for multi-tenant)*

If your OpenViking version changes any of this, update the constants at the top of `viking_bridge.py` and `sync_knowledge.py`. They're deliberately grouped together for exactly this reason.

---

## Security

**Read the Tool code before you enable it in production.** It runs inside the OpenWebUI process, with access to whatever environment that process has access to. Specifically:

- API keys must never be logged or echoed in tool responses.
- Verify the Tool source matches what's in this repo — Tools are easy to paste, easy to tamper with.
- Store `OPENVIKING_API_KEY` and `OPENWEBUI_API_TOKEN` in a real secret store (Docker secrets, Vault, SOPS, your platform's secret manager) — **not** plaintext config files committed to git.
- Treat the OpenWebUI admin token like an SSH key. It is one.

If you find a security issue, please **do not** open a public issue. See [Reporting vulnerabilities](#reporting-vulnerabilities) below.

---

## Contributing

Contributions are welcome — but this project favors **small, sharp, well-tested** changes over big rewrites. The rules below are strict on purpose. They keep the bridge boring, and boring is exactly what an inference-time integration should be.

### Hard rules

1. **One concern per pull request.** A PR fixes one bug, adds one feature, or refactors one thing. Mixed PRs will be asked to split.
2. **Tests are not optional.** Any change to `viking_bridge.py` or `sync_knowledge.py` must come with tests. New behavior → new tests. Bug fix → regression test that fails before your fix and passes after.
3. **No new runtime dependencies** without a written justification in the PR description. The current dependency footprint (`requests`, `pydantic`) is intentional. If you need more, argue for it.
4. **No new services, daemons, or middleware.** This project is one Tool + one script. If your change requires a third moving part, open an issue first to discuss.
5. **The two-verb contract is sacred.** `send_to_viking` and `query_openviking` are the public API the model sees. Don't add a third verb without an issue, a discussion, and consensus. Don't change their signatures without a major version bump.
6. **Failures must be explicit.** No silent fallbacks, no swallowed exceptions, no `except: pass`. Errors become tool errors with clear messages.
7. **Secrets never appear in logs, responses, or test fixtures.** Ever. Run `git grep` before you push.
8. **Style:** `black` for formatting, `ruff` for linting, type hints on every public function. CI will reject anything that fails these.
9. **Commit messages** follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`).
10. **Sign your commits.** `git commit -s` — by signing, you accept the [Developer Certificate of Origin](https://developercertificate.org/).

### Workflow

1. **Open an issue first** for anything bigger than a typo or a one-line fix. Saves everyone time.
2. **Fork** the repo and create a branch named `feat/<short-name>`, `fix/<short-name>`, or `docs/<short-name>`.
3. **Write the test first** when fixing a bug. Watch it fail. Then fix.
4. **Run the full suite locally** before pushing:
   ```bash
   pytest -v
   ruff check .
   black --check .
   ```
5. **Run the health check** against a real (or local) OpenViking instance if your change touches the HTTP layer:
   ```bash
   python healthcheck_bridge.py
   ```
6. **Open a PR** against `main`. Fill out the PR template. Link the issue.
7. **Be patient and be kind.** Reviews are done by humans on their own time.

### What gets rejected fast

- PRs without tests
- PRs that add dependencies "just in case"
- PRs that change Valve names, env var names, or endpoint paths without a migration note
- PRs that introduce silent error handling
- PRs that reformat unrelated files
- PRs that include API keys, even fake-looking ones, in fixtures or examples

### Reporting vulnerabilities

For security issues, **do not open a public GitHub issue**. Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) on this repository, or email the maintainers directly (address in `SECURITY.md`). We'll acknowledge within 72 hours.

---

## License

This project is licensed under the **Apache License 2.0** — see [`LICENSE`](./LICENSE) for the full text.

Apache 2.0 was chosen deliberately over MIT because it includes an **explicit patent grant**, which matters for an integration project that touches multiple upstream codebases (OpenWebUI, OpenViking, Ollama). It's also the license most commonly used across the surrounding ecosystem, which keeps relicensing friction near zero for downstream users.

In short, you can:

- ✅ Use it commercially
- ✅ Modify it
- ✅ Distribute it
- ✅ Use it privately
- ✅ Use any patents the contributors hold on this code

You must:

- 📌 Include the license and copyright notice with any redistribution
- 📌 State significant changes you made
- 📌 Not use contributor names/trademarks for endorsement without permission

You cannot:

- ❌ Hold contributors liable
- ❌ Expect any warranty

```
Copyright 2026 The ov-oi-v1 Contributors

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

---

## Acknowledgments

- The [OpenWebUI](https://github.com/open-webui/open-webui) team for a Tools/Valves system that made this whole thing a single file.
- The [OpenViking](https://github.com/volcengine/OpenViking) project for an HTTP API clean enough to wrap in an afternoon.
- Everyone who filed an issue, ran the health check on a weird OS, and told us what broke.

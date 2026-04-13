# OpenViking Bridge - Repeatable Regression Workflow

Use this workflow to quickly confirm that OpenWebUI is still using Ollama and the `openviking_bridge` tool correctly.

## 1) One-time setup

1. Ensure these services are running:
   - OpenViking (`http://127.0.0.1:1933`)
   - Ollama (`http://localhost:11434`)
   - OpenWebUI (`http://localhost:8080`)
2. Confirm the tool exists in OpenWebUI:
   - Workspace -> Tools -> `OpenViking Bridge`
3. Confirm valves are configured for that tool:
   - `openviking_base_url`
   - `openviking_api_key`
   - `openviking_account`
   - `openviking_user`
4. (Recommended) Add `viking_skill.md` to your model/system instructions to bias the model toward correct tool usage.

## 2) CLI canary (fast, automated)

Run this from the repo root:

```bash
python healthcheck_bridge.py
```

The script verifies, in order:
- Ollama reachable
- OpenWebUI reachable
- OpenWebUI auth works
- `openviking_bridge` exists + valves present
- OpenViking reachable with those valves
- OpenWebUI executes `send_to_viking`
- OpenWebUI executes `query_openviking`

Pass condition: final line is `[PASS] End-to-end integration is healthy`.

## 3) Manual UI canary (human-visible)

Use a dedicated chat named `OV Bridge Canary` and keep it pinned for quick reuse.

1. Select model: `qwen3.5:27b` (or your intended production model).
2. Enable tool: `OpenViking Bridge` (tool selector in composer).
3. Run prompt A:

```text
Use send_to_viking to search "authentication flow" and report the top 2 URIs.
```

4. Copy one URI from the result and run prompt B:

```text
Use query_openviking with this URI and summarize in 3 bullets:
<PASTE_URI_HERE>
```

5. Service inventory canary (your target behavior), run prompt C:

```text
Use openviking resource `example_app` and list the existing project services in a table with: service name, Viking URI, and one-line description.
```

Manual pass criteria:
- You see tool/source attribution for `openviking_bridge/send_to_viking` and `openviking_bridge/query_openviking`.
- Responses include actual Viking URIs and retrieved content.
- No auth/timeout errors from OpenViking.
- Prompt C returns at least 3 service entries with valid Viking URIs and one-line descriptions.

## 4) Suggested cadence

- Before major sessions: run `python healthcheck_bridge.py`
- After changing model/tool/system prompt: run both CLI canary + manual UI canary
- Optional automation: run the script on a schedule (cron/systemd timer) and alert on non-zero exit code

## 5) If it fails

Use this quick triage order:

1. OpenWebUI reachable but no tool execution:
   - Tool likely not selected in current chat/model context.
2. Tool exists but OpenViking call fails:
   - Recheck valves (`base_url`, API key, account/user headers).
3. Tool call succeeds in direct script but not in normal chat:
   - System prompt/model instructions are overriding tool behavior; re-attach `viking_skill.md`.
4. Model answers from prior knowledge only:
   - Prompt explicitly for tool usage and verify tool/source attribution appears.
